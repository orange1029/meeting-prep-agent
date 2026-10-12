"""
mcp_client_demo.py — MCP Client + LLM Agent 完整闭环

演示 MCP 最核心的价值：**Agent 代码里没有任何硬编码的工具定义**。

完整链路（5 步）：
  1. Client 启动 server 子进程，走 stdio 传输
  2. Client 调用 list_tools() 拿到工具清单（含 JSON Schema）
  3. 把清单**动态转成** OpenAI function calling 格式，注入 LLM 请求
  4. LLM 决定调工具 → Client 把调用**转发给 server**
  5. server 执行 → 结果回传 LLM → LLM 写最终回答

关键点：第 3 步的转换是纯机械的。
  MCP 工具定义（FastMCP 生成的 JSON Schema）
      ↓ 自动转换
  OpenAI function calling 格式
这个转换函数是 MCP 生态最实用的部分——它让 MCP server 能直接
喂给任何支持 OpenAI function calling 的模型（Claude/GPT/DeepSeek）。

运行：
  python mcp_client_demo.py                      # 用DeepSeek
  python mcp_client_demo.py --server-url http://127.0.0.1:8000/mcp   # 连 SSE server
"""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from openai import OpenAI

# trace 接入：只 import，业务逻辑零改动
from trace_logger import (
    EVENT_LLM_REQUEST,
    EVENT_LLM_RESPONSE,
    EVENT_TOOL_CALL,
    Tracer,
    _summarize_args,
)

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
MAX_ROUNDS = 5


