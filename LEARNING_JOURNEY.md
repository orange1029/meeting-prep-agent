# Agent 开发学习完整流程

> **作者**：一个从乙方咨询转 FDE 的工程师  
> **周期**：2026-10-10 ~ 2026-10-13（4 天，每天 8-10 小时）  
> **底子**：Python 零基础、SQL 零基础、Agent 零基础  
> **现状**：能独立设计并实现生产级 AI Agent 系统  
> **目标读者**：想转 Agent 工程师 / FDE 的同行；想了解"零基础到能写 Agent 真实要多久"的人

---

## 一、写在前面

这份文档不是教程，是**复盘**——把"从零到能做 Agent"这条路上**每个步骤的目的、意义、结果**讲清楚。

重点不是"我学了什么"，而是"我为什么学这个，学完能干什么，中间踩了什么坑"。

如果你正在考虑学 Agent 开发，这篇文档能帮你判断：**这条路值不值走、多久能走完、难不难**。

---

## 二、流程概览

### 2.1 五步路线图

```
步骤 1：主流模型扫盲
   ↓
步骤 2：单 Agent 基础（Function Calling + Agent Loop）
   ↓
步骤 3：单 Agent 进阶（流式响应 + 长期记忆）
   ↓
步骤 4：多 Agent 协作（LangGraph / AutoGen / CrewAI 对比）
   ↓
步骤 5：Agent 产品化（MCP 协议 + 评估 + 调试）
   ↓
步骤 6：真实项目（个人开源 / 提 PR / 解决行业问题）
```

### 2.2 时间投入

| 阶段   | 时间    | 关键产出                                       |
| ---- | ----- | ------------------------------------------ |
| 步骤 1 | 半天    | 9 个主流 LLM 能力对比表 + 选型                       |
| 步骤 2 | 1.5 天 | 4 个 Tool + Agent Loop + 错误注入测试 26/26       |
| 步骤 3 | 1 天   | 流式响应 + 长期记忆（三层结构）                          |
| 步骤 4 | 1 天   | LangGraph 3 Agent 协作 + AutoGen/CrewAI 横向对比 |
| 步骤 5 | 0.5 天 | MCP server + 14 case 评估 + trace 调试         |

总计：约 4 天，每天 8-10 小时，**总投入约 35-40 小时**。

### 2.3 最终能力图谱

| 能力               | 达到水平                                          |
| ---------------- | --------------------------------------------- |
| LLM 主流模型选型       | 能讲清 9 个模型的差异和适用场景                             |
| Function Calling | 能写 Schema 描述让模型 100% 选对工具                     |
| Agent Loop       | 能写出 5 步主循环 + 处理流式 / 工具调用 / 错误回传               |
| 长期记忆             | 能设计"短期上下文 / 长期记忆 / 原始数据"三层结构                  |
| 多 Agent 协作       | 能用 LangGraph 实现状态机式多 Agent，含回退 / 重试 / 路由      |
| MCP 协议           | 能写 server + client，理解"MCP 是 Agent 工具的 USB 标准" |
| Agent 评估         | 能建 Eval 数据集 + 跑 14 个 case + 给出优化方向            |
| Agent 调试         | 能写 trace logger + 自动分析瓶颈                      |

---

## 三、每步详解

### 步骤 1：主流模型扫盲

#### 目的

**解决"不知道用哪个模型"的问题**。

AI 圈有 9 个以上主流 LLM（GPT-4o、Claude、DeepSeek、Qwen、GLM 等），各自擅长不同、价格差 100 倍、上下文长度差 10 倍。不搞懂它们，就没法做 Agent 选型。

#### 意义

| 学完能干什么         | 实际场景                                       |
| -------------- | ------------------------------------------ |
| 面试被问"用过哪些 LLM" | 能讲 9 个模型的能力差异                              |
| 项目选型           | 知道"中文场景选 Qwen，工具调用选 GPT-4o，便宜量大选 DeepSeek" |
| 控制成本           | 同样任务一年账单能从 10 万降到 1 千，靠模型选型                |

#### 关键指令

> 写一个 Python 脚本 `model_bench.py`，同一段 prompt 跑 5 个模型，对比耗时 / token / 价格 / 质量。

