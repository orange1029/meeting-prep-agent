"""
run_chat_with_memory.py — 带长期记忆的多轮对话 Agent（智能体）

在 run_chat.py 之上加了长期记忆（long-term memory）：
  · 会话结束退出 → 下次重开，Agent 仍记得你说过的事
  · 每轮自动召回相关记忆，注入系统提示（system prompt）
  · 模型可主动调 save_memory / recall_memory 两个工具

记忆的三层结构（重要，理解了这个就不会乱）：
  第1 层 短期：messages（对话历史）—— 本次进程内，退出即失
  第2 层 长期：memory/memories.json —— 跨会话持久化，本文件的主角
  第3 层 原始数据：tools_impl.py 的 mock 数据 —— 事实来源，记忆不能替代它

一个重要原则：**记忆是线索，不是事实来源**
  真实数据永远从工具查。记忆只用来提供「用户偏好」「上下文提示」。
  如果记忆与工具查询结果冲突，以工具结果为准——这条写进了系统提示。

运行：
    python run_chat_with_memory.py
    python run_chat_with_memory.py --no-stream
    python run_chat_with_memory.py "记住：我偏好结论先行"    # 带初始问题进入
    python run_chat_with_memory.py --no-memory              # 临时关掉记忆做对比
"""

import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

from memory_store import MemoryStore
from memory_tools import (
    MEMORY_REGISTRY,
    MEMORY_TOOLS,
    get_store,
    recall_memory_impl,
    save_memory_impl,
)
from run_chat import C, _brief, _no_color, c
from tools_impl import run_tool
from tools_schema import TOOLS

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MAX_ROUNDS = 8
COLOR_OK = bool(_no_color())

# 业务工具 + 记忆工具，合成完整工具集
ALL_TOOLS = TOOLS + MEMORY_TOOLS

# 记忆工具合并进统一分发器
BASE_REGISTRY = None  # 延迟导入 tools_impl 的 run_tool 内部表


def _dispatch(name, arguments):
    """
    统一分发：记忆工具走自己的实现，其余走 tools_impl.run_tool。
    这样不用改 tools_impl，也不用把记忆注册进业务表。
    """
    if name in MEMORY_REGISTRY:
        return MEMORY_REGISTRY[name](**arguments)
    return run_tool(name, arguments)


SYSTEM_BASE = (
    "你是一个 CRM（客户关系管理）数据分析助手。"
    "回答要点：先给结论，再给支撑数据，用中文回答。"
    "金额要带千位分隔符并注明单位（元）。"
    "工具返回错误时，读懂错误原因后修正参数重试，不要原样重试。"
    "注意：对话是多轮的，用户说「刚才那个」「那北京呢」时，"
    "要结合上文理解指代，不要反问已经明确的信息。"
)

MEMORY_RULES = (
    "\n【关于长期记忆】"
    "\n- 系统提示中若出现「你此前记住的信息」，那是跨会话的长期记忆，"
    "请在回答中自然地用上它。"
    "\n- 记忆只是线索，不是事实来源。真实数据必须用工具查。"
    "若记忆与工具查询结果冲突，以工具结果为准。"
    "\n- 当用户明确说「记住」「以后都要」，或你确认了某个跨对话有用的"
    "客观事实（联系人、邮箱、金额约定、口径偏好），用 save_memory 记下。"
    "\n- 当用户说「上次」「之前」「我跟你说过的」，用 recall_memory 主动检索；"
    "若系统提示里已有相关记忆，直接使用即可，不必重复调用。"
    "\n- 不要记闲聊内容，不要把一次性的查询结果当长期记忆存。"
)


def build_system_prompt(recalled_text=""):
    """把召回的记忆拼进系统提示。注意必须在召回之后才调用"""
    prompt = SYSTEM_BASE + MEMORY_RULES
    if recalled_text:
        prompt += "\n\n" + recalled_text
    return prompt


