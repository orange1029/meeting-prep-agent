# 三框架实测对比：LangGraph vs AutoGen vs CrewAI

**实测日期**：2026-10-12
**主题**：「2025 年 LLM Agent 的发展」
**模型**：DeepSeek `deepseek-v4-flash`（三个框架全部实测跑通）
**环境**：Python 3.12.4 + `.venv-agents/`（独立虚拟环境，见文末踩坑）

---

## 一、总览对比

| 维度 | LangGraph | AutoGen | CrewAI |
|---|---|---|---|
| **有效代码行数** | 349 行 | **82 行** | **76 行** |
| **跑通耗时** | 72s | 27s | 31s |
| **产出报告字数** | 197 字（目标 200） | 259 字 | **188 字（目标 200）** |
| **心智模型** | 状态机 | 群聊 | 流水线 |
| **你定义什么** | 节点 + 边 + State | Agent + Team + 终止条件 | 角色 + 任务 + 顺序 |
| **控制流归属** | 完全在自己手里 | 交给框架（发言规则） | 几乎全自动 |
| **装包体积** | 轻（langgraph 1.2.14） | 中（autogen-agentchat 0.7.5） | **重**（crewai 1.15.27，拉了 80+ 包含 chromadb/lancedb） |

**结论先给**：
- 追求**代码最少** → CrewAI
- 追求**交互式多 Agent 讨论** → AutoGen
- 追求**复杂编排 + 可控性** → LangGraph

---

## 二、代码量对比

三个框架实现同一个「2 Agent 串行拿报告」的需求，核心代码如下（不含 import 和注释）。

### CrewAI（76 行，最少）

```python
llm = LLM(model="openai/deepseek-v4-flash", api_key=..., base_url=..., max_tokens=8000)

researcher = Agent(role="资料研究员", goal="产出120-180字事实综述",
                   backstory="严谨的技术研究员，不用编造", llm=llm)

research_task = Task(description="研究主题并输出综述，含系统名/时间点/数字",
                     expected_output="120-180字中文综述", agent=researcher)

crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task],
            process=Process.sequential)
result = crew.kickoff()
```

**你甚至不用写「谁先谁后」** —— `tasks` 列表的顺序就是执行顺序。

### AutoGen（82 行）

```python
model = OpenAIChatCompletionClient(model=..., api_key=..., base_url=...,
                                   model_info={/* 6 个必填字段 */})

researcher = AssistantAgent("researcher", model, system_message="...")
writer     = AssistantAgent("writer",     model, system_message="...")

team = RoundRobinGroupChat([researcher, writer],
                           termination_condition=MaxMessageTermination(4))
async for msg in team.run_stream(task="研究并撰写报告：..."):
    ...
```

比 CrewAI 多的部分主要是**异步**（`async for` + `asyncio.run`）和 `model_info` 那一坨配置。

### LangGraph（349 行，4 倍）

要显式定义 State（含 `Annotated` 累加语义）、3 个节点函数、条件路由函数、图组装、`compile()`。

**多出来的 270 行不是「复杂任务的必要成本」，而是我加的健壮性代码**：
- 死循环三层防护（计数器不重置 + `MAX_RETRY` 硬上限 + `recursion_limit`）
- `llm()` 封装的 max_tokens 指数重试
- `parse_sub_questions()` 各种脏格式兜底
- 报告超长截断

如果只写「顺利路径」的最小版，LangGraph 大约 120 行。三者的**净差距是 76 : 82 : 120**，不是 76 : 82 : 349。

---

## 三、耗时对比

| | 耗时 | 说明 |
|---|---|---|
| CrewAI | **31s** | 2 次 LLM 调用 |
| AutoGen | 27s | 2 轮对话，但每轮含思维链 |
| LangGraph | 72s | 5 次 LLM 调用（拆题 1 + 查资料 3 + 写作 1） |

**耗时差异的根因不是框架性能，是 LLM 调用次数**。
LangGraph 多一个 Planner 节点，就多一次调用。CrewAI 和 AutoGen 都是 2 次调用。

⚠️ CrewAI 首次安装耗时 **6 分 24 秒**（拉了 80+ 个包），AutoGen 只需 **1 分 24 秒**。这是选型时要算的成本。