#### 实际产出

- `model_bench.py`：并发调用 5 个 LLM 的命令行工具
- 一张**模型选型速查表**（10 个场景对应推荐模型）

#### 关键决策

- **用 DeepSeek 不用 GPT-4**：DeepSeek 便宜 50 倍，中文强，OpenAI 兼容协议（写一份代码能跑 DeepSeek / GPT-4o / Qwen 三个模型）
- **用 5 个模型不是 9 个**：够覆盖主流，价格区间从 0.001 元到 0.1 元/千 token

---

### 步骤 2：单 Agent 基础

#### 目的

**学会"让 LLM 决定调什么工具、自己写代码执行工具"**。

这是 Agent 的核心。前面学的 LLM 只能回答问题，**Agent 能自己干活**——查数据库、算金额、发邮件。

#### 意义

| 学完能干什么 | 实际场景                                              |
| ------ | ------------------------------------------------- |
| 简历能写   | "独立设计并实现 AI Agent（Function Calling + Agent Loop）" |
| 面试必问   | "Agent 怎么工作""Tool 怎么设计"——这一步骤给你答案                 |
| 后续基础   | 步骤 3 / 4 / 5 全部基于这一步                              |

#### 关键指令

1. **写 `tools_schema.py`**：定义 4 个 Tool 的 OpenAI function calling 格式
   - **为什么用 OpenAI function calling 格式**（不用 Anthropic tool use、也不用自己造协议）：
     - **OpenAI 是事实标准**——DeepSeek / Qwen / GLM 三大国产模型都兼容
     - **一份代码能跑 5 个模型**（换 base_url 即可，不用改任何业务代码）
     - 简历写"Function Calling"是行业通用词，面试官一听就懂
   - **为什么单独一个文件**（不写在 agent.py 里）：
     - Schema 是"说明书"，实现是"干活的"——**职责分离**
     - 后续换实现（比如从 mock 换真 DB）只动实现，不动 Schema
     - 多个 Agent 可以共享同一份 Schema
2. **写 `tools_impl.py`**：实现 4 个 Tool 的 Python 函数
   - **为什么用 mock 数据**（不接 PostgreSQL）：
     - Agent 测试阶段 mock 更可控（不依赖 PG 跑没跑）
     - 4 个 demo 全跑通（用 PG 还得先启 PG，反而更复杂）
     - 注释里明确写"以后接真实数据库只需替换函数体，Schema 和 Agent 一行不用改"——这是**工具可替换**的设计
   - **为什么函数返回 `dict` 而不是 `raise`**：
     - `raise` 会**中断整个 Agent 循环**，用户看到的是 traceback
     - 返回 `{"error": "stage 取值无效：'已成交'，合法值：won、lost、..."}` 让 LLM 看到错误后**自己纠正参数**——这是 Agent Loop 的核心能力之一
   - **为什么用 `_clean / _clamp / _parse_date` 等小工具函数**：
     - **容错前置**——脏参数先清洗成标准格式（"None" / "无" / "不限" 统一成 ""）
     - 业务函数只处理"对的输入"，逻辑干净
     - 这三个小函数在 4 个 Tool 之间复用，不重复造轮子
3. **写 `agent.py`**：主循环（LLM 思考 → 调工具 → 看结果 → 继续或回答）
   - **为什么是 while 循环 + if 判断**（不是状态机 / 事件驱动）：
     - Agent 没那么神秘——**就是 while 循环**
     - 5 步流程比"状态机"更易读，新人能 5 分钟看懂
     - 状态机适合多 Agent（步骤 4 才用），单 Agent 用不到
   - **为什么用 `MAX_ROUNDS = 8`**：
     - 工具链可能死循环（模型反复调同一个工具）
     - 不设上限进程会卡死，**这是单 Agent 最容易翻车的地方**
   - **为什么 assistant 消息必须原样回传**：
     - 模型下一轮需要知道"我刚才要求调了什么"
     - 漏了这一行会**重复调同一个工具**（实测过——Agent 卡在同一个 query 调 5 次）