# ============ 流式单轮（与 run_chat.py 同逻辑，改为用 _dispatch）============


def _accumulate_tool_calls(chunks):
    """把流式 chunk 拼成完整 tool_calls（按 index 分组，arguments 拼接）"""
    by_index = {}
    for ck in chunks:
        for tc in (ck.choices[0].delta.tool_calls or []):
            slot = by_index.setdefault(
                tc.index, {"id": None, "name": None, "arguments": ""}
            )
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
    result = []
    for idx in sorted(by_index):
        slot = by_index[idx]
        if not slot["name"]:
            continue
        result.append({
            "id": slot["id"] or f"call_{idx}",
            "type": "function",
            "function": {
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            },
        })
    return result


def stream_round(client, messages):
    """流式跑一轮，返回 (文本, tool_calls, reasoning_content)"""
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "tools": ALL_TOOLS,
        "tool_choice": "auto",
    }
    t0 = time.perf_counter()
    first_at = None
    parts, tool_chunks, reasoning = [], [], []

    for ck in client.chat.completions.create(**kwargs):
        delta = ck.choices[0].delta
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reasoning.append(rc)
        if delta.tool_calls:
            tool_chunks.append(ck)
            continue
        if delta.content:
            if first_at is None:
                first_at = time.perf_counter() - t0
                sys.stdout.write(c("  ·首字", C.DIM) + f" {first_at:.2f}s\n")
            sys.stdout.write(delta.content)
            sys.stdout.flush()
            parts.append(delta.content)

    sys.stdout.write("\n")
    return ("".join(parts), _accumulate_tool_calls(tool_chunks),
            "".join(reasoning) or None)


def nonstream_round(client, messages):
    """非流式跑一轮（调试用）"""
    resp = client.chat.completions.create(
        model=MODEL, messages=messages, tools=ALL_TOOLS, tool_choice="auto"
    )
    msg = resp.choices[0].message
    calls = [{
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.function.name,
                     "arguments": tc.function.arguments},
    } for tc in (msg.tool_calls or [])]
    if msg.content:
        print(msg.content)
    return msg.content or "", calls, getattr(msg, "reasoning_content", None)


# ============ 摘要工具结果 ============


def summarize(result, name):
    if "error" in result:
        return result["error"][:80]
    if name == "save_memory":
        return f"{result['message']}（库中共 {result['total_memories']} 条）"
    if name == "recall_memory":
        if not result["found"]:
            return "未找到相关记忆"
        return f"召回 {result['found']} 条：" + \
               "；".join(m["content"][:24] for m in result["memories"][:2])
    if "customers" in result:
        return f"命中 {result['total']} 个客户"
    if "deals" in result:
        return f"{result['total']} 笔成交，合计 {result['amount_sum']:,.0f} 元"
    if "sum" in result:
        return f"总和 {result['sum']:,.2f}"
    if "sent" in result:
        return f"已模拟发送 → {result['to']}"
    return json.dumps(result, ensure_ascii=False, default=str)[:70]


# ============ 单轮处理（含记忆召回）============


