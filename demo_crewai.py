"""
demo_crewai.py — CrewAI 最小 Demo：Researcher + Writer 两 Agent 协作

主题：2025 年 LLM Agent 的发展

CrewAI 的核心抽象是「角色扮演 + 任务委派」：
  · Agent  = 一个「角色」（role）+ 目标（goal）+ 背景故事（backstory）
  · Task   = 一份「工作说明书」（description）+ 期望产出（expected_output）
  · Crew   = 一支团队，用 Process.sequential（顺序执行）或 hierarchical（层级）

最小闭环 4 步：建 LLM → 建 Agent → 建 Task → Crew.kickoff()。

和前两个框架的本质差异（实测后才明白）：
  LangGraph：状态机——你定义节点和边，控制流在自己手里
  AutoGen：  群聊——你定义成员和发言规则，控制流交给框架
  CrewAI：   流水线——你定义角色和任务，控制流几乎全自动

CrewAI 的心智负担最小：你甚至不用写「谁先谁后」，
按 Task 列表的顺序自动串起来。代价是灵活性也最小。

写法（48 行有效代码）
"""

import os

from dotenv import load_dotenv
from crewai import Agent, Crew, LLM, Process, Task

load_dotenv()
TOPIC = "2025 年 LLM Agent 的发展"

# 1. LLM。CrewAI 底层走 LiteLLM，OpenAI 兼容服务用 "provider/" 前缀指定
llm = LLM(
    model="openai/" + os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    # 思考模式下思考链会占用输出预算，给足余量
    max_tokens=8000,
    temperature=0.3,
)

# 2. 两个 Agent。CrewAI 的 role/backstory 是必填，缺失会报校验错误
researcher = Agent(
    role="资料研究员",
    goal=f"针对「{TOPIC}」产出一份 120-180 字的事实性综述",
    backstory="你是一位严谨的技术研究员。擅长梳理关键时间点、"
              "代表性系统与可查证数据。你从不用编造，"
              "不确定的信息会明确说明无法核实。",
    llm=llm,
    verbose=False,
)
writer = Agent(
    role="技术报告撰写者",
    goal="把研究资料压缩成 200 字以内的中文报告",
    backstory="你是一位擅长提炼要点的编辑。写作风格是单段式，"
              "首句给核心判断，随后用 2-3 句支撑，不写小标题不用列表。",
    llm=llm,
    verbose=False,
)

# 3. 两个 Task。Task 的 description 就是「工作说明书」
research_task = Task(
    description=f"研究主题「{TOPIC}」。输出一段 120-180 字的中文综述，"
                "必须包含代表性系统或产品名、时间点、可查证数字。"
                "只写你确实知道的信息，不确定的明确说明无法核实。",
    expected_output="一段 120-180 字的中文事实综述，含系统名、时间点、数字。",
    agent=researcher,
)
write_task = Task(
    description="把上面那份研究资料压缩成一篇 200 字以内的中文报告。"
                "结构：单段式，不要小标题，不要列表。"
                "首句给核心判断，随后 2-3 句支撑。"
                "只能使用研究资料里的信息，不要添加资料中没有的内容。",
    expected_output="一篇 200 字以内的中文报告正文，无标题无列表。",
    agent=writer,
)

# 4. 组队跑。Process.sequential = 按 Task 列表顺序串行执行
crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task],
            process=Process.sequential, verbose=False)

if __name__ == "__main__":
    import time

    print("=" * 60)
    print(f"  CrewAI | 主题：{TOPIC}")
    print("=" * 60)

    t0 = time.perf_counter()
    result = crew.kickoff()
    dt = time.perf_counter() - t0

    print("\n" + "=" * 60)
    print("  最终报告")
    print("=" * 60)
    print(f"\n{result.raw}\n")
    print(f"字数：{len(result.raw)}")
    print(f"耗时：{dt:.1f}s")