4. **写 `test_errors.py`**：错误注入测试（26 个 case）
   - **为什么要专门测错误**（不是只测对的）：
     - Agent 上生产**最大的风险是"答错"和"被注入"**，不是"答对"
     - 正常路径 100% 没用——只要有 1 个注入漏洞就上新闻
   - **为什么 26 个 case**（不是 5 个）：
     - 覆盖 3 类：**A 参数层 / B 行为层 / C 安全层**
     - C 组（提示注入 + 越权）是生产 Agent 必备——**这是简历加分项，99% 的 Agent 教程不讲**
   - **为什么用规则匹配**（不是比对文本）：
     - 模型输出是概率性的，**不能断言"必须等于某句话"**
     - 用 `required_any` / `forbidden` 组合判断（必须包含 X / 不能包含 Y），容错性更强

#### 实际产出

- **`tools_schema.py`**：4 个 Tool 的 JSON Schema（含 description / parameters / required）
  - **为什么 description 写得这么长**（含"什么时候用 / 什么时候不用 / 参数填什么"）：
    - 模型读的是 description，**写错了再正确的代码也白搭**
    - 工具之间互相点名（如"要发邮件请用 B"）能显著降低误选率——实测 prompt 加一句"如果用户要发邮件请用 send_email，不要用本工具"，误选率从 30% 降到 0%
- **`tools_impl.py`**：含 mock 数据 + 参数校验 + 错误处理
  - **为什么参数校验放最外层**（不在函数内）：
    - 函数内 `**kwargs` 会**静默吞掉拼错的参数**（`{"amount_list": [1,2]}` 被当 `amounts=""` 处理，返回"为空"，模型以为是自己漏填，又补一遍——绕了一圈居然也能对，纯属运气）
    - 在分发层主动校验 `unknown_params`，直接报错并列出合法参数名
  - **为什么用 `if not summary` 容错**（不 raise）：
    - raise 会让 Agent 整个循环崩，用户看到 traceback
    - 返回 `{"error": ...}` 让 LLM 读懂后改正——**这是 Agent 区别于"if-else 程序"的关键**
- **`agent.py`**：5 步主循环
  - **为什么 `messages.append(msg.model_dump(...))` 这一行必须有**：
    - assistant 消息原样追加（含 tool_calls）
    - 漏了模型下一轮会**重复调同一个工具**（实测过——Agent 卡在同一个 query 调 5 次）
  - **为什么用 `if not msg.tool_calls` 判定循环结束**：
    - 不是我们判断"任务完成"——**我们不知道任务完没完成**
    - 模型自己决定"我已经答完了"才退出循环
    - 这是 Agent Loop 的终止条件，**不能由我们写 if-else 强断**
- **`test_errors.py`**：26 case 全部通过
  - **为什么分 A / B / C 三组**：
    - A 组：参数层（脏参数是否被工具拦住）—— 12 个 case
    - B 组：行为层（Agent 面对刁钻输入的表现）—— 8 个 case
    - C 组：安全层（注入与越权，红线是否被突破）—— 6 个 case
  - **为什么用"反向断言"**（`must_refuse` + `forbidden` 组合）：
    - 模型解释拒绝时会**复述被禁内容**——这是正确行为不能算踩红线
    - 用 `must_refuse` 判定"是否明确拒绝"再决定 forbidden 是否豁免
    - 简单禁止"出现 X"会被误判，**用 `must_refuse` 兜底**是评估器的关键设计

#### 关键收获

1. **Agent Loop 就是 while 循环 + if 判断**——模型的唯一能力是"输出文本，并在文本里表达意图"
2. **description 才是 Schema 的重点**——代码只是骨架，模型读的是 description，写错了再正确的代码也白搭
3. **错误信息要让模型能自我纠正**——错误信息的目标不是"说清楚发生了什么"，而是"让模型知道下一步该填什么"
4. **assistant 消息必须原样回传**——少了这行，模型下一轮会重复调同一个工具

#### 关键数据

- 4 个 Tool：query_customers / query_deals / calc_sum / send_email
- 错误注入测试：26/26 全过
- 实际 demo：4 个问题全部跑通，包括 3 层指代理解（"他们"指代上文客户、"张伟"指代联系人、"具体数据"指代上轮数字）