---

## 四、灵活性对比

### LangGraph —— 最灵活，也最费心

唯一的「图」框架。能力边界：
- ✅ 条件路由（运行时按数据决定下一跳）
- ✅ 回退环（资料不足 → 重新查 → 再合成）
- ✅ 状态累积（`Annotated[list, operator.add]`）
- ✅ 任意拓扑（DAG、环、并行分支都行）
- ❌ 没有现成的终止/终止符机制，得自己写

**适合**：流程不固定、需要根据中间结果动态决策的场景。比如「查不到就换关键词重查」「质量不达标就重写」。

### AutoGen —— 中等灵活，交互式

模型是「群聊」：
- ✅ 多轮讨论（Agent 之间能对话，不只是单向传递）
- ✅ 多种 Team（RoundRobin / Selector / Swarm / MagenticOne）
- ✅ 成熟的条件终止（`TextMentionTermination`、`MaxMessageTermination`、`TokenUsageTermination`）
- ❌ 自定义控制流要绕框架的设计
- ⚠️ **思考模式下有致命冲突**（见下方踩坑）

**适合**：需要 Agent 之间来回协商、或需要人机混战的场景。

### CrewAI —— 最省心，最不灵活

心智负担最小，但控制力也最弱：
- ✅ 声明式，代码极少
- ✅ 内置 `Process.hierarchical`（自动分配任务，不需要你排顺序）
- ❌ 复杂流程基本做不到（想要条件分支只能自己写 Custom Task 覆盖）
- ❌ 想精确控制传给下一个 Agent 的内容，要靠 Task 的 `context` 列表，很啰嗦

**适合**：固定流水线、快速原型、让非工程师也能读懂的 Agent 流程。

---

## 五、踩坑记录（三框架实测）

这部分比对比表更有价值。

### 🔴 最严重：装包把主环境搞坏了

**现象**：在项目主环境（`D:\Python312`）装 AutoGen，pip 把 `pydantic` 升到 2.46.5 但 `pydantic-core` 卡在 2.41.5，导致**整个 pydantic 生态崩溃**，项目里所有文件都跑不了：

```
SystemError: The installed pydantic-core version (2.41.5) is
incompatible with the current pydantic version, which requires 2.46.5
```

**根因**：pip 的依赖解析在半升级场景下会留下不一致状态。强行指定版本也救不回来——镜像源里 `pydantic` 最高只到 2.14.1，而 `pydantic-core` 有 2.50.1，两者根本配不上对。

**教训**：**Agent 框架绝不要装进业务项目的主环境**。这类框架（尤其 CrewAI）依赖树庞大、版本激进，且会连带升级/降级 pydantic、openai 等基础库。

**正确做法**（本项目最终采用）：
```bash
python -m venv .venv-agents
.venv-agents/Scripts/python.exe -m pip install "autogen-agentchat>=0.4" "autogen-ext[openai]" crewai
```

### 🔴 AutoGen：`TextMentionTermination` 在思考模式下必然误触发

**现象**：用 `termination_condition=TextMentionTermination("TERMINATE")`，团队在 Researcher 发言后就终止了，Writer **根本没机会说话**，输出空字符串。

**根因**：DeepSeek 思考模式下，AutoGen 会把**思维链也当作一条消息**发出来。而思维链里会写「我需要在末尾输出 TERMINATE」——`TextMentionTermination` 是纯字符串匹配，一匹配到就终止。

**排查路径**：先怀疑 `max_turns` 不够（改到 12 无效），再打印所有消息确认 Writer 单独跑是好的，最后用 `MaxMessageTermination` 替代才定位到真凶。

**解法**：**不要依赖 LLM 输出终止符**，改用消息条数控制：
```python
termination_condition=MaxMessageTermination(4)   # 2个Agent × (思维链+正文) = 4条
```

### 🟡 AutoGen：`model_info` 6 个字段全是必填

```
ValueError: Missing required field 'vision' in ModelInfo.
Starting in v0.4.7, the required fields are enforced.
```

