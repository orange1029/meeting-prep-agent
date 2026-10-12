"""
run_agent.py — 完整 Agent（智能体）循环：Schema + 实现 + DeepSeek API 打通

这是前三个文件的汇合点：
    tools_schema.py  → 工具说明书（告诉模型有什么工具）
    tools_impl.py    → 工具实现（真正干活的Python 函数）
    run_agent.py     → 把两者接起来的调度循环

Agent 循环的本质（记住这个五步）：
    1. 把messages（对话历史）+ tools（工具清单）发给模型
    2. 模型返回 tool_calls（工具调用请求）→ 说明它要调工具
    3. 我们的代码执行工具，把结果包成 role="tool" 消息
    4. 把结果追加回 messages，再发给模型
    5. 重复 2-4，直到模型不再要求调工具（它开始写最终回答）

关键细节：
  · 必须把模型返回的 assistant 消息（含 tool_calls）原样追加回messages，
    否则模型不知道「我刚才要求调了什么」，会重复调用同一个工具
  · role="tool" 消息的 tool_call_id 必须和请求里的 id 对应上
  · 内容必须是字符串（内部 json.dumps），模型才能读到

运行：
    python run_agent.py              # 跑预设的 4 个问题
    python run_agent.py "你的问题"    # 问任意问题
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
MAX_ROUNDS = 8  # 最多循环几轮，防止工具链死循环

SYSTEM_PROMPT = (
    "你是一个 CRM（客户关系管理）数据分析助手。"
    "回答要点：先给结论，再给支撑数据，用中文回答。"
    "金额要带千位分隔符并注明单位（元）。"
    "工具返回错误时，读懂错误原因后修正参数重试，不要原样重试。"
)

# 预设测试问题，覆盖不同的工具组合
DEMO_QUESTIONS = [
    "上海有哪些教育行业的客户？",
    "今年 5 月上海的成交总额是多少？",
    "帮我算一下 12000.5、8000、15600 这三笔加起来多少。",
    "给 zhangwei@shjtu-edu.com 发封邮件，告诉他五月成交进展顺利，请他继续支持。",
]


def new_client():
    return OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL)


def run_agent(client, question, verbose=True):
    """跑一轮完整 Agent 循环，返回最终回答文本"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_log = []

    for rnd in range(1, MAX_ROUNDS + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto"
        )
        msg = resp.choices[0].message

        # 模型不再要调工具 → 它已经给出最终回答
        if not msg.tool_calls:
            answer = msg.content or ""
            if verbose:
                print(f"\n【最终回答】\n{answer}")
                print("\n【工具调用链】")
                for i, step in enumerate(tool_log, 1):
                    print(f"  {i}. {step}")
                if not tool_log:
                    print("  （本次未调用工具）")
            return answer, tool_log

        # 把 assistant 消息（含 tool_calls）原样追加回对话历史
        messages.append(msg.model_dump(exclude_none=True))

        # 执行模型请求的所有工具调用
        for tc in msg.tool_calls:
            name = tc.function.name
            args_raw = tc.function.arguments
            result = run_tool(name, args_raw)

            # 工具结果必须序列化成字符串
            content = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

            ok = "error" not in result
            step = f"{'✓' if ok else '✗'} {name}({args_raw})"
            if ok:
                step += f" → {_summarize(result)}"
            else:
                step += f" → {result['error']}"
            tool_log.append(step)
            if verbose:
                print(f"  [第{rnd}轮] {step}")

    return "⚠ 达到最大轮数仍未得出答案，可能是工具链有问题", tool_log


def _summarize(result):
    """把工具结果压成一行摘要，用于日志显示"""
    if "customers" in result:
        return f"找到 {result['total']} 个客户"
    if "deals" in result:
        return f"{result['total']} 笔成交，合计 {result['amount_sum']} 元"
    if "sum" in result:
        return f"总和 {result['sum']}（{result['count']} 笔）"
    if "sent" in result:
        return f"已模拟发送 {result['message_id']} → {result['to']}"
    return str(result)[:60]


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未找到 DEEPSEEK_API_KEY，请在 .env 中配置")
        sys.exit(1)

    client = new_client()
    questions = sys.argv[1:] or DEMO_QUESTIONS

    print(f"model={MODEL}  base_url={BASE_URL}  问题数={len(questions)}\n")

    for i, q in enumerate(questions, 1):
        print("=" * 66)
        print(f"问题 {i}：{q}")
        print("-" * 66)
        run_agent(client, q)
        print()

    if SENT_EMAILS:
        print("=" * 66)
        print(f"本次共模拟发送 {len(SENT_EMAILS)} 封邮件：")
        for m in SENT_EMAILS:
            print(f"  {m['message_id']} → {m['to']} | {m['subject']}")
        print("（真实项目里这里换成 smtplib / 邮件服务商 API）")


if __name__ == "__main__":
    main()