#### 踩过的坑

- **参数名拼错被静默吞掉**：`**kwargs` 让人写得舒服，但把校验责任推给了别人。修法是在分发层主动校验，未知参数直接报错
- **链式调用不是模型自动做的**：每一次"链"都是我们代码在中间搬运数据。模型看不到工具结果，除非我们喂给它
- **MAX_ROUNDS 必须设上限**：工具链可能死循环，不设上限进程会卡死

---

### 步骤 3：单 Agent 进阶

#### 目的

**让 Agent 能"流式输出 + 长期记忆"**。

基础 Agent 一次问答完事，进阶 Agent 要能：

- **多轮对话**：用户连续问 5 轮，每轮都要带上下文
- **流式响应**：用户看到 token 一个一个出来，不用等几十秒
- **跨会话记忆**：下次启动还记得你说过什么

#### 意义

| 学完能干什么 | 实际场景                         |
| ------ | ---------------------------- |
| 简历能写   | "实现多轮对话 Agent（含流式响应 + 长期记忆）" |
| 用户体验   | 从"等 30 秒看结果"变成"边生成边看"        |
| 个性化    | Agent 记得"用户偏好结论先行"——不用每次重复说  |

#### 关键指令

1. 写 `run_chat.py`：多轮对话 + 流式响应
2. 写 `memory_store.py`：长期记忆持久化
3. 写 `memory_tools.py`：save_memory / recall_memory 工具
4. 写 `run_chat_with_memory.py`：集成长期记忆的版本

#### 实际产出

- `run_chat.py`：交互式多轮 Agent（含流式 + 5 个命令 + 思考模式兼容）
- `run_chat_with_memory.py`：长期记忆版（含 save/recall/decay 三个机制）

#### 关键收获

1. **流式下 tool_calls 是分片到达的**——这是整个项目最隐蔽的坑，必须按 index 分组累积（id 只在第一段、name 只在第一段、arguments 必须 += 拼接）
2. **reasoning_content 必须回传**——思考模式下模型会输出 `reasoning_content`，下一轮请求不带回去就 400
3. **记忆分三层**：短期 messages（本次进程内）、长期 JSON（跨会话）、原始数据（事实来源）。混淆这三层是记忆系统最常见的失败原因
4. **记忆只是线索，不是事实来源**——真实数据必须用工具查。若记忆与工具查询结果冲突，以工具结果为准

#### 关键数据

- 多轮对话：3 层指代全部理解（"他们"/"张伟"/"具体数据"）
- 长期记忆：跨会话回忆 + 强弱相关分离 + 衰减遗忘
- 记忆库：1 条偏好 + 6 个管理命令

#### 踩过的坑

- **流式 chunk 拼装**：模型返回的 arguments 是切碎的 JSON 片段，直接 json.loads 必然失败。正确做法是按 index 分组累积
- **GBK 编码**：Windows 默认编码，Python 写中文文件不显式 encoding="utf-8" 会乱码
- **记忆的强弱相关必须分离**：单一阈值做不了二分类——调阈值只能解一头。真正的解法是承认"相关有两种程度"

---

### 步骤 4：多 Agent 协作

#### 目的

**学会"多个 Agent 协作完成复杂任务"**。

单 Agent 能做 80% 的事，但遇到"研究一个主题"这种任务，需要：

- **Planner**：把主题拆成子问题
- **Researcher**：每个子问题查资料
- **Writer**：把资料合成报告

一个 Agent 串行做这事太慢，多 Agent 协作才能高效。

#### 意义

| 学完能干什么 | 实际场景                                         |
| ------ | -------------------------------------------- |
| 简历能写   | "使用 LangGraph 构建多 Agent 协作系统"                |
| 实际场景   | 客服 Agent、研究 Agent、数据分析 Agent 多角色协作           |
| 选型     | 知道 LangGraph / AutoGen / CrewAI 三个框架的差异和适用场景 |

#### 关键指令

1. 写 `multi_agent_graph.py`：LangGraph 完整 3 Agent 协作
2. 写 `demo_autogen.py`：AutoGen 最小 demo
3. 写 `demo_crewai.py`：CrewAI 最小 demo
4. 写 `FRAMEWORK_COMPARISON.md`：3 框架横向对比

