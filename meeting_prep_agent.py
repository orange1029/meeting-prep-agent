"""
meeting_prep_agent.py — 会前简报 Agent

输入客户 + 行业 + 议题，自动产出一页markdown（轻量标记语言）简报。

用法：
    python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "数据中台建设"
    python meeting_prep_agent.py --client "平安银行" --industry "金融" --topic "实时风控" --no-stream

输出：
    briefings/招商银行_2026-10-13.md

设计取舍（三个刻意的选择）：

1. **不用流式**做默认输出。
   流式在这里体验很差——简报是长markdown，终端里逐字打印没法看。
   真正该流式的是「进度」（正在查客户档案…），所以这里保留进度输出，
   正文一次性打印。加 --no-stream 可关掉进度。

2. **简报模板是固定的，LLM 只负责填内容。**
   让 LLM 自由生成整篇 markdown 会出现格式漂移（这次有表格下次没表格），
   且没法在模板里强制「无历史记录必须显式说明」这类硬要求。
   所以模板由代码控制，LLM 只填四个段落。

3. **强制诚实检查。**
   如果某个数据源返回了 error 或空，简报里必须显式写明，
   不能让 LLM 用推测填补。这一步在代码里做，不靠 prompt 祈祷。
"""

import argparse
import json
import os
import sys
import time
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

from prep_tools_impl import run_prep_tool
from prep_tools_schema import ALL_PREP_TOOLS

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
BRIEFING_DIR = "briefings"
MAX_ROUNDS = 6

SYSTEM_PROMPT = (
    "你是一名资深售前顾问的助手，负责在会面客户前整理简报。"
    "工作准则："
    "1. 只使用工具返回的数据，绝不编造人名、数字、时间点。"
    "2. 工具返回 error 时，不要猜测内容，直接说明该信息暂缺及原因。"
    "3. 找不到历史沟通记录时，明确写「无历史记录（首次接触或未存档）」，"
    "不要虚构过往会议。"
    "4. 输出简洁、信息密度高，顾问要在会前 3 分钟读完。"
)


# ============ 数据采集 ============

def collect_data(client, industry, topic, client_llm, trace_progress=True):
    """
    第一步：直接把三个工具各调一次，把原始数据拉齐。

    为什么不靠 LLM 自己去调工具？
    因为简报的**数据需求是固定的**（档案/趋势/历史各一份），
    让 LLM 决定调什么，会出现「少查一个」或「重复查」的情况，
    而这三个数据缺任何一个简报都不完整。
    确定性的流程就用确定性代码，把 LLM 留给「怎么写」。

    但注意：这里仍然保留 LLM 自主调工具的版本（见 run_agentic），
    两者的差别在报告里说明了。
    """
    p = (lambda s: print(f"  · {s}", flush=True)) if trace_progress \
        else (lambda s: None)

    data = {}

    p("查客户档案…")
    r = run_prep_tool("query_customers", {"industry": industry, "limit": 50})
    customers = r.get("customers", []) if "error" not in r else []
    # 找到目标客户本身
    target = next(
        (c for c in customers
         if client in c.get("name", "") or c.get("name", "") in client
         or client in c.get("name", "")),
        None,
    )
    data["profile_error"] = r.get("error")
    data["customer"] = target
    data["peers"] = [c for c in customers if not target or c is not target]
    p(f"命中 {len(customers)} 家，识别出目标客户："
      f"{target['name'] if target else '（档案中未找到）'}")

    p("查行业趋势…")
    r = run_prep_tool("search_industry_news", {"industry": industry, "days": 30})
    data["news_error"] = r.get("error")
    data["news"] = r.get("news", [])
    p(f"近 30 天 {data['news'].__len__()} 条行业动态")

    p("查历史沟通…")
    r = run_prep_tool("get_meeting_history", {"client_name": client})
    data["history_error"] = r.get("error")
    data["history"] = r
    p(f"历史记录 {r.get('count', 0)} 条"
      + ("（首次接触）" if not r.get("has_history") else ""))

    return data


# ============ 简报生成 ============

BRIEF_TEMPLATE = """# {client} · {topic} 会前简报

> 生成时间：{today} ｜ 数据来源：CRM 系统 + 行业情报库 + 长期记忆

## 一、客户档案

{profile_block}

## 二、行业趋势（近 30 天）

{news_block}

## 三、历史沟通

{history_block}

## 四、本次沟通建议

{suggestion_block}

---
<sub>本简报由 AI 生成，仅供会前参考。带 ⚠ 的部分表示数据缺失，请人工核实。</sub>
"""