def handle_turn(client, messages, user_input, store, stream=True,
                use_memory=True, verbose=True):
    """
    处理一轮用户输入。
    注意流程顺序：**先召回记忆，再构造 system prompt**——
    顺序反了记忆就注入不进本轮上下文。
    """
    recalled_text = ""
    recalled_n = 0
    if use_memory:
        # 自动召回：把本轮提问作为检索 query。
        # 只注入「强相关」记忆——弱相关留给模型主动调 recall_memory 去捞，
        # 自动塞进上下文只会干扰模型判断。
        hits = store.retrieve(user_input, top_k=5)
        recalled_text = store.render_for_prompt(hits, only_strong=True)
        strong = sum(1 for h in hits if h["relevance"] == "strong")
        recalled_n = strong
        if verbose and recalled_text:
            preview = "；".join(
                h["content"][:20] for h in hits if h["relevance"] == "strong"
            )
            c(f"  🧠 召回 {strong} 条强相关记忆：{preview}", C.DIM)
        elif verbose and hits:
            c(f"  🧠 有 {len(hits)} 条弱相关记忆未注入（如需可让模型主动检索）", C.DIM)

    # 重建 messages：[0] 是 system，必须放在最前面
    messages.append({"role": "user", "content": user_input})
    sys_prompt = build_system_prompt(recalled_text)
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = sys_prompt
    else:
        messages.insert(0, {"role": "system", "content": sys_prompt})

    stats = {"rounds": 0, "tools": [], "recalled": recalled_n,
             "saved": 0, "elapsed": 0.0}
    t_turn = time.perf_counter()

    for rnd in range(1, MAX_ROUNDS + 1):
        stats["rounds"] = rnd
        if verbose:
            c(f"  ── 第 {rnd} 轮推理", C.DIM)

        if stream:
            text, calls, reasoning = stream_round(client, messages)
        else:
            text, calls, reasoning = nonstream_round(client, messages)

        if not calls:
            if not text.strip() and verbose:
                c("  (模型返回空内容)", C.YELLOW)
            break

        assistant = {"role": "assistant", "content": text or None,
                     "tool_calls": calls}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        messages.append(assistant)

        for call in calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            # 参数可能是 JSON 字符串
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError as e:
                    result = {"error": f"参数不是合法 JSON：{e}"}
                    args = {}
            elif not isinstance(args, dict):
                result = {"error": "参数类型错误，应为对象"}
                args = {}

            result = _dispatch(name, args)
            stats["tools"].append((name, result))
            if name == "save_memory" and result.get("saved"):
                stats["saved"] += 1

            ok = "error" not in result
            if verbose:
                head = c(" ⚙", C.CYAN) + f" {c(name, C.BOLD)}({_brief(call['function']['arguments'])})"
                print(head + "  " + c(summarize(result, name),
                                       C.GREEN if ok else C.RED))

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    stats["elapsed"] = time.perf_counter() - t_turn
    return stats


# ============ 交互循环 ============

HELP = """
可用命令（斜杠开头）：
  /help      显示本帮助
  /memory    查看长期记忆统计
  /list      列出全部记忆
  /recall <关键词>   手动检索记忆
  /remember <内容>   手动记一条（默认 type=fact）
  /forget <ID>       按编号遗忘一条；不带 ID 则交互确认清空
  /forget-decay      执行衰减清理，删除长期未命中的记忆
  /reset      清空对话历史（注意：不删长期记忆）
  /emails     查看本次会话模拟发送过的邮件
  /exit退出
"""


def print_banner(stream, use_memory, store):
    print(c("═" * 64, C.CYAN))
    print(c("  CRM 数据分析 Agent  ·  多轮对话 + 流式 + 长期记忆", C.BOLD))
    print(c("═" * 64, C.CYAN))
    print(f"  model（模型）   : {MODEL}")
    print(f"  base_url（接口）: {BASE_URL}")
    print(f"  工具: {', '.join(t['function']['name'] for t in ALL_TOOLS)}")
    print(f"  模式: {'流式' if stream else '非流式'}"
          f" | 记忆: {'开启' if use_memory else '关闭'}")
    print(f"  记忆库: {store.path}（{len(store.memories)} 条）")
    print(c("  /help 查看命令，/exit 退出", C.DIM))