#### 实际产出

- `multi_agent_graph.py`：完整状态机（含回退环 / 条件路由 / 死循环三层防护）
- `demo_autogen.py`：AutoGen 群聊式 2 Agent
- `demo_crewai.py`：CrewAI 流水线式 2 Agent
- `FRAMEWORK_COMPARISON.md`：3 框架实测对比报告（含踩坑记录）

#### 关键收获

1. **LangGraph 是状态机，AutoGen 是群聊，CrewAI 是流水线**——三种心智模型，三种适用场景
2. **耗时差异的根因不是框架性能，是 LLM 调用次数**——LangGraph 多一个 Planner 节点就多一次调用
3. **能力是另外两者的超集**——CrewAI 和 AutoGen 能做的编排，LangGraph 都能做；反之不行
4. **框架必须装在隔离 venv**——CrewAI 装包把主环境 pydantic 搞崩溃过，教训深刻

#### 关键数据

| 框架        | 代码行数        | 跑通耗时 | 输出字数  |
| --------- | ----------- | ---- | ----- |
| LangGraph | 349 行（含健壮性） | 72s  | 197 字 |
| AutoGen   | 82 行        | 27s  | 259 字 |
| CrewAI    | 76 行        | 31s  | 188 字 |

**净差距**（去掉 LangGraph 的健壮性代码）：76 : 82 : **120**，不是 76 : 82 : 349

#### 踩过的坑

- **planner 返回 retry_count: 0 导致死循环**：图跑了 3 分 24 秒没停
- **AutoGen TextMentionTermination 在思考模式下必然误触发**：思维链里出现"TERMINATE"字面就终止
- **AutoGen model_info 6 个字段全必填**：v0.4.7+ 强校验，漏一个就 ValueError
- **max_tokens 被思考链吃光**：三个框架全遇到，"API 成功但 content 是空字符串"，极易误判

---

### 步骤 5：Agent 产品化

#### 目的

**让 Agent 真正能上生产**——不是 demo，是能跑、能评估、能调试的产品。

#### 意义

| 学完能干什么 | 实际场景                                       |
| ------ | ------------------------------------------ |
| 简历能写   | "使用 MCP 协议构建可复用工具生态" + "具备 Agent 评估与调试能力"  |
| 上生产    | MCP 让 Agent 不再 hardcode 工具，换数据源不改 Agent 代码 |
| 持续迭代   | 评估 + trace 让 Agent 错误能定位、能修复               |

#### 关键指令

**5.1 MCP 协议**：

- 写 `mcp_server.py`：用 fastmcp 暴露 2 个工具
- 写 `mcp_client_demo.py`：Agent 通过 MCP 调 server 工具

**5.2 Agent 评估**：

- 写 `eval_mcp_agent.py`：跑 14 个 case，3 个核心指标
- 写 `EVAL_REPORT.md`：评估报告

**5.3 Agent 调试**：

- 写 `trace_logger.py`：JSONL 格式 trace 记录器
- 写 `trace_analyze.py`：trace 分析工具（自动找瓶颈）
- 接入 `mcp_client_demo.py`：3 个 trace 接入点

#### 实际产出

- `mcp_server.py`：2 个工具（query_customers / query_deals）
- `mcp_client_demo.py`：MCP client + Agent，**Agent 代码里没有硬编码任何工具定义**
- `eval_mcp_agent.py`：14 case × 8 类场景的评估脚本
- `EVAL_REPORT.md`：含核心指标 + 分类表现 + 元认知（"100% 不能直接采信"）
- `trace_logger.py`：JSONL 流式 trace + 自测
- `trace_analyze.py`：自动找最新文件 + 3 维度分析 + 优化建议

#### 关键收获

1. **MCP 是 Agent 工具的 USB 标准**——写一次 server，到处可插。Agent 不再 hardcode 工具定义
2. **评估不只是"对不对"**——还要看"花多少"。一个 case 全绿但调了 3 次工具，比 1 次贵 3 倍
3. **trace 立刻能给出优化方向**——分析器报"末轮输入 > 首轮"，立刻知道该上 prompt caching
4. **「100% 不能直接采信」**——14 个 case 都自己设计的、mock 数据太规整、错误路径没充分压测、没有并发测试