def _block(items, fmt, empty_note):
    """把数据渲染成 markdown 列表，空时给出明确说明"""
    if not items:
        return f"⚠ {empty_note}"
    return "\n".join(fmt(x) for x in items)


def build_blocks(data, client, industry, topic):
    """构造四个段落的markdown。模板由代码控制，保证格式稳定"""

    # --- 档案 ---
    c = data.get("customer")
    if c:
        profile = (f"**{c['name']}**（{c['id']}）\n\n"
                   f"| 项目 | 内容 |\n|---|---|\n"
                   f"| 行业 | {c.get('industry', '—')} |\n"
                   f"| 城市 | {c.get('city', '—')} |\n"
                   f"| 联系人 | {c.get('contact', '—')} |\n"
                   f"| 邮箱 | {c.get('email', '—')} |\n")
        if data.get("peers"):
            profile += (f"\n**同行业参考客户**（{len(data['peers'])} 家）："
                        + "、".join(x["name"] for x in data["peers"][:4]))
    else:
        profile = (f"⚠ 档案库中未找到「{client}」。"
                   + (f"（检索错误：{data.get('profile_error')}）"
                      if data.get("profile_error") else "")
                   + "请人工核对客户名称，或确认该客户是否已建档。")

    # --- 行业趋势 ---
    news = data.get("news") or []
    news_block = _block(
        news,
        lambda n: (f"- **{n['title']}**（{n['date']}，影响：{n['impact']}）\n"
                   f"  {n['summary']}　<sub>来源：{n['source']}</sub>"),
        f"未获取到{industry}行业近 30 天的动态情报。"
        + (f"（原因：{data.get('news_error')}）" if data.get("news_error") else "")
    )

    # --- 历史沟通 ---
    h = data.get("history") or {}
    if not h.get("has_history"):
        hist = ("⚠ **无历史记录**（首次接触或过往沟通未存档）\n\n"
                f"> {h.get('message', '')}\n\n"
                "**建议**：本次会议需覆盖基础情况介绍，"
                "并主动确认对方的现状与痛点。")
    else:
        parts = [f"记忆库中命中 {h['count']} 条相关记录。"]
        for label, key in [("过往会议", "meetings"),
                           ("已承诺事项", "commitments"),
                           ("遗留问题", "open_issues"),
                           ("客户关注点", "focus"),
                           ("其他记录", "others")]:
            if h.get(key):
                parts.append(f"\n**{label}**：")
                parts.extend(f"- {m['content']}" for m in h[key])
        hist = "\n".join(parts)

    return profile, news_block, hist


def generate_suggestion(client, industry, topic, data, client_llm):
    """
    第四段「沟通建议」交给 LLM —— 这是唯一需要主观判断的部分。

    把已采集的数据作为事实基础喂给它，并明确列出「哪些是缺失的」，
    让它在缺失的前提下给建议，而不是假装信息完整。
    """
    facts = {
        "客户": data.get("customer"),
        "行业": industry,
        "议题": topic,
        "行业动态": [{"日期": n["date"], "标题": n["title"],
                     "影响": n["impact"], "摘要": n["summary"]}
                    for n in (data.get("news") or [])],
        "历史沟通": (data.get("history") or {}).get("all_memories"),
        "数据缺失项": [k for k in ("profile_error", "news_error", "history_error")
                       if data.get(k)],
        "是否首次接触": not (data.get("history") or {}).get("has_history", False),
    }

    prompt = (
        f"客户：{client}\n行业：{industry}\n本次议题：{topic}\n\n"
        f"已采集的事实（JSON，只以此为准）：\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
        f"请写一份会前沟通建议，要求：\n"
        f"1. 3-5 条要点，每条一行，以「- 」开头\n"
        f"2. 必须包含：开场切入建议、本次要确认的问题、需要带的数据或材料\n"
        f"3. 若无历史记录，第一条要说明这是首次深度沟通，应侧重建立信任与摸清现状\n"
        f"4. 不要写「建议进一步了解」这类空话，每条都要具体可执行\n"
        f"5. 只用上面事实里有的信息，不要编造客户背景"
    )

    try:
        r = client_llm.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        return f"⚠ 建议生成失败（{type(e).__name__}: {e}），请人工补充。"


