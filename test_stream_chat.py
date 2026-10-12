"""
test_stream_chat.py — run_chat.py 的关键机制测试

重点测流式下最容易出错的地方：
  1. tool_calls 分片累积（index 分组 + arguments 拼接）
  2. 长参数完整性（被切成几十段也要能拼回合法 JSON）
  3. reasoning_content（思考模式）收集与回传
  4. assistant 消息结构正确（content=None 时也要能被 API 接受）
  5. 多轮对话历史配对完整（tool_call_id 与 tool 消息必须对应）

这些是纯逻辑 + 少量真实 API 调用。跑法：
    python test_stream_chat.py
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from run_chat import (
    SYSTEM_PROMPT,
    TOOLS,
    _accumulate_tool_calls,
    handle_turn,
    stream_one_round,
)
from tools_impl import run_tool

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
RESULTS = {"pass": 0, "fail": 0}


def check(label, cond, detail=""):
    ok = bool(cond)
    RESULTS["pass" if ok else "fail"] += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail and not ok else ""))
    return ok


# ============ 纯逻辑测试：不需要 API ============

def test_accumulate_synthetic():
    """用构造的假 chunk 测累积逻辑，不花钱也不会因模型随机性失败"""
    print("=" * 60)
    print("测试 1：tool_calls 分片累积（构造数据，不调 API）")

    class FakeDelta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class FakeChoice:
        def __init__(self, delta):
            self.delta = delta

    class FakeChunk:
        def __init__(self, delta):
            self.choices = [FakeChoice(delta)]

    class FakeTc:
        def __init__(self, idx, tid=None, name=None, args=None):
            self.index = idx
            self.id = tid
            class F:
                pass
            self.function = F()
            self.function.name = name
            self.function.arguments = args

    # 单个工具调用，参数被切成 4 段（用 dumps 生成，避免手写转义出错）
    raw = json.dumps({"city": "上海", "limit": 10}, ensure_ascii=False)
    n = len(raw)
    cuts = [0, n // 3, 2 * n // 3, n]
    chunks = [
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, "call_1", "query_deals", raw[cuts[0]:cuts[1]])])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, None, None, raw[cuts[1]:cuts[2]])])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, None, None, raw[cuts[2]:cuts[3]])])),
    ]
    calls = _accumulate_tool_calls(chunks)
    check("单个工具调用累积成功", len(calls) == 1)
    if calls:
        check("id 正确", calls[0]["id"] == "call_1")
        check("name 正确", calls[0]["function"]["name"] == "query_deals")
        args = calls[0]["function"]["arguments"]
        check("arguments 拼接完整", args == raw, f"实际: {args}")
        check("拼接后可JSON 解析", json.loads(args)["city"] == "上海")

    # name 只在第一段出现（API 行为），后续段 id/name/arguments 均为 None
    chunks = [
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, "call_x", "calc_sum")])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, None, None, '{"amounts":[1]}')])),
    ]
    calls = _accumulate_tool_calls(chunks)
    check("name 仅首段出现也能累积", len(calls) == 1 and
          calls[0]["function"]["name"] == "calc_sum")

    # 两个工具调用交错到达（并行工具调用场景）。
    # 用 json.dumps 生成真实 JSON 再切片，模拟 API 的分片方式，
    # 避免手写转义出错
    raw_sum = json.dumps({"amounts": [100, 200]}, ensure_ascii=False)
    raw_deals = json.dumps({"city": "上海", "limit": 10}, ensure_ascii=False)
    chunks = [
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, "c0", "calc_sum", raw_sum[:12])])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(1, "c1", "query_deals", raw_deals[:14])])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(0, None, None, raw_sum[12:])])),
        FakeChunk(FakeDelta(tool_calls=[FakeTc(1, None, None, raw_deals[14:])])),
    ]
    calls = _accumulate_tool_calls(chunks)
    check("并行工具调用累积出 2 个", len(calls) == 2)
    check("按 index 排序（0 在前）", calls[0]["function"]["name"] == "calc_sum",
          f"实际: {calls[0]['function']['name']}")
    check("两个都能解析", json.loads(calls[1]["function"]["arguments"])["city"] == "上海")

    # 空输入
    check("无 tool_calls 时返回空列表", _accumulate_tool_calls([]) == [])
    check("只有文本 chunk 返回空列表",
          _accumulate_tool_calls([FakeChunk(FakeDelta(content="你好"))]) == [])

    # 没有 name 的异常分块应被跳过，不能产生垃圾数据
    calls = _accumulate_tool_calls([FakeChunk(FakeDelta(tool_calls=[FakeTc(0, None, None, '{}')]))])
    check("缺 name 的分块被跳过", len(calls) == 0)

    # arguments 为空时兜底成 {}
    calls = _accumulate_tool_calls([FakeChunk(FakeDelta(tool_calls=[FakeTc(0, "c0", "calc_sum", None)]))])
    check("空 arguments 兜底为 {}", calls and calls[0]["function"]["arguments"] == "{}")


# ============ 真实 API 测试 ============

def test_live(client):
    print("=" * 60)
    print("测试 2：真实 API —— 长参数流式拼接")
    chunks = list(client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content":
                   "给 wangwu@example.com 发封邮件，正文写：" +
                   "这是一段很长的测试正文用来触发流式参数分片。" * 8}],
        tools=TOOLS, tool_choice="auto", stream=True))
    calls = _accumulate_tool_calls(chunks)
    check("累积到 1 个工具调用", len(calls) == 1)
    if calls:
        raw = calls[0]["function"]["arguments"]
        check("参数较长（被切成多段）", len(raw) > 100, f"实际长度 {len(raw)}")
        try:
            parsed = json.loads(raw)
            check("长参数拼接后 JSON 合法", isinstance(parsed, dict))
            check("to 字段正确", parsed.get("to") == "wangwu@example.com")
            check("body 内容完整", len(parsed.get("body", "")) > 100)
        except json.JSONDecodeError as e:
            check("长参数拼接后 JSON 合法", False, str(e))

    print("=" * 60)
    print("测试 3：reasoning_content 收集")
    text, tc, reasoning = stream_one_round(
        client,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": "你好"}],
        tools_enabled=False,
    )
    check("拿到文本内容", len(text) > 0)
    # 注意：不能断言 reasoning_content 必须是 None。
    # 模型是否返回思维链是概率行为（实测同一句话有时返回有时不返回），
    # 这里只验证「拿到了字符串或 None」这个类型契约，
    # 以及非思考模式下它不会被误当成正文打印。
    check("reasoning_content 类型正确（str 或 None）",
          reasoning is None or isinstance(reasoning, str),
          f"实际类型: {type(reasoning).__name__}")
    check("思维链内容未混入正文", reasoning is None or reasoning not in text)

    print("=" * 60)
    print("测试 4：多轮对话 + 工具循环历史完整性")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    handle_turn(client, messages, "查一下上海的教育客户", stream=True, verbose=False)
    check("第一轮后历史已增长", len(messages) > 2)

    # 校验 assistant.tool_calls 与 role=tool 消息必须 id 一一对应
    pairs_ok = True
    pending = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            pending = [tc["id"] for tc in m["tool_calls"]]
        elif m["role"] == "tool":
            if not pending or m["tool_call_id"] != pending[0]:
                pairs_ok = False
            if pending:
                pending.pop(0)
    check("tool_call_id 与 tool 消息配对正确", pairs_ok)

    # 第二轮：追问，验证上下文延续
    before = len(messages)
    handle_turn(client, messages, "那北京的教育客户呢？", stream=True, verbose=False)
    check("追问后历史继续增长", len(messages) > before)

    # assistant 消息里不能有非法的 content=None + 无 tool_calls
    bad = [m for m in messages
           if m["role"] == "assistant"
           and m.get("content") is None
           and not m.get("tool_calls")]
    check("不存在 content=None 且无 tool_calls 的 assistant 消息", not bad)


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY")
        sys.exit(1)

    test_accumulate_synthetic()

    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    test_live(client)

    print("=" * 60)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"结果：{RESULTS['pass']}/{total} 通过")
    if RESULTS["fail"]:
        print("✗ 有用例失败")
        sys.exit(1)
    print("✓ 流式与多轮机制全部正常")


if __name__ == "__main__":
    main()