#### 关键数据

- MCP 评估：14/14 通过，工具选择 100% / 答案准确 100% / 无编造 100%
- 平均延迟 2.7s，平均 token 1865，平均成本 $0.000307/次
- 评估器踩的坑：同义词误判、漏统计调用次数、断言比 Agent 错了


#### 踩过的坑

- **MCP 协议**：stdio 模式绝不能 print 到 stdout（JSON-RPC 通道），调试信息必须写 stderr
- **fastmcp 4.x 不再自动推断 transport**：必须显式构造 StdioTransport
- **streamable-http vs sse 自动选错 → 502 Bad Gateway**：显式指定 transport
- **工具自己返回的 {"error": ...} 也算失败**：不能只看异常，要看 payload 里有没有 error

---

## 四、关键技术决策

### 4.1 为什么用 DeepSeek 不用 GPT-4

| 维度     | DeepSeek V3    | GPT-4o          |
| ------ | -------------- | --------------- |
| 价格（输出） | ¥2 / 1M tokens | ¥72 / 1M tokens |
| 中文能力   | ★★★★★          | ★★★★            |
| 工具调用   | ★★★★           | ★★★★★           |
| 协议     | OpenAI 兼容      | OpenAI          |

**结论**：日常用 DeepSeek 主力，关键场景（要求稳定）切 GPT-4o。**一份代码能跑两个模型**（OpenAI 兼容协议）。

### 4.2 为什么用 mock 数据不用真实 DB

- Agent 测试阶段 mock 更可控（不依赖 PG 跑没跑）
- 注释里明确说"以后接真实数据库只需替换函数体"
- 4 个 demo 全跑通（用 PG 还得先启 PG，更复杂）

### 4.3 为什么选 LangGraph 做主力多 Agent

| 框架            | 适合场景               |
| ------------- | ------------------ |
| **LangGraph** | 复杂流程 + 可控性 + 需要持久化 |
| **AutoGen**   | 多 Agent 讨论 / 辩论    |
| **CrewAI**    | 快速原型 / 非工程师能读懂     |

**结论**：LangGraph 代码多但能力是超集——CrewAI 和 AutoGen 能做的 LangGraph 都能做，反之不行。

### 4.4 为什么必须隔离 venv

- **踩坑实录**：装 CrewAI 时 pip 把 pydantic 升到 2.46.5 但 pydantic-core 卡在 2.41.5，**整个 pydantic 生态崩溃**
- 解决：所有 Agent 框架装到 `.venv-agents/` 独立 venv，主环境不被污染
- **Agent 框架绝不要装进业务项目的主环境**

---

## 五、踩过的坑（实战经验汇总）

按"坑" → "解法"的方式列：

### 5.1 Agent Loop 相关

- **assistant 消息必须原样回传**——少了下一轮重复调工具
- **tool_call_id 必须对得上**——模型认不出哪个结果对应哪个请求
- **MAX_ROUNDS 必须设上限**——工具链可能死循环

### 5.2 Function Calling 相关

- **参数名拼错被 `**kwargs` 静默吞掉**——在分发层主动校验未知参数
- **description 写得不够强，模型选错工具**——描述里互相点名
- **错误信息必须带正确值的样子**——格式错要给格式示例，枚举错要列合法值

### 5.3 流式响应相关

- **tool_calls 是分片到达的**——按 index 分组累积（id/name 只在第一段、arguments += 拼接）
- **reasoning_content 必须回传**——思考模式下下一轮请求丢了就 400
- **stdin/stdout 是 JSON-RPC 通道**——stdio 模式绝不能 print 到 stdout

### 5.4 多 Agent 相关

- **planner 不能重置 retry_count**——计数器只能由 count_retry 递增，永不重置
- **path_map 键必须与路由返回值一致**——两处对不上就 KeyError
- **lstrip 把 "2025" 啃成 "025"**——用正则别用 lstrip
- **max_tokens 被思考链吃光**——三个框架全遇到，API 成功但 content 是空字符串

