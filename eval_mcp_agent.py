"""
eval_mcp_agent.py — MCP Agent 评估框架

回答一个问题：**这个 MCP Agent 到底能不能上生产？**

三个指标：
  1. 工具选择正确率 —— 模型选对工具了吗？（选错工具比答错更严重，
     因为它意味着模型对系统的理解有偏差，会在其他场景连环出错）
  2. 答案准确率     —— 答案包含期望关键字吗？
  3. 效率指标       —— 平均延迟 / 平均 token 用量

设计要点：
  · MCP server 只启动**一次**，所有 case 复用同一个会话。
    每次重连会额外花 1-2 秒，10 个 case 就是 10-20 秒的无效开销。
  · 每个 case 独立重置 messages，但保留 MCP 连接。
  · token 用量从 API 响应的 usage 里累加，这是真实的计费口径。

测试用例覆盖 4 类场景（不只是正常查询）：
  A正常查询   —— 参数明确，工具该选得对
  B 跨维度   —— 需要组合条件（如「上海的医疗客户」）
  C 零结果   —— mock 数据里没有的行业/城市，应当返回 0 条而非编造
  D 歧义输入 —— 只说「成交」不说城市/阶段，看模型会不会瞎猜参数

运行：
  python eval_mcp_agent.py
  python eval_mcp_agent.py -v        # 打印每个 case 的完整答案
  python eval_mcp_agent.py --only 3  # 只跑第 3 个 case
"""

import argparse
import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from openai import OpenAI

# 复用 client demo 的转换逻辑——评估代码不该复制一份
from mcp_client_demo import strip_title, mcp_tool_to_openai

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
MAX_ROUNDS = 5

# ============ 测试用例 ============
# category: normal / cross / empty / ambiguous
# expect_tool: 期望调用的工具；None 表示「不该调工具」（纯闲聊）
# must_contain: 答案必须包含的关键字（任一命中即可，除非 require_all）
# must_not: 答案不能出现的内容（用于检测编造）

CASES = [
    {
        "id": 1,
        "category": "normal",
        "question": "查一下上海有哪些教育行业的客户",
        "expect_tool": "query_customers",
        "must_contain": ["上海交大", "启明"],
    },
    {
        "id": 2,
        "category": "normal",
        "question": "金融行业有哪些客户？",
        "expect_tool": "query_customers",
        "must_contain": ["平安"],
    },
    {
        "id": 3,
        "category": "normal",
        "question": "上海的成交记录",
        "expect_tool": "query_deals",
        "must_contain": ["金额", "万"],
    },
    {
        "id": 4,
        "category": "normal",
        "question": "医疗行业客户名单",
        "expect_tool": "query_customers",
        "must_contain": ["医院"],
    },
    {
        "id": 5,
        "category": "normal",
        "question": "深圳的成交情况",
        "expect_tool": "query_deals",
        "must_contain": ["华为"],
    },
    {
        "id": 6,
        "category": "cross",
        "question": "上海有哪些医疗行业的客户？",
        "expect_tool": "query_customers",
        "must_contain": ["华山"],
        # 关键：上海的医疗客户只有华山一家，
        # 如果模型把北京的协和也答进来，说明 city 过滤没生效
        "must_not": ["协和"],
    },
    {
        "id": 7,
        "category": "cross",
        "question": "北京教育行业有哪些客户？",
        "expect_tool": "query_customers",
        "must_contain": ["新东方", "四中"],
    },
    {
        "id": 8,
        "category": "empty",
        "question": "查一下上海的航空航天行业客户",
        "expect_tool": "query_customers",
        # 关键：mock 数据里没有航空航天，Agent 必须说「没有」
        # 而不是编一家「上海航天科技」出来
        "must_contain": ["没有", "0", "未找到", "不存在"],
        "must_not": ["航空航天科技", "航天科技公司"],
        # 零结果场景 Agent 往往会二次查询做自查（确认「是不是真没有」），
        # 这是严谨行为不是缺陷，所以允许多次调用
        "max_calls": 3,
    },
    {
        "id": 9,
        "category": "empty",
        "question": "乌鲁木齐的客户名单",
        "expect_tool": "query_customers",
        "must_contain": ["没有", "0", "未找到", "不存在"],
    },
    {
        "id": 10,
        "category": "ambiguous",
        "question": "已成交的商机有哪些？",
        "expect_tool": "query_deals",
        # 没说城市→模型应留空（不限），不能瞎猜一个城市
        # 期望答案里含有真实的已成交记录（won 的有上海2笔、北京1笔、深圳1笔）
        "must_contain": ["交大", "启明", "新东方", "华为"],
        "min_hits": 3,          # 4 笔已成交里至少答对 3 笔
    },
    # ========== 压测用例：第一轮全过说明题目太简单，必须加难的 ==========
    {
        "id": 11,
        "category": "hard_multi",
        # 同一个问题既要客户又要成交，两个工具都要用。
        # 这考验的是「能不能串联多个工具」，也考验效率。
        # 平均调用次数应≤2（一次查客户 + 一次查该客户的成交）
        "question": "上海的教育行业客户有哪些？他们成交了多少？",
        "expect_tool": "query_customers",
        "must_contain": ["交大", "启明"],
        "max_calls": 2,
    },
    {
        "id": 12,
        "category": "hard_ambiguous",
        # 完全没说行业也没说城市，模型必须真的留空而不是瞎猜
        "question": "有哪些客户？",
        "expect_tool": "query_customers",
        # mock 里共 10 个客户，覆盖 教育/互联网/医疗/零售/金融
        "must_contain": ["交大", "华为", "华山"],
        "min_hits": 3,
    },
    {
        "id": 13,
        "category": "hard_wrong_tool",
        # 问的是客户，但关键词里有「成交」，容易诱导模型选 query_deals。
        # 这是工具选择的经典陷阱。
        "question": "上海有哪些客户？成交情况怎么样？",
        "expect_tool": "query_customers",
        "must_contain": ["交大"],
    },
    {
        "id": 14,
        "category": "hard_invalid",
        # 诱导模型填一个 schema 里不存在的 stage 枚举值。
        # 期望 server 返回错误后模型能自己改正（错误回传机制是否生效）
        "question": "查一下阶段是「已签约」的成交记录",
        "expect_tool": "query_deals",
        "must_contain": ["won", "合法", "无效", "阶段"],
    },
]


