"""
multi_agent_graph.py — LangGraph 三 Agent 协作（状态机编排）

主题：「2025 年 LLM Agent 的发展」
Planner→ Researcher → Writer

这是 LangGraph 的核心用法，四个概念必须分清：

  1. **State（状态）**：用 TypedDict 定义，是一个「全局共享的字典」。
     每个节点读它、改它，返回**要更新的部分**（不用返回全部）。

  2. **Node（节点）**：普通 Python 函数，签名是 `(state) -> dict`。
     只负责做一件事，改状态里的某几个字段。

  3. **Edge（边）**：决定「谁之后是谁」。分两种——
       普通边：固定流向，  planner → researcher
       条件边：运行时判断，  add_conditional_edges

  4. **Graph（图）**：StateGraph 声明结构，compile() 之后才能跑。
     compile() 是必须的，类比「类」与「实例」。

为什么用状态机而不是直接写 if-else？
  because（因为）多 Agent 协作里，**执行顺序会变**。
  比如 researcher 发现某个子问题资料不足，需要回退给 planner 重新拆；
  或者 writer 发现素材太少，需要再查一轮。这种「回头」用 if-else
  写出来是嵌套地狱，用图就是加一条边。

运行：
    python multi_agent_graph.py
    python multi_agent_graph.py --ascii      # 打印 ASCII 版流程图
    python multi_agent_graph.py --trace      # 打印每个节点的执行轨迹
"""

import os
import re
import sys
import time

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from typing import Annotated, TypedDict
import operator

from openai import OpenAI

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
REPORT_WORD_LIMIT = 200

_client = None


def client():
    """全局单例。3 个节点共用一个连接，省去重复建连"""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL
        )
    return _client


def llm(system, user, max_tokens=800, retries=3):
    """
    统一的 LLM 调用封装。两个要点：

    1) 出错返回空串而不是抛异常——图里任何一个节点抛异常，整张图就崩了。
    2) max_tokens 必须留足余量，且要按「指数重试」放大。

    为什么（一个很难查的坑）：
    DeepSeek 思考模式下，max_tokens 是**思考链 + 正文共享的上限**。
    我设 350 时，模型把 350 token 全花在 reasoning_content（实测 1283 字符），
    正文一字未出——返回 finish_reason=length 和空字符串。
    表现为「API 成功但内容为空」，极易误判成 prompt 问题。

    关键：**思考链长度与输入长度正相关**，不能按「输出多少字」估算预算。
    Writer 要读 3 条长资料，思考链能到几千 token；
    我第一次只给 3900，依然被吃光。所以这里用指数放大
    （×3 → ×8 → ×20），而不是拍一个固定倍数。
    """
    budget = max_tokens * 3 + 400

    for attempt in range(retries):
        try:
            r = client().chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=budget,
            )
            text = (r.choices[0].message.content or "").strip()
            if text:
                return text

            # 正文为空——预算被思考链吃光，指数放大后重试
            reason = (getattr(r.choices[0].message, "reasoning_content", "")
                      or "")
            print(f"    ⚠ 正文为空（finish_reason="
                  f"{r.choices[0].finish_reason}，思考链 {len(reason)} 字）"
                  f"，预算 {budget} → {budget * 4}")
            budget *= 4
        except Exception as e:
            print(f"    [LLM 错误] {type(e).__name__}: {e}")
            return ""

    return ""


def parse_sub_questions(raw, topic="", limit=3):
    """把 LLM 返回的编号列表解析成子问题数组。

    独立成函数是为了能单独测试——这类字符串解析很容易被
    意料之外的格式坑到，而它出问题会让整个图静默降级。

    处理过的格式：
      "1. xxx"  "2、xxx"  "3)xxx"  "3）xxx"（全角）
      "- xxx"   "* xxx"   "• xxx"
      无编号裸行（也接受，LLM 常常忘记编号）
    """
    subs = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # 剥编号。必须用正则按「数字+分隔符」整体匹配，
        # 绝不能用 lstrip——它是按字符集剥，
        # lstrip("1. 2. ") 遇到 "2025年..." 会把 2/0/5 当编号剥掉。实测踩过。
        ln = re.sub(r"^(\d+)\s*[\.、\)）:：]\s*", "", ln)
        # 剥列表符号
        ln = re.sub(r"^[-*•·]\s*", "", ln)
        # 剥 Markdown 粗体/反引号
        ln = ln.strip("*`_ ").strip()
        if 2 <= len(ln) <= 100:
            subs.append(ln)

    subs = subs[:limit]
    if len(subs) < 2:
        # 兜底：模型不听话时自己补，保证 researcher 至少有活干
        filler = [f"{topic}的现状与规模", f"{topic}的主要挑战",
                  f"{topic}的发展趋势"]
        subs += filler[len(subs):limit]
    return subs