def mcp_tool_to_openai(tool):
    """
    MCP 工具定义 → OpenAI function calling 格式。

    这是整个 demo 最核心的 20 行。MCP 工具对象结构是：
        {"name":..., "description":..., "input_schema": {...JSON Schema...}}
    而 OpenAI 要的是：
        {"type":"function","function":{"name":...,"description":...,
                                    "parameters": {...JSON Schema...}}}
    两边都是 JSON Schema，所以只需换个壳——这就是 MCP 生态的价值所在。

    注意两个坑：
    1. 字段名在 MCP SDK v2 从 inputSchema 改成了 input_schema。
       直接读旧名会触发 FastMCPDeprecationWarning，所以两个都兼容读取。
    2. FastMCP 会往 schema 里塞 "title" 字段，部分 OpenAI 兼容服务
       遇到未知字段会报错，要清掉。
    """
    raw = getattr(tool, "input_schema", None)
    if raw is None:                      # 兼容旧版 SDK
        raw = getattr(tool, "inputSchema", None)
    schema = dict(raw or {})
    schema.pop("title", None)
    schema.pop("$schema", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


def strip_title(obj):
    """递归清掉 JSON Schema 里的 title/$schema 键"""
    if isinstance(obj, dict):
        return {k: strip_title(v) for k, v in obj.items()
                if k not in ("title", "$schema")}
    if isinstance(obj, list):
        return [strip_title(x) for x in obj]
    return obj


async def run(question, server_url=None, verbose=True, trace=True):
    client_llm = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

    # 【trace 接入点 0】建 tracer。整个函数体包在 try/finally 里，
    # 保证无论正常结束还是抛异常，trace 文件都会被 close 并写 session_end。
    # 少了这个，异常退出会留下一个「看起来没跑完」的 trace 文件。
    tracer = Tracer(output_dir=os.path.dirname(os.path.abspath(__file__)),
                    console=False) if trace else Tracer(enabled=False)
    try:
        return await _run_inner(client_llm, question, server_url, verbose,
                                trace=tracer)
    finally:
        trace_path = tracer.close()
        if trace:
            print(f"\n  trace 已写入：{os.path.basename(trace_path)}")


async def _run_inner(client_llm, question, server_url, verbose, trace):
    """真正的 Agent 逻辑。拆出来是为了让 run() 只管 trace 的生命周期，
    业务逻辑这一层可以完全无视 trace 的存在。"""

    # ---- 1. 连上 MCP server ----
    if server_url:
        # HTTP 模式。这里有个很容易踩的坑：
        # fastmcp 看到 http:// 开头的 URL 会**默认选 SSETransport（旧协议）**，
        # 而 server 跑的是 streamable-http（MCP 新规范），
        # 结果就是 client 发 SSE 请求过去 → 502 Bad Gateway。
        #
        # 解法：显式构造 StreamableHttpTransport，别让框架猜。
        transport = StreamableHttpTransport(server_url)
        connect_desc = f"streamable-http（{server_url}）"
    else:
        # stdio 模式：Client 用子进程把 server 拉起来，
        # 通过 stdin/stdout 通信。这是本地 server 最省事的方式。
        #
        # fastmcp 4.x 起**不再自动推断** transport，必须显式构造
        # StdioTransport，而且 command 和 args 是两个独立参数
        # （不是传一个列表）。我第一版传了列表，报
        # "Could not infer a valid transport"。
        python = os.path.abspath(sys.executable)
        transport = StdioTransport(
            command=python,
            args=["mcp_server.py"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        connect_desc = f"stdio（子进程：{os.path.basename(python)} mcp_server.py）"

    async with Client(transport) as mcp:
        # ---- 2. 拿工具清单 ----
        tools = await mcp.list_tools()
        openai_tools = [strip_title(mcp_tool_to_openai(t)) for t in tools]

        print("=" * 66)
        print(f"  MCP Client + Agent | 主题：{question}")
        print("=" * 66)
        print(f"  连接方式：{connect_desc}")
        if verbose and getattr(mcp, "instructions", None):
            print(f"  server 说明：{mcp.instructions[:100]}")
        print(f"\n  从 MCP server 拉取到 {len(openai_tools)} 个工具"
              f"（Agent 代码里没有硬编码任何工具定义）：")
        for t in openai_tools:
            props = list(t["function"]["parameters"].get("properties", {}))
            print(f"    - {t['function']['name']}({', '.join(props)})")

        # ---- 3~5. Agent 循环 ----
        messages = [
            {"role": "system",
             "content": "你是一个 CRM 数据助手。回答用中文，先给结论再给数据。"},
            {"role": "user", "content": question},
        ]

        for rnd in range(1, MAX_ROUNDS + 1):
            # 【trace 接入点 1】LLM 请求——用 span 包住，退出时自动记录耗时。
            # 只包一层 with，**不改变任何原有逻辑**。
            trace.event(EVENT_LLM_REQUEST, round=rnd, model=MODEL,
                        tools_count=len(openai_tools),
                        messages_count=len(messages))
            with trace.span(EVENT_LLM_RESPONSE, round=rnd) as sp:
                resp = client_llm.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    # 思考模式下 max_tokens 是思考链+正文共享的，给足余量
                    max_tokens=3000,
                )
                msg = resp.choices[0].message

                # usage 从 API 响应里取，是真实计费口径
                usage = getattr(resp, "usage", None)
                sp.add(
                    content_len=len(msg.content or ""),
                    tool_calls_count=len(msg.tool_calls or []),
                    prompt_tokens=getattr(usage, "prompt_tokens", 0),
                    completion_tokens=getattr(usage, "completion_tokens", 0),
                )
                # 【trace 接入点 1 结束】

            if not msg.tool_calls:
                print(f"\n【Agent 回答】\n{msg.content}")
                return msg.content

            messages.append(msg.model_dump(exclude_none=True))

            for call in msg.tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments or "{}")
                print(f"\n  [第{rnd}轮] LLM 决定调用 MCP 工具：{name}")
                print(f"         参数：{args}")

                # ---- 4. 转发给 MCP server 执行 ----
                # 【trace 接入点 2】工具调用——用 span 包住，退出时记录
                # 耗时 + 结果长度 + 成功与否。同样不改原逻辑。
                with trace.span(EVENT_TOOL_CALL, round=rnd, name=name,
                                args_summary=_summarize_args(args)) as sp:
                    result = await mcp.call_tool(name, args)

                    # MCP 返回 CallToolResult，文本在 content 里
                    payload = None
                    for block in (result.content or []):
                        if getattr(block, "type", None) == "text":
                            payload = block.text
                            break
                    if payload is None:
                        payload = str(result)

                    # 工具自己返回的 {"error": ...} 也算失败，
                    # 必须单独标出来——否则链路追踪会显示「一切正常」，
                    # 实际参数是错的
                    ok = '"error"' not in payload[:200]
                    sp.add(result_len=len(payload), ok=ok)
                # 【trace 接入点 2 结束】

                print(f"         server 返回：{payload[:160]}")

                # ---- 5. 回传给 LLM ----
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": payload,
                })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", help="SSE 模式地址，如 http://127.0.0.1:8000/mcp")
    parser.add_argument("--question", default="上海教育行业有哪些客户？")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    asyncio.run(run(args.question, args.server_url))


if __name__ == "__main__":
    main()