def main():
    argv = sys.argv[1:]
    stream = "--no-stream" not in argv
    use_memory = "--no-memory" not in argv
    initial = [a for a in argv if not a.startswith("--")]

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    store = get_store()
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL
    )

    messages = [{"role": "system", "content": build_system_prompt()}]
    all_tools = []
    total_saved = 0
    total_recalled = 0

    print_banner(stream, use_memory, store)

    from tools_impl import SENT_EMAILS

    for q in initial:
        print(f"\n{c('你', C.BOLD)}> {q}")
        st = handle_turn(client, messages, q, store, stream=stream,
                         use_memory=use_memory)
        all_tools.extend(st["tools"])
        total_saved += st["saved"]
        total_recalled += st["recalled"]
        print(c(f"  ({st['rounds']} 轮 / {st['elapsed']:.1f}s)", C.DIM))

    if initial:
        print()

    while True:
        try:
            q = input(c("\n你", C.BOLD) + "> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not q:
            continue

        if q.startswith("/"):
            parts = q[1:].split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("exit", "quit", "q"):
                break
            elif cmd == "help":
                print(HELP)
            elif cmd == "memory":
                s = store.stats()
                print(f"  记忆总数：{s['total']}")
                print(f"  按类型：{s['by_type'] or '（空）'}")
                print(f"  从未被召回：{s['never_hit']} 条")
                if s["top_hits"]:
                    print("  命中最多：")
                    for mid, hits, txt in s["top_hits"]:
                        print(f"    {mid} ({hits} 次) {txt}...")
            elif cmd == "list":
                if not store.memories:
                    print("  （记忆库为空）")
                for m in store.memories:
                    hit = f"命中{m['hit_count']}次" if m.get("hit_count") else "未命中"
                    print(f"  {m['id']} [{m['type']}] {m['content']}  ({hit})")
            elif cmd == "recall":
                if not arg:
                    print("  用法：/recall 张伟")
                else:
                    hits = store.retrieve(arg, top_k=10)
                    if not hits:
                        print("  未找到相关记忆")
                    for h in hits:
                        print(f"  {h['id']} ({h['score']}) {h['content']}")
            elif cmd == "remember":
                if not arg:
                    print("  用法：/remember 我偏好结论先行")
                else:
                    ok, msg = store.add(arg, "fact")
                    print(f"  {'✓' if ok else '✗'} {msg}")
            elif cmd == "forget":
                if not arg:
                    n = len(store.memories)
                    ans = input(f"  将清空全部 {n} 条记忆，确认？(yes/no) ").strip()
                    if ans.lower() == "yes":
                        store.memories = []
                        store._save()
                        print("  已清空全部记忆")
                    else:
                        print("  已取消")
                else:
                    n = store.forget(mem_id=arg)
                    print(f"  {'✓' if n else '✗'} 已遗忘 {n} 条")
            elif cmd == "forget-decay":
                n = store.decay_and_forget()
                print(f"  清理了 {n} 条长期未命中的记忆，"
                      f"剩余 {len(store.memories)} 条")
            elif cmd == "reset":
                messages = [{"role": "system", "content": build_system_prompt()}]
                print("  已清空对话历史（长期记忆保留）")
            elif cmd == "emails":
                if not SENT_EMAILS:
                    print("  （本次会话未发送邮件）")
                for m in SENT_EMAILS:
                    print(f"  {m['message_id']} → {m['to']} | {m['subject']}")
            else:
                print(f"  未知命令 /{cmd}，输入 /help 查看可用命令")
            continue

        print()
        st = handle_turn(client, messages, q, store, stream=stream,
                         use_memory=use_memory)
        all_tools.extend(st["tools"])
        total_saved += st["saved"]
        total_recalled += st["recalled"]
        bits = [f"{st['rounds']} 轮", f"{st['elapsed']:.1f}s"]
        if use_memory and st["recalled"]:
            bits.append(f"召回 {st['recalled']} 条")
        if st["saved"]:
            bits.append(f"新记 {st['saved']} 条")
        print(c("  (" + " / ".join(bits) + ")", C.DIM))

    print(f"\n本次会话：工具调用 {len(all_tools)} 次"
          + (f"，召回记忆 {total_recalled} 次" if use_memory else "")
          + f"，新增记忆 {total_saved} 条")
    print(f"长期记忆库现有 {len(store.memories)} 条 → {store.path}")


if __name__ == "__main__":
    main()