async def run_case(mcp, client_llm, openai_tools, case, verbose=False):
    """跑一个 case，返回评估结果 dict"""
    messages = [
        {"role": "system",
         "content": "你是一个 CRM 数据助手。回答用中文，先给结论再给数据。"
                    "只报告工具返回的内容，工具没返回的绝不要编造。"
                    "查不到数据时明确说明没有查到。"},
        {"role": "user", "content": case["question"]},
    ]

    called_tools = []
    tokens = {"prompt": 0, "completion": 0}
    t0 = time.perf_counter()

    final_answer = ""
    for _ in range(MAX_ROUNDS):
        resp = client_llm.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            max_tokens=3000,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            tokens["prompt"] += usage.prompt_tokens
            tokens["completion"] += usage.completion_tokens

        msg = resp.choices[0].message

        if not msg.tool_calls:
            final_answer = (msg.content or "").strip()
            break

        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            name = call.function.name
            called_tools.append(name)
            args = json.loads(call.function.arguments or "{}")
            result = await mcp.call_tool(name, args)

            payload = None
            for block in (result.content or []):
                if getattr(block, "type", None) == "text":
                    payload = block.text
                    break
            payload = payload or str(result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": payload,
            })
            if verbose:
                print(f"      ⚙ {name}({args})")

    elapsed = time.perf_counter() - t0

    # ---- 评估 ----
    # 指标 1：工具选择正确率
    expect_tool = case.get("expect_tool")
    if expect_tool is None:
        tool_ok = len(called_tools) == 0
    else:
        # 取第一次调用（模型可能调多次，取首个更能反映「选择」）
        tool_ok = bool(called_tools) and called_tools[0] == expect_tool

    # 指标 2：答案准确率
    answer = final_answer
    keywords = case.get("must_contain", [])
    keyword_hit = [k for k in keywords if k in answer]
    # 关键设计：must_contain 默认是**同义词的 OR 关系**——
    # 「没有查到」「未找到」「不存在」表达的是同一件事，
    # 命中任意一个就说明模型正确表达了「查不到」。
    #
    # 我第一版设了 min_hits=2 要求命中 2 个同义词，结果 case8
    # 答「没有查到上海的航空航天行业客户」完全正确，却因只命中
    # 「没有」一个被判失败。**规则比 Agent 错了。**
    #
    # min_hits 只用于「需要多个不同信息都答对」的场景（如 case10
    # 要求 4 笔成交里至少答对 3 笔），默认 1。
    min_hits = case.get("min_hits", 1)
    keyword_ok = len(keyword_hit) >= min_hits if keywords else True

    # 编造检测
    fabricated = [k for k in case.get("must_not", []) if k in answer]
    no_fabrication = not fabricated

    # 指标 4：工具调用次数（效率相关的独立维度）
    # 我第一版评估漏了这个，看分数全是 100% 就以为没问题。
    # 实际上 case8调了 3 次工具——Agent 在自查「是不是数据里就没这个行业」。
    # 这个行为本身**是对的**（严谨），但它直接影响 token 成本和延迟，
    # 必须在报告里显式呈现，否则评估就是自欺欺人。
    n_calls = len(called_tools)
    excessive_calls = n_calls > case.get("max_calls", 2)

    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "called_tools": called_tools,
        "n_calls": n_calls,
        "excessive_calls": excessive_calls,
        "expect_tool": expect_tool,
        "tool_ok": tool_ok,
        "keyword_ok": keyword_ok,
        "keyword_hit": keyword_hit,
        "keyword_miss": [k for k in keywords if k not in answer],
        "fabricated": fabricated,
        "no_fabrication": no_fabrication,
        "answer_len": len(answer),
        "elapsed": elapsed,
        "tokens": tokens,
        "answer": answer,
        # 「通过」只看正确性，不含效率——效率单列指标，避免用快掩盖错
        "pass": tool_ok and keyword_ok and no_fabrication,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--only", type=int, help="只跑指定 id 的 case")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY")
        sys.exit(1)

    cases = [c for c in CASES if not args.only or c["id"] == args.only]
    if args.only and not cases:
        print(f"✗ 没有 id={args.only} 的用例")
        sys.exit(1)

    client_llm = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

    python = os.path.abspath(sys.executable)
    transport = StdioTransport(
        command=python,
        args=["mcp_server.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    results = []
    # MCP server 只启动一次，所有 case 复用同一个连接
    async with Client(transport) as mcp:
        tools = await mcp.list_tools()
        openai_tools = [strip_title(mcp_tool_to_openai(t)) for t in tools]

        print("=" * 70)
        print(f"  MCP Agent 评估 | {len(cases)} 个用例 | model={MODEL}")
        print("=" * 70)
        print(f"  MCP server 已启动，暴露 {len(openai_tools)} 个工具：")
        for t in openai_tools:
            print(f"    - {t['function']['name']}")

        for case in cases:
            print(f"\n{'─' * 70}")
            print(f"[{case['id']}] {case['question']}  ({case['category']})")
            if args.verbose:
                print(f"    期望工具：{case.get('expect_tool')} | "
                      f"期望关键字：{case.get('must_contain')}")
            res = await run_case(mcp, client_llm, openai_tools, case,
                                 verbose=args.verbose)
            results.append(res)

            mark = "✓" if res["pass"] else "✗"
            print(f"  {mark} 工具：{res['called_tools'] or '（未调用）'}"
                  f" | 关键字命中：{res['keyword_hit']}"
                  + (f" | 未命中：{res['keyword_miss']}" if res["keyword_miss"] else ""))
            print(f"    {res['elapsed']:.1f}s | "
                  f"{res['tokens']['prompt']}+{res['tokens']['completion']} tokens"
                  f" | 答案 {res['answer_len']} 字")
            if res["fabricated"]:
                print(f"    ⚠ 检测到编造内容：{res['fabricated']}")
            if args.verbose:
                print(f"    答案：{res['answer'][:300]}")

    # ============ 汇总报告 ============
    n = len(results)
    tool_ok_n = sum(r["tool_ok"] for r in results)
    kw_ok_n = sum(r["keyword_ok"] for r in results)
    fab_n = sum(1 for r in results if not r["no_fabrication"])
    pass_n = sum(r["pass"] for r in results)
    avg_latency = sum(r["elapsed"] for r in results) / n
    avg_prompt = sum(r["tokens"]["prompt"] for r in results) / n
    avg_completion = sum(r["tokens"]["completion"] for r in results) / n
    avg_tokens = avg_prompt + avg_completion
    avg_calls = sum(r["n_calls"] for r in results) / n

    # 按类别的细分
    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    print("\n" + "=" * 70)
    print("  评估汇总报告")
    print("=" * 70)

    print(f"\n【核心指标】")
    print(f"  ① 工具选择正确率    : {tool_ok_n}/{n} = {tool_ok_n / n * 100:.0f}%")
    print(f"  ② 答案准确率        : {kw_ok_n}/{n} = {kw_ok_n / n * 100:.0f}%")
    print(f"  ③ 无编造率          : {n - fab_n}/{n} = {(n - fab_n) / n * 100:.0f}%")
    print(f"  综合通过率          : {pass_n}/{n} = {pass_n / n * 100:.0f}%")

    print(f"\n【效率指标】")
    print(f"  平均延迟            : {avg_latency:.1f}s")
    print(f"  平均 token 输入     : {avg_prompt:.0f}")
    print(f"  平均 token 输出     : {avg_completion:.0f}")
    print(f"  平均 token 合计     : {avg_tokens:.0f}")
    # DeepSeek flash 价格：输入 $0.14/M，输出 $0.28/M
    cost = (avg_prompt * 0.14 + avg_completion * 0.28) / 1_000_000
    print(f"  平均单次成本估算    : ${cost:.6f}")

    print(f"\n【工具调用次数】")
    print(f"  平均调用次数        : {avg_calls:.2f} 次/用例")
    multi = [r for r in results if r["n_calls"] > 1]
    excessive = [r for r in results if r["excessive_calls"]]
    print(f"  多次调用的用例      : {len(multi)}/{n}"
          f"（case {[r['id'] for r in multi]}）")
    print(f"  调用次数超预期的    : {len(excessive)}/{n}"
          f"（case {[r['id'] for r in excessive]}）")
    if multi:
        print("  说明：重复调用不一定是缺陷。零结果场景下 Agent 二次查询做自查")
        print("        （例：「查不到 A，再查全部确认是不是真没有」）是严谨行为，")
        print("        但会实打实推高 token 与延迟，需要在成本敏感场景优化。")

    print(f"\n【分类别表现】")
    cat_names = {
        "normal": "正常查询",
        "cross": "组合条件",
        "empty": "零结果",
        "ambiguous": "歧义输入",
        "hard_multi": "多工具串联",
        "hard_ambiguous": "极简输入",
        "hard_wrong_tool": "工具诱导",
        "hard_invalid": "非法参数",
    }
    for cat, rs in by_cat.items():
        ok = sum(r["pass"] for r in rs)
        avg_t = sum(r["elapsed"] for r in rs) / len(rs)
        print(f"  {cat_names.get(cat, cat):8} {ok}/{len(rs)} 通过"
              f" | 平均 {avg_t:.1f}s")
        for r in rs:
            if not r["pass"]:
                reasons = []
                if not r["tool_ok"]:
                    reasons.append(f"选错工具(得到 {r['called_tools']})")
                if not r["keyword_ok"]:
                    reasons.append(f"关键字缺失 {r['keyword_miss']}")
                if r["fabricated"]:
                    reasons.append(f"编造 {r['fabricated']}")
                print(f"      ✗ [{r['id']}] {r['question'][:24]}"
                      f" → {'; '.join(reasons)}")

    # 结果落盘，便于跨次对比
    out = {
        "model": MODEL,
        "cases": n,
        "tool_accuracy": tool_ok_n / n,
        "answer_accuracy": kw_ok_n / n,
        "pass_rate": pass_n / n,
        "avg_latency": round(avg_latency, 2),
        "avg_tokens": round(avg_tokens, 1),
        "detail": [{k: v for k, v in r.items() if k != "answer"} for r in results],
    }
    with open("eval_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  详细结果已写入 eval_result.json")


if __name__ == "__main__":
    asyncio.run(main())