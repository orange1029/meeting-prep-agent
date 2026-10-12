"""
run_chat.py — 交互式多轮对话 Agent（智能体），带流式响应

比 run_agent.py 强在哪：
  run_agent.py  一次性问答，问完就结束，看不到完整过程
  run_chat.py   保持对话历史，能追问「那北京呢」「帮我发邮件给刚才那个客户」
                + 流式逐字输出 + 工具调用可视化 + /history /reset 等命令

流式（streaming）下的技术难点（本文件最核心的部分）：
  普通模式下，模型返回的 tool_calls 是完整的一个对象。
  流式模式下，tool_calls 被拆成很多chunk（数据块），
  且**同一个工具调用的参数会被切成几十段**（delta 增量），例如：
      chunk1: index=0, id="call_abc", name="query_deals"
      chunk2: index=0, arguments='{"city":'
      chunk3: index=0, arguments='"上海"'
      ...
  必须按 index 把这些片段**拼接起来**，否则 JSON 解析必然失败。
  详见下面的 _accumulate_tool_calls。

运行：
    python run_chat.py                # 进入多轮对话
    python run_chat.py --no-stream    # 关掉流式（非流式整体返回，便于调试）
    python run_chat.py "先查上海客户"   # 带初始问题进入
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from tools_impl import SENT_EMAILS, run_tool
from tools_schema import TOOLS

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MAX_ROUNDS = 8  # 单轮内最多工具调用轮数，防死循环

SYSTEM_PROMPT = (
    "你是一个 CRM（客户关系管理）数据分析助手。"
    "回答要点：先给结论，再给支撑数据，用中文回答。"
    "金额要带千位分隔符并注明单位（元）。"
    "工具返回错误时，读懂错误原因后修正参数重试，不要原样重试。"
    "注意：对话是多轮的，用户说「刚才那个」「那北京呢」时，"
    "要结合上文理解指代，不要反问已经明确的信息。"
)

# ============ 终端着色 ============

class C:
    """ANSI 颜色码。Windows 10+ 的终端支持"""
    DIM = "\033[2m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _no_color():
    """检测终端是否支持颜色（不支持则全部置空）"""
    if os.name == "nt":
        return os.environ.get("WT_SESSION") or os.environ.get("TERM")
    return True


COLOR_OK = bool(_no_color())


def c(text, color):
    return f"{color}{text}{C.END}" if COLOR_OK else text


# ============ 流式增量累积（核心）============


def _accumulate_tool_calls(chunks):
    """
    把流式返回的 chunk 拼成完整的 tool_calls 列表。

    流式下同一个工具调用会被拆散在多个 chunk 里，且参数是增量片段。
    关键点：
      1. 按 index 分组 —— index 相同的是同一个工具调用
      2. id / name 只在第一段出现，后续段没有，要保留第一段的
      3. arguments 必须拼接后再 json.loads，单独一段是残缺 JSON
    """
    by_index = {}
    for ck in chunks:
        for tc in (ck.choices[0].delta.tool_calls or []):
            slot = by_index.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

    result = []
    for idx in sorted(by_index):  # 按 index 排序，保证顺序和模型请求一致
        slot = by_index[idx]
        if not slot["name"]:
            continue  # 没拿到工具名的异常分块，跳过
        result.append({
            "id": slot["id"] or f"call_{idx}",
            "type": "function",
            "function": {
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            },
        })
    return result


def stream_one_round(client, messages, tools_enabled=True):
    """流式跑一轮，返回 (完整文本, tool_calls 列表, reasoning_content 思考内容)"""
    import time

    kwargs = {"model": MODEL, "messages": messages, "stream": True}
    if tools_enabled:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"

    t0 = time.perf_counter()
    first_at = None
    parts = []
    tool_chunks = []
    reasoning = []  # 思考模式的思维链内容

    for ck in client.chat.completions.create(**kwargs):
        delta = ck.choices[0].delta

        # 思考模式：思维链内容也要收集。
        # DeepSeek 在思考模式下要求下一轮回传 reasoning_content，
        # 丢掉会导致 400。这里先存着，模型没显式要求时不主动附加。
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reasoning.append(rc)

        # 工具调用分片：只收集，不打印
        if delta.tool_calls:
            tool_chunks.append(ck)
            continue

        text = delta.content
        if text:
            if first_at is None:
                first_at = time.perf_counter() - t0
                # 首字到达后才打印标记，避免用户误以为卡死
                sys.stdout.write(c("  ·首字", C.DIM) + f" {first_at:.2f}s\n")
            sys.stdout.write(text)
            sys.stdout.flush()
            parts.append(text)

    sys.stdout.write("\n")
    return ("".join(parts), _accumulate_tool_calls(tool_chunks),
            "".join(reasoning) or None)


def nonstream_one_round(client, messages, tools_enabled=True):
    """非流式跑一轮（调试用，便于看完整响应结构）"""
    kwargs = {"model": MODEL, "messages": messages}
    if tools_enabled:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    calls = []
    for tc in (msg.tool_calls or []):
        calls.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name,
                         "arguments": tc.function.arguments},
        })
    text = msg.content or ""
    if text:
        print(text)
    reasoning = getattr(msg, "reasoning_content", None)
    return text, calls, reasoning


# ============ 单轮处理（含工具循环）============


def handle_turn(client, messages, user_input, stream=True, verbose=True):
    """处理一轮用户输入，messages（对话历史）就地追加，返回统计信息"""
    messages.append({"role": "user", "content": user_input})
    stats = {"rounds": 0, "tools": [], "elapsed": 0.0}

    import time
    t_turn = time.perf_counter()

    for rnd in range(1, MAX_ROUNDS + 1):
        stats["rounds"] = rnd
        if verbose:
            c(f"  ── 第 {rnd} 轮推理", C.DIM)

        if stream:
            text, calls, reasoning = stream_one_round(client, messages)
        else:
            text, calls, reasoning = nonstream_one_round(client, messages)

        # 模型不再要工具 → 它已经给出最终回答
        if not calls:
            if not text.strip() and verbose:
                c("  (模型返回空内容)", C.YELLOW)
            break

        # 把 assistant 消息（含 tool_calls）原样追加回历史。
        # 流式下要用自己拼出来的 calls，且 arguments 必须是 JSON 字符串。
        assistant = {"role": "assistant", "content": text or None,
                     "tool_calls": calls}
        if reasoning:
            # 思考模式下必须回传，否则下一轮 400。
            # 非思考模式是空字符串，此时不加这个键。
            assistant["reasoning_content"] = reasoning
        messages.append(assistant)

        # 执行工具
        for call in calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            result = run_tool(name, args)
            stats["tools"].append((name, result))

            ok = "error" not in result
            head = c(" ⚙", C.CYAN) + f" {c(name, C.BOLD)}({_brief(args)})"
            if verbose:
                print(head + "  " + c(_summarize(result), C.GREEN if ok else C.RED))

            # 工具结果回传，必须是字符串
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    stats["elapsed"] = time.perf_counter() - t_turn
    return stats


def _brief(args, limit=70):
    """压缩参数显示，过长截断"""
    s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "..."


def _summarize(result):
    """工具结果压成一行摘要"""
    if "error" in result:
        return result["error"][:80]
    if "customers" in result:
        return f"命中 {result['total']} 个客户，返回 {result['returned']} 条"
    if "deals" in result:
        return f"{result['total']} 笔成交，合计 {result['amount_sum']:,.0f} 元"
    if "sum" in result:
        return f"总和 {result['sum']:,.2f}（{result['count']} 笔）"
    if "sent" in result:
        return f"已模拟发送 {result['message_id']} → {result['to']}"
    return json.dumps(result, ensure_ascii=False, default=str)[:70]


# ============ 交互循环 ============

HELP = """
可用命令（斜杠开头）：
  /help      显示本帮助
  /history   查看当前对话历史统计
  /tools     本轮及累计工具调用记录
  /reset     清空对话历史，重新开始
  /emails    查看本次会话模拟发送过的邮件
  /exit      退出（Ctrl+C 也可以）
