"""
demo_autogen.py — AutoGen 最小 Demo：Researcher + Writer 两Agent 协作

主题：2025 年 LLM Agent 的发展

AutoGen 的核心抽象是「对话式多Agent」：
  · AssistantAgent= 一个有 system prompt 的 LLM 包装
  · Team        = 多个 Agent + 「谁下一个说话」+「什么时候结束」
  · Termination = 结束条件（AutoGen 0.4+ 才有，早期版本没有）

最小闭环只需 4 步：建model → 建 agent → 建 team → run_stream。

对比 LangGraph 的思路差异（实测后才明白）：
  LangGraph 是「状态机」——你定义节点和边，控制流在自己手里
  AutoGen  是「群聊」——你定义成员和发言规则，控制流交给框架

写法（47 行有效代码）
"""

import asyncio
import os
import re

from dotenv import load_dotenv
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

load_dotenv()
TOPIC = "2025 年 LLM Agent 的发展"

RESEARCHER_PROMPT = (
    "你是资料研究助手。针对给定主题，输出一段 120-180 字的事实性综述，"
    "必须包含：代表性系统/产品名、时间点、可查证的数字。"
    "只讲你确实知道的，不确定的明确说「无法核实」，绝对不要编造。"
    "输出完直接结束，不要输出 TERMINATE 之类的话——"
    "那是 Writer 的结束信号，你说了团队会提前终止。"
)
WRITER_PROMPT = (
    "你是技术报告撰写者。把研究资料压缩成一篇 200 字以内的中文报告。"
    "结构：单段式，不要小标题，不要列表。首句给核心判断，随后 2-3 句支撑。"
    "只用研究资料里的信息。写完直接结束，不要输出任何结束标记。"
)


async def main():
    # 1. 模型客户端。DeepSeek 兼容 OpenAI 协议，直接换 base_url 即可
    # model_info 的 6 个字段从 v0.4.7 起是**强制必填**的，漏一个就
    # ValueError: Missing required field 'vision'。踩过。
    # max_tokens 给足余量：AutoGen 默认 512，思考模式下会被思维链吃光。
    model = OpenAIChatCompletionClient(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model_info={
            "context_window": 128000,
            "max_tokens": 8000,
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "multiple_system_messages": False,
            "structured_output": False,
            "family": "unknown",
        },
    )

    # 2. 两个 Agent
    researcher = AssistantAgent(
        "researcher", model, system_message=RESEARCHER_PROMPT)
    writer = AssistantAgent(
        "writer", model, system_message=WRITER_PROMPT)

    # 3. 组队。
    #
    # 这里有个非常隐蔽的坑：不能用 TextMentionTermination("TERMINATE")。
    # DeepSeek 思考模式下，AutoGen 会把**思维链也当作一条消息**发出来，
    # 而思维链里会提到「我在考虑要不要输出 TERMINATE」——
    # TextMentionTermination 是纯字符串匹配，一匹配到就终止团队，
    # Writer 根本没机会发言。我实测卡死在这里，排查了很久。
    #
    # 可靠做法：用消息条数控制。两个 Agent 轮流发言，每人 2 条
    # （思维链 + 正文），4 条足够跑完一轮。
    team = RoundRobinGroupChat(
        [researcher, writer],
        termination_condition=MaxMessageTermination(4),
    )

    # 4. 跑
    print("=" * 60)
    print(f"  AutoGen | 主题：{TOPIC}")
    print("=" * 60)

    final = ""
    async for msg in team.run_stream(task=f"研究并撰写报告：{TOPIC}"):
        # run_stream 会吐出多种消息类型：
        #   TextMessage（Agent 发言，有 .source/.content）
        #   TaskResult（团队结束时的汇总对象，**没有** .source）
        # 直接访问 msg.source 会在 TaskResult 上抛 AttributeError。踩过。
        source = getattr(msg, "source", None)
        content = (getattr(msg, "content", "") or "").strip()
        if not content or source in (None, "user"):
            continue
        # 思考模式会多吐一条思维链消息（source 同样是 writer，
        # 实测长度 7000+），正文只有几百字。用长度区分。
        is_thinking = len(content) > 2000
        if not is_thinking:
            print(f"\n[{source}]\n{content[:500]}")
            if source == "writer":
                # Writer 会在末尾带一行 TERMINATE（终止信号），
                # 剥离掉才是干净的报告正文
                final = content
        else:
            print(f"[{source}]（思考中 {len(content)} 字，略）")

    print("\n" + "=" * 60)
    print("  最终报告")
    print("=" * 60)
    print(f"\n{final or '（无输出）'}\n")
    print(f"字数：{len(final)}")


if __name__ == "__main__":
    asyncio.run(main())