# ============ LLM 自主调工具的版本（对比用）============

def run_agentic(client, industry, topic, client_llm, verbose=True):
    """
    另一种做法：让 LLM 自己决定调哪些工具、调几次。

    保留它是为了做对比——同样的任务，两种范式的实际差别：
      collect_data 版：数据必齐、格式稳定、token 可控，但流程写死在代码里
      run_agentic 版：灵活、能处理意外需求，但可能漏查、可能重复查

    生产环境建议用 collect_data 版。agentic 版适合「简报要素不固定」
    的场景（比如要按客户类型调整采集项）。
    """
    question = (
        f"我准备和「{client}」开会，议题是「{topic}」，"
        f"该公司属于{industry}行业。"
        f"请查询所需信息，然后写一份会前简报。"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    rounds = 0
    for _ in range(MAX_ROUNDS):
        resp = client_llm.chat.completions.create(
            model=MODEL, messages=messages,
            tools=ALL_PREP_TOOLS, tool_choice="auto", max_tokens=2000,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", rounds
        rounds += 1
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            if verbose:
                print(f"  · LLM 调用 {name}({args})")
            result = run_prep_tool(name, args)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
    return "（达到最大轮次未完成）", rounds


# ============ 主流程 ============

def main():
    ap = argparse.ArgumentParser(
        description="会前简报 Agent：输入客户/行业/议题，输出一页 markdown 简报")
    ap.add_argument("--client", required=True, help="客户名称，如 招商银行")
    ap.add_argument("--industry", required=True, help="行业，如 金融")
    ap.add_argument("--topic", required=True, help="本次议题，如 数据中台建设")
    ap.add_argument("--no-stream", action="store_true",
                    help="关闭进度输出（脚本内被调用时用）")
    ap.add_argument("--mode", choices=["template", "agentic"], default="template",
                    help="template=代码控流程(默认)；agentic=LLM 自主调工具")
    ap.add_argument("--out", help="自定义输出路径")
    args = ap.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    client_llm = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                        base_url=BASE_URL)
    verbose = not args.no_stream

    print("=" * 66)
    print("  会前简报 Agent")
    print("=" * 66)
    print(f"  客户：{args.client}")
    print(f"  行业：{args.industry}")
    print(f"  议题：{args.topic}")
    print(f"  模式：{args.mode}")

    t0 = time.perf_counter()
    if verbose:
        print("\n[1/2] 采集数据")

    data = collect_data(args.client, args.industry, args.topic,
                        client_llm, trace_progress=verbose)

    if verbose:
        print("\n[2/2] 生成简报")

    if args.mode == "agentic":
        content, rounds = run_agentic(args.client, args.industry, args.topic,
                                      client_llm, verbose=verbose)
        dt = time.perf_counter() - t0
        path = save(content, args, rounds=rounds, elapsed=dt)
        print("\n" + "=" * 66)
        print(content)
        print("=" * 66)
        print(f"\n✓ 已保存：{path}（agentic 模式，{rounds} 轮工具调用，"
              f"{dt:.1f}s）")
        return

    # template 模式：模板 + LLM 只填第四段
    profile, news_block, hist = build_blocks(
        data, args.client, args.industry, args.topic)

    suggestion = generate_suggestion(
        args.client, args.industry, args.topic, data, client_llm)

    md = BRIEF_TEMPLATE.format(
        client=args.client,
        topic=args.topic,
        today=date.today().isoformat(),
        profile_block=profile,
        news_block=news_block,
        history_block=hist,
        suggestion_block=suggestion or "⚠ 未生成",
    )

    dt = time.perf_counter() - t0
    path = save(md, args, elapsed=dt)

    print("\n" + "=" * 66)
    print(md)
    print("=" * 66)
    print(f"\n✓ 已保存：{path}（{dt:.1f}s）")


def save(content, args, rounds=None, elapsed=0.0):
    """保存简报。文件名带日期，避免多次运行互相覆盖"""
    os.makedirs(BRIEFING_DIR, exist_ok=True)
    if args.out:
        path = args.out
    else:
        safe = args.client.replace("/", "_").strip()
        path = os.path.join(BRIEFING_DIR,
                            f"{safe}_{date.today().isoformat()}.md")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


if __name__ == "__main__":
    main()