"""


def print_banner(stream):
    print(c("═" * 62, C.CYAN))
    print(c("  CRM 数据分析 Agent  ·  多轮对话 + 流式响应", C.BOLD))
    print(c("═" * 62, C.CYAN))
    print(f"  model（模型）  : {MODEL}")
    print(f"  base_url（接口）: {BASE_URL}")
    print(f"  工具: {', '.join(t['function']['name'] for t in TOOLS)}")
    print(f"  模式: {'流式（逐字输出）' if stream else '非流式（整体返回）'}")
    print(c("  输入 /help 查看命令，/exit 退出", C.DIM))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stream = "--no-stream" not in sys.argv

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL)

    # 多轮的核心：messages 一直保留，模型能看到之前所有对话
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    all_tools = []  # 累计工具调用记录

    print_banner(stream)

    # 命令行直接给了问题，先答了再进交互
    for q in args:
        print(f"\n{c('你', C.BOLD)}> {q}")
        st = handle_turn(client, messages, q, stream=stream)
        all_tools.extend(st["tools"])
        print(c(f"  ({st['rounds']} 轮 / {st['elapsed']:.1f}s)", C.DIM))

    if args:
        print()

    while True:
        try:
            q = input(c("\n你", C.BOLD) + "> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not q:
            continue

        # ---- 命令处理 ----
        if q.startswith("/"):
            cmd = q[1:].lower()
            if cmd in ("exit", "quit", "q"):
                break
            if cmd == "help":
                print(HELP)
            elif cmd == "history":
                roles = [m["role"] for m in messages]
                print(f"  对话消息数：{len(messages)}")
                print(f"  构成：user {roles.count('user')} 条 / "
                      f"assistant {roles.count('assistant')} 条 / "
                      f"tool {roles.count('tool')} 条")
                print(f"  累计 token 估算见 API 响应")
            elif cmd == "tools":
                if not all_tools:
                    print("  （暂无工具调用）")
                for i, (n, r) in enumerate(all_tools, 1):
                    mark = "✗" if "error" in r else "✓"
                    print(f"  {i}. {mark} {n} → {_summarize(r)}")
            elif cmd == "emails":
                if not SENT_EMAILS:
                    print("  （本次会话未发送邮件）")
                for m in SENT_EMAILS:
                    print(f"  {m['message_id']} → {m['to']} | {m['subject']}")
                    print(f"     {m['body'][:60]}...")
            elif cmd == "reset":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                all_tools.clear()
                print("  已清空对话历史。")
            else:
                print(f"  未知命令 /{cmd}，输入 /help 查看可用命令")
            continue

        # ---- 正常提问 ----
        print()  # 换行，让流式输出另起一行
        st = handle_turn(client, messages, q, stream=stream)
        all_tools.extend(st["tools"])
        print(c(f"  ({st['rounds']} 轮 / {st['elapsed']:.1f}s / "
                f"工具 {len(st['tools'])} 次)", C.DIM))

    print(f"\n本次会话共调用工具 {len(all_tools)} 次，"
          f"发送 {len(SENT_EMAILS)} 封模拟邮件。")


if __name__ == "__main__":
    main()