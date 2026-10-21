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
# 用例数据在 evals/mcp_agent_cases.yaml —— 加用例改文件，不改代码。
# 加载与校验在 eval_cases.py（独立模块，不依赖 fastmcp，
# 这样主环境也能跑配置校验测试）。

from eval_cases import (  # noqa: E402
    BASELINE_FILE,
    CASES_FILE,
    EVALS_DIR,
    load_cases,
)

CASES = load_cases()


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


BASELINE_FILE = os.path.join(EVALS_DIR, "baseline.json")


def save_baseline(summary, per_case, path=BASELINE_FILE):
    """把当前结果存成基线，供下次对比。

    基线要存**入库**（不像 eval_result.json 那样 gitignore），
    因为它的用途就是「跨次对比」——不入库就失去意义了。

    存的内容只保留评估结论，不存模型原文答案：
    原文是那一次运行的产物，不该成为基线的一部分。
    """
    data = {
        "note": "评估基线。跑 eval_mcp_agent.py --save-baseline 更新。",
        "model": summary["model"],
        "cases": summary["cases"],
        "tool_accuracy": summary["tool_accuracy"],
        "answer_accuracy": summary["answer_accuracy"],
        "pass_rate": summary["pass_rate"],
        "avg_latency": summary["avg_latency"],
        "avg_tokens": summary["avg_tokens"],
        "per_case": {
            str(cid): {"pass": ok, "tool_ok": t_ok}
            for cid, ok, t_ok in per_case
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_baseline(path=BASELINE_FILE):
    """读基线文件。没有基线返回 None（不是错误，是首次运行）"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def compare_with_baseline(summary, per_case, baseline):
    """和基线对比，返回 (回归列表, 改善列表)

    回归 = 基线里通过、这次不通过
    改善 = 基线里不通过、这次通过

    只看 pass（综合通过），不对比延迟和 token——
    那两个受网络和服务负载影响太大，当回归信号会产生噪音。
    指标变化在报告里单独列，不进回归判定。
    """
    if not baseline:
        return [], []

    base_cases = baseline.get("per_case") or {}
    regressed, improved = [], []
    for cid, ok, _t_ok in per_case:
        key = str(cid)
        if key not in base_cases:
            continue          # 新增用例，没有基线可比，跳过
        was = base_cases[key].get("pass")
        if was is True and not ok:
            regressed.append(cid)
        elif was is False and ok:
            improved.append(cid)
    return regressed, improved


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--only", type=int, help="只跑指定 id 的 case")
    parser.add_argument("--save-baseline", action="store_true",
                        help="把这次结果存成基线（改过用例或改过代码后用）")
    parser.add_argument("--no-compare", action="store_true",
                        help="跳过与基线的对比")
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

    # ============ 与基线对比 ============
    # eval_result.json 每次都变（它在 .gitignore 里，这是对的——
    # 一次运行的产物不该入库）。但那样就没法看「有没有退步」。
    # 所以另有一份入库的基线文件 evals/baseline.json，
    # 专门用来回答「这次比上次差了吗」。
    per_case = [(r["id"], r["pass"], r["tool_ok"]) for r in results]

    if args.save_baseline:
        p = save_baseline(out, per_case)
        print(f"  已更新基线：{p}")

    if not args.no_compare:
        baseline = load_baseline()
        if baseline is None:
            print("\n  尚无基线（首次运行）。"
                  "确认结果没问题后跑一次 --save-baseline 建立基线。")
        else:
            regressed, improved = compare_with_baseline(out, per_case, baseline)
            print(f"\n【与基线对比】基线模型={baseline.get('model')}，"
                  f"基线通过率={baseline.get('pass_rate', 0) * 100:.0f}%"
                  f"（{baseline.get('cases')} 个用例）")
            print(f"  本次通过率          : {pass_n / n * 100:.0f}%（{n} 个用例）")

            if regressed:
                print(f"  ⚠ 回归用例           : {regressed}")
                print("    这些用例上次通过、这次不通过。改了代码要查这里。")
            if improved:
                print(f"  ↑ 改善用例           : {improved}")
            if not regressed and not improved:
                print("  ✓ 与基线一致，无回归也无改善")

            # 指标变化单独列，不进回归判定
            # 延迟和 token 受网络与负载影响大，当回归信号会全是噪音
            for key, label in (("tool_accuracy", "工具选择正确率"),
                               ("answer_accuracy", "答案准确率"),
                               ("pass_rate", "综合通过率")):
                base_v = baseline.get(key)
                if base_v is None:
                    continue
                delta = (out[key] - base_v) * 100
                if abs(delta) >= 0.5:
                    arrow = "↑" if delta > 0 else "↓"
                    print(f"  {label:12} {base_v * 100:.0f}% → "
                          f"{out[key] * 100:.0f}%  {arrow}{abs(delta):.0f}pp")

            if regressed:
                print("\n  提示：如果是故意改坏再确认修复，可以跑 "
                      "--save-baseline 刷新基线。")


if __name__ == "__main__":
    asyncio.run(main())