必须写全：`context_window` / `max_tokens` / `vision` / `function_calling` / `json_output` / `multiple_system_messages` / `structured_output` / `family`。

顺带：`max_tokens` 默认 512 太小，思考模式下正文会被思维链吃光。

### 🟡 AutoGen：`run_stream` 会吐出没有 `.source` 的对象

```
AttributeError: 'TaskResult' object has no attribute 'source'
```

团队结束时多发一个 `TaskResult` 汇总对象。用 `getattr(msg, "source", None)` 取值，别直接访问。

### 🟡 AutoGen：`max_turns` 要按「消息条数」算，不是轮数

思考模式下每个 Agent 一轮产生 **2 条**消息（思维链 + 正文）。实测 Writer 的思维链有 **7611 字**。按轮数设 6 会被思维链占满，正文轮不到。

### 🟠 LangGraph：`planner` 返回 `retry_count: 0` 导致死循环

我第一版让 `planner` 返回 `retry_count: 0`，结果条件边回退到 `planner` 时计数器被**重置**，形成「失败 → planner → 重置 → 再失败」的无限循环。图跑了 **3 分 24 秒**没停，是我手动 kill 的。

**解法**：计数器只能由 `count_retry` 递增、永不重置，加硬上限 + `recursion_limit` 兜底。

### 🟠 LangGraph：`add_conditional_edges` 的 path_map 键必须与路由返回值一致

```
KeyError: 'count_retry'
```

路由函数 `return "count_retry"`，但 path_map 里写的是 `"researcher"`。两处对不上就 KeyError。

### 🟠 LangGraph：`lstrip` 把「2025」啃成「025」

```python
ln.strip().lstrip("1. 2. 3. 4. 5. ")   # "2025年..." → "025年..."
```

`lstrip` 按**字符集**剥离，遇到 `2025年` 会把 2、0、5 全当编号剥掉。改用正则：
```python
re.sub(r"^(\d+)\s*[\.、\)）:：]\s*", "", ln)
```

### 🟡 通用：`max_tokens` 被思考链吃光（三个框架都中招）

这是**今天踩得最隐蔽的坑**，三个框架全遇到：

```
finish_reason=length, content='', reasoning_content=2401字
```

API 调用**成功**，但 `content` 是空字符串。极易误判成 prompt 写错。

**关键认知**：思考链长度与输入长度**正相关**，不能按「输出多少字」估算预算。
**解法**：给足余量 + 检测空正文后**指数重试**（×3 → ×4 → ×4）。

### 🟢 CrewAI：几乎零坑

唯一一次失败是 `pip install crewai` 被 `-q` 静默掐断（去掉 `-q` 才看到真因）。写好代码后**一次跑通**。

---

## 六、选型建议

| 你的处境 | 选谁 |
|---|---|
| 快速验证一个想法 / 原型 | **CrewAI**（76 行，1 小时能出结果） |
| 多 Agent 需要讨论、辩论 | **AutoGen**（群聊是它的主场） |
| 流程有条件分支、要回退、要人工介入 | **LangGraph** |
| 要让非工程师看懂/改 Agent 流程 | **CrewAI**（声明式最易读） |
| 未来要加持久化、断点续跑 | **LangGraph**（有 checkpointer 机制） |

**如果只能选一个**：LangGraph。理由是它虽然代码多，但**能力是另外两者的超集**——CrewAI 和 AutoGen 能做的编排，LangGraph 都能做；反之不行。

---

## 七、复现步骤

```bash
# 1. 建隔离环境（务必不要装进主环境）
python -m venv .venv-agents

# 2. 装包
.venv-agents/Scripts/python.exe -m pip install "autogen-agentchat>=0.4" "autogen-ext[openai]" crewai python-dotenv

# 3. 跑
.venv-agents/Scripts/python.exe demo_autogen.py    # AutoGen，约 27s
.venv-agents/Scripts/python.exe demo_crewai.py     # CrewAI，约 31s
python multi_agent_graph.py                        # LangGraph 用主环境，约 72s

# 4. 看 LangGraph 图结构（不需要 API Key）
python multi_agent_graph.py --ascii
```

⚠️ `.env` 配置文件在项目根目录，三个 demo 共用。