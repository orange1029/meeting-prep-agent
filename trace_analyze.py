"""
trace_analyze.py — trace 文件分析工具

trace 记录下来是为了什么？为了回答这几个问题：
  · 这次回答慢在哪？是 LLM 慢还是工具慢？
  · 调了几次工具？参数填对了吗？有没有反复试错？
  · token 花了多少？多轮之间的 token 是累加的还是每次全量重发？

用法：
  python trace_analyze.py                    # 分析最新的 trace 文件
  python trace_analyze.py trace_xxx.jsonl   # 分析指定文件
  python trace_analyze.py --raw             # 同时打印原始 JSON
"""

import glob
import json
import os
import sys

from trace_logger import read_trace


def find_latest():
    files = glob.glob("trace_*.jsonl")
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def analyze(path):
    recs = read_trace(path)
    if not recs:
        print("空 trace 文件")
        return

    print("=" * 66)
    print(f"  Trace 分析 | {os.path.basename(path)}")
    print("=" * 66)
    print(f"  session_id : {recs[0]['session_id']}")
    print(f"  记录数     : {len(recs)}")

    reqs = [r for r in recs if r["event_type"] == "llm_request"]
    resps = [r for r in recs if r["event_type"] == "llm_response"]
    tools = [r for r in recs if r["event_type"] == "tool_call"]

    # ---- 时间分布 ----
    print(f"\n【时间分布】")
    llm_ms = sum(r.get("duration_ms", 0) for r in resps)
    tool_ms = sum(r.get("duration_ms", 0) for r in tools)
    total = recs[-1].get("elapsed_ms", 0)
    print(f"  总耗时     : {total:.0f}ms")
    print(f"  LLM 累计   : {llm_ms:.0f}ms（{len(resps)} 次）"
          + (f" | 平均 {llm_ms / len(resps):.0f}ms" if resps else ""))
    print(f"  工具累计   : {tool_ms:.0f}ms（{len(tools)} 次）"
          + (f" | 平均 {tool_ms / len(tools):.0f}ms" if tools else ""))
    if total:
        pct = f"LLM 占 {llm_ms / total * 100:.0f}%"
        print(f"  瓶颈       : {pct}")

    # ---- 轮次结构 ----
    print(f"\n【调用结构】")
    print(f"  LLM 轮次   : {len(reqs)}")
    print(f"  工具调用   : {len(tools)}")
    for t in tools:
        flag = "" if t.get("ok") else "← 失败"
        print(f"    第{t.get('round')}轮 {t.get('name')}"
              f"  {t.get('duration_ms', 0):.0f}ms"
              f"  参数={t.get('args_summary', '')[:52]}{flag}")

    # ---- token ----
    print(f"\n【Token 用量】")
    pt = sum(r.get("prompt_tokens", 0) for r in resps)
    ct = sum(r.get("completion_tokens", 0) for r in resps)
    print(f"  输入合计    : {pt}")
    print(f"  输出合计    : {ct}")
    print(f"  总计        : {pt + ct}")
    if resps:
        print(f"  单轮平均    : {(pt + ct) / len(resps):.0f}")
        # 关键观察：多轮场景下，每轮都重发全部历史，
        # 所以 token 是累加的，成本随轮数平方增长
        if len(reqs) > 1:
            counts = [r.get("prompt_tokens", 0) for r in resps]
            print(f"  逐轮输入token: {counts}")
            if counts[-1] > counts[0]:
                print(f"  ⚠ 末轮输入({counts[-1]}) > 首轮({counts[0]})，"
                      f"说明历史消息被重发。轮数越多成本涨得越快，"
                      f"可用 prompt caching 缓解")
    cost = (pt * 0.14 + ct * 0.28) / 1_000_000
    print(f"  成本估算    : ${cost:.6f}")

    # ---- 异常 ----
    errs = [r for r in recs if r["event_type"] == "error"]
    failed = [r for r in tools if not r.get("ok", True)]
    if errs or failed:
        print(f"\n【异常】")
        for e in errs:
            print(f"  ✗ {e.get('error_type')}: {str(e.get('error'))[:60]}")
        for f in failed:
            print(f"  ✗ 工具 {f.get('name')} 返回了 error")
    else:
        print(f"\n【异常】无")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else find_latest()
    if not path:
        print("✗ 找不到 trace 文件（先跑一次 mcp_client_demo.py）")
        sys.exit(1)
    analyze(path)
    if "--raw" in sys.argv:
        print("\n" + "=" * 66)
        print("原始记录：")
        print("=" * 66)
        for r in read_trace(path):
            print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()