# ============ 1. State：全局共享状态 ============

class ReportState(TypedDict):
    """
    整个图共享的状态。

    两个设计要点：

    1. 用 Annotated + operator.add 而不是直接写 list
       → 研究资料用「累加」合并，多个节点追加时自动拼接
       → 不加的话后写的会覆盖先写的（LangGraph 默认覆盖语义）
    """
    topic: str                                  # 输入：主题
    sub_questions: list                          # Planner 产出：子问题
    research: Annotated[list, operator.add]      # Researcher 产出：逐条累加
    report: str                                  # Writer 产出：最终报告
    retry_count: int                             # 控制回退次数，防死循环


# ============ 2. Node：三个 Agent ============

def planner(state: ReportState) -> dict:
    """
    Planner（拆任务）：把主题拆成 2-3 个子问题。
    返回值只需包含**要更新的字段**，LangGraph 会自动合并进全局 state。
    """
    topic = state["topic"]
    print(f"\n{'=' * 60}\n[Planner] 拆解主题：{topic}\n{'-' * 60}")

    raw = llm(
        system=(
            "你是研究型任务规划者。把给定主题拆成 3 个互不重叠的子问题，"
            "每个子问题要具体、可独立查证。"
            "输出格式：每行一个子问题，以『1. 』『2. 』『3. 』编号开头，"
            "不要任何解释文字、标题或前后缀。必须输出 3 行。"
        ),
        user=f"主题：{topic}\n\n请拆成 3 个子问题，每行一个，以编号开头。",
        max_tokens=400,
    )

    subs = parse_sub_questions(raw, topic, limit=3)

    for i, q in enumerate(subs, 1):
        print(f"  子问题 {i}: {q}")
    # 注意：这里**绝不能**返回 retry_count: 0。
    # planner 会被条件边回退调用，一旦它把计数器清零，
    # 「失败 → planner → 重置 → 再失败」就是无限循环。
    # 我第一版就是踩了这个坑，图跑了 3 分钟没停。
    # 计数器只能由 count_retry 递增，永不重置。
    return {"sub_questions": subs}


def researcher(state: ReportState) -> dict:
    """
    Researcher（查资料）：对**每个**子问题调 web_search 拿摘要。

    这里用 DeepSeek 模拟 web_search 的返回（题目允许）。
    真实场景替换成 Tavily / SerpAPI / 内部搜索服务即可——
    只需要保证返回值是 (子问题, 摘要) 的列表，
    节点逻辑一行都不用改。这是「工具可替换」的设计。
    """
    subs = state["sub_questions"]
    print(f"\n{'=' * 60}\n[Researcher] 查资料，共 {len(subs)} 个子问题\n{'-' * 60}")

    results = []
    for q in subs:
        print(f"  搜索：{q}")
        summary = llm(
            system=(
                "你是资料检索助手。基于你的知识，针对问题给出一段 60-100 字的事实性摘要，"
                "包含关键数据、时间点、机构或产品名。语言平实，不要空话。"
                "注意：只讲你确实知道的，不确定的不要编造。"
            ),
            user=f"问题：{q}",
            max_tokens=350,
        )
        if not summary:
            print("    ⚠ 未拿到摘要")
            continue
        results.append({"question": q, "summary": summary})
        print(f"    ✓ 拿到 {len(summary)} 字摘要")

    return {"research": results}   # Annotated 累加语义，会追加而非覆盖


def writer(state: ReportState) -> dict:
    """
    Writer（合成报告）：把所有子答案拼成约 200 字报告。
    """
    topic = state["topic"]
    research = state.get("research") or []
    print(f"\n{'=' * 60}\n[Writer] 合成报告（目标 {REPORT_WORD_LIMIT} 字）"
          f"\n{'-' * 60}")

    if not research:
        print("  ⚠ 没有任何研究资料，返回降级报告")
        return {"report": f"（研究阶段未产出资料，无法生成「{topic}」的报告）"}

    material = "\n\n".join(
        f"【子问题】{r['question']}\n【资料】{r['summary']}" for r in research
    )
    # 字数控制的关键：把结构要求压缩成一条，别给太多「段落要求」。
    # 之前我写了「开头一句+随后分段+结尾一句」，结果模型写出 464 字——
    # 因为「分段」这个要求天然需要篇幅，字数上限被结构性指令淹没了。
    # 规则越具体，模型越容易顾此失彼。
    report = llm(
        system=(
            f"你是技术报告撰写者。根据资料写一份**不超过 {REPORT_WORD_LIMIT} 字**的中文报告。"
            f"硬性要求：全文总长度必须在 {REPORT_WORD_LIMIT - 40} 到 {REPORT_WORD_LIMIT} 字之间，"
            f"这比任何结构要求都重要。"
            "结构：单段式，不要分小标题，不要列表。"
            "内容：首句给核心判断，随后用 2-3 句支撑它。"
            "只用资料里的信息，不要添加资料中没有的内容。"
            "直接输出正文，不要标题、不要说明、不要字数统计。"
        ),
        user=f"主题：{topic}\n\n资料：\n{material}",
        max_tokens=REPORT_WORD_LIMIT + 100,
    )

    # 兜底裁剪：模型偶尔不听话，但用户要的是 200 字，超了就是没做到
    if len(report) > REPORT_WORD_LIMIT * 1.5:
        print(f"  ⚠ 报告 {len(report)} 字，超出目标，截断至约 {REPORT_WORD_LIMIT} 字")
        report = report[:REPORT_WORD_LIMIT].rstrip("，、") + "……"

    print(f"  ✓ 报告已生成，{len(report)} 字（目标 {REPORT_WORD_LIMIT}）")
    return {"report": report}


# ============ 3. 条件路由 ============

MAX_RETRY = 3
MAX_RESEARCH_CHARS = 200


def route_after_research(state: ReportState) -> str:
    """
    条件边：Researcher 之后决定去哪。

    这就是「状态机」和「流水线」的本质区别——
    流水线是固定顺序，状态机允许根据数据动态分流。

    # 路由规则：
      资料为空        → 回 planner（重拆）
      资料不足 2 条  → 经 count_retry 计数后回 researcher（补查）
      资料齐全        → 去 writer
      超过重试上限    → 强制去 writer（降级输出，绝不无限循环）

    注意：返回值必须和下面 path_map 的键完全一致，否则报 KeyError。
    """
    n = len(state.get("research") or [])
    retry = state.get("retry_count", 0)

    # 硬上限：无论什么情况，到顶了就去 writer。
    # 图里的回退边一定要有这层保护，否则一个死循环能把进程挂死。
    if retry >= MAX_RETRY:
        print(f"\n[路由] 重试已达上限 {MAX_RETRY} → 强制去 writer（降级输出）")
        return "writer"

    if n == 0:
        print(f"\n[路由] 资料为空 → 回 planner 重拆（retry={retry}）")
        return "planner"

    if n < 2:
        print(f"\n[路由] 资料仅 {n} 条，不足 → 经 count_retry 后回 researcher 补查"
              f"（retry={retry}）")
        return "count_retry"

    print(f"\n[路由] 资料齐全（{n} 条）→ 去 writer")
    return "writer"


def count_retry(state: ReportState) -> dict:
    """辅助节点：把重试次数 +1。

    任何**回退路径**都必须经过它，否则计数器不增长，
    上限判断就永远不触发。纯逻辑操作，不调 LLM。
    """
    n = state.get("retry_count", 0) + 1
    print(f"  [重试计数] {state.get('retry_count', 0)} → {n} /上限 {MAX_RETRY}")
    return {"retry_count": n}


# ============ 4. 组装图 ============

def build_graph():
    """
    组装 StateGraph。

    图结构（含回退边）：

                    ┌──────────────┐
                    │              │
      START → planner → researcher ─┘(资料不足,回退)
                    │       │
                    │       ↓(资料齐全)
                    │     writer → END
                    │
                    └─→ researcher（planner 重试后再查）
    """
    g = StateGraph(ReportState)

    g.add_node("planner", planner)
    g.add_node("researcher", researcher)
    g.add_node("writer", writer)
    g.add_node("count_retry", count_retry)

    # --- 边 ---
    g.add_edge(START, "planner")
    g.add_edge("planner", "researcher")
    g.add_edge("writer", END)

    # 条件边：从 researcher 出发，运行时决定去向
    g.add_conditional_edges(
        "researcher",
        route_after_research,
        {
            # 键必须与 route_after_research 的返回值完全一致，
            # 值是下一个要执行的节点名。两处对不上就是 KeyError。
            "planner": "count_retry",
            "count_retry": "count_retry",
            "writer": "writer",
        },
    )
    # count_retry 之后按状态决定回planner 还是回 researcher
    g.add_conditional_edges(
        "count_retry",
        lambda s: "planner"
        if not (s.get("research") or [])
        else "researcher",
        {"planner": "planner", "researcher": "researcher"},
    )

    return g.compile()


# ============ 5. 跑起来 ============

def show_graph(graph):
    """打印图的真实结构。能画出来就说明边没有非法定义。"""
    print("\n图结构（从编译后的图对象读出，非手写）：")
    try:
        print(graph.get_graph().draw_ascii())
    except Exception:
        #不同版本 draw_ascii 行为不同，退回打印节点列表
        nodes = list(getattr(graph, "nodes", {}).keys())
        print(f"  节点：{nodes}")

    print("\n  条件边：researcher 的去向由 route_after_research 决定")
    print("    资料为空 → planner（重试拆题）")
    print("    资料<2 条 → count_retry → researcher（补查）")
    print("    资料齐全  → writer（合成）")


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    topic = "2025 年 LLM Agent 的发展"
    args = sys.argv[1:]
    if args and not args[0].startswith("--"):
        topic = args[0]

    print("═" * 60)
    print("  LangGraph 三 Agent 协作")
    print("═" * 60)
    print(f"  主题：{topic}")
    print(f"  模型：{MODEL}")

    if "--ascii" in args:
        show_graph(build_graph())
        return

    graph = build_graph()

    init_state: ReportState = {
        "topic": topic,
        "sub_questions": [],
        "research": [],
        "report": "",
        "retry_count": 0,
    }

    t0 = time.perf_counter()
    # recursion_limit（递归上限）是 LangGraph 自带的安全网。
    # 即使路由逻辑有 bug、出现我没预料到的环，它也会在这里停下并抛错，
    # 而不是让进程无限跑。单位是「节点执行总次数」。
    config = {"recursion_limit": 25}
    try:
        if "--trace" in args:
            # stream 模式逐节点打印，适合调试图。
            # stream 只返回「各节点的增量更新」，不是完整状态，
            # 所以要自己把它们合并起来还原最终状态。
            print("\n[执行轨迹]")
            merged = dict(init_state)
            for chunk in graph.stream(init_state, config,
                                      stream_mode="updates"):
                for node, upd in chunk.items():
                    fields = list(upd.keys())
                    print(f"↓ 节点 {node} 完成，更新字段：{fields}")
                    # research 是 Annotated 累加语义，stream 里已是增量
                    merged.update(upd)
            final = merged
        else:
            final = graph.invoke(init_state, config)
    except Exception as e:
        print(f"\n✗ 图执行中断：{type(e).__name__}: {e}")
        print("  通常是recursion_limit 触发，说明路由逻辑有问题")
        sys.exit(1)

    dt = time.perf_counter() - t0

    print("\n" + "═" * 60)
    print("  最终产物")
    print("═" * 60)
    print(f"\n主题：{topic}")
    print(f"子问题数：{len(final.get('sub_questions') or [])}")
    print(f"资料条数：{len(final.get('research') or [])}")
    print(f"回退次数：{final.get('retry_count', 0)}")
    print(f"总耗时：{dt:.1f}s")
    print(f"\n{'─' * 60}\n报告正文：\n")
    print(final.get("report") or "（无报告）")
    print(f"\n{'─' * 60}\n各子问题的资料：\n")
    for i, r in enumerate(final.get("research") or [], 1):
        print(f"\n[{i}] {r['question']}")
        print(f"    {r['summary'][:150]}...")


if __name__ == "__main__":
    main()