### 5.5 评估相关

- **同义词当成需要全部命中**——"没有/0/未找到" 命中任一即可
- **第一版报告全绿**——漏了"调用次数"指标
- **断言失败时先质疑断言本身**——这一条在这个项目里踩了 5 次，每次都是测试错、代码对

### 5.6 环境相关

- **装包把主环境搞坏**——必须隔离 venv
- **GBK 编码**——Python 写中文文件必须显式 encoding="utf-8"
- **deepseek-v4-flash 在 autogen 内置映射里没有**——会出 warning 但不影响功能

---

## 六、简历会写什么

15 天后能写进简历的 8 行：

> ✅ 熟悉 Python / PostgreSQL / FastAPI / Docker  
> ✅ 熟悉主流 LLM：GPT-4o、Claude 3.5/3.7、DeepSeek V3/R1、Qwen2.5、GLM-4 的能力差异与适用场景  
> ✅ 独立设计并实现 AI Agent 系统（Function Calling + Agent Loop + 错误回传机制）  
> ✅ 使用 LangGraph 构建多 Agent 协作系统（Planner/Researcher/Writer 状态机，含回退与重试）  
> ✅ 理解 MCP 协议，能编写 MCP server + client  
> ✅ 具备 Agent 评估能力（建 Eval 数据集 + 跑 14 case + 给出 token/成本/准确率报告）  
> ✅ 实现 Agent trace 调试（JSONL 记录 + 自动瓶颈分析 + prompt caching 优化建议）  
> ✅ 端到端交付过 Agent 产品 demo

**可投岗位**：Agent 工程师 / AI 应用工程师 / FDE / AI 产品工程师

---

## 七、给后续学习者的建议

### 7.1 如果你也是零基础

1. **不要从框架开始**——先把步骤 2 的"单 Agent 基础"吃透。框架只是工具，Agent Loop 才是核心
2. **不要从理论开始**——直接动手写。看了 100 篇博客不如写 1 个 Tool
3. **每一步都建测试**——没有测试的 Agent 上生产就是赌命
4. **每个坑都写注释**——未来的你会感谢现在的你

### 7.2 如果你已有 Python 基础

1. **跳过步骤 1 的代码部分**——直接看模型选型速查表
2. **重点看步骤 4-5**——多 Agent 框架对比和 MCP 协议是简历加分项
3. **trace 调试 + 评估**——这是区分"会写 Agent"和"会上生产 Agent"的关键

### 7.3 如果你要做真实项目

按价值排序：

1. **A：个人 Agent 工具开源到 GitHub**（简历"独立开源项目"）
2. **B：给 LangGraph / LangChain 提 PR**（简历"开源贡献"——**含金量最高**）
3. **A + B 组合**——既有自己的项目，也有开源贡献

**推荐先做 A，再做 B**——A 让你建立完整作品集，B 让你看生产级代码。

### 7.4 持续学习方向

- **MCP 生态**：每天都有新 server 出来，跟踪 <https://github.com/modelcontextprotocol/servers>
- **多 Agent 框架**：LangGraph 文档 + Anthropic 的 Building Effective Agents
- **Agent 评估**：LangSmith / Langfuse 是生产标准，建议接一个看 trace
- **RAG / 长上下文**：1M context 的模型出来后，RAG 的边界变了
- **Agent 安全**：Prompt injection 是真实威胁，每次给 Agent 接新工具都要重新评估

---

## 八、参考资源

- **官方文档**：OpenAI Function Calling、Anthropic Building Effective Agents、LangGraph Docs
- **代码**：`D:\workbuddy1\2026-10-10-18-00-03\` 下全部文件
- **笔记**：`NOTES.md`（步骤 2-3 收获）+ `EVAL_REPORT.md`（步骤 5.2 评估）+ `FRAMEWORK_COMPARISON.md`（步骤 4 框架对比）

---

> **最后一句话**：Agent 没那么神秘——就是 while 循环。模型的唯一能力是输出文本，**我们写代码把意图变成行动**。把这件简单事想清楚、做扎实，你就超过了 80% 的 Agent 工程师。
