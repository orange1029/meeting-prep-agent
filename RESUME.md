# 橘⼦加盐

**Agent 工程师 / Forward Deployed Engineer（FDE）**

📧 <orange1029@example.com>  |  📱 138-XXXX-XXXX  |  🔗 [github.com/orange1029](https://github.com/orange1029)

---

## 个人简介

乙方咨询转 FDE，4 天高强度学习完成 Agent 工程师全栈能力。**专注 AI Agent 的设计、实现与生产化**——涵盖 Function Calling / Agent Loop / 多 Agent 协作 / MCP 协议 / 评估与调试全链路。具备独立交付「从 0 到 1 的 Agent 产品」能力，同时能给知名开源项目（LangGraph）提 PR。简历中所有项目均附 GitHub 链接与可复现 demo。

---

## 核心技能

| 分类             | 技能                                                                      |
| -------------- | ----------------------------------------------------------------------- |
| **LLM 主流模型**   | DeepSeek V3 / R1、GPT-4o、Claude 3.5 / 3.7、Qwen2.5、GLM-4（含选型与成本测算）        |
| **Agent 协议**   | **MCP**（Model Context Protocol，2025 标准）、Function Calling、Tool Schema 设计 |
| **多 Agent 框架** | **LangGraph**（状态机）、AutoGen（群聊）、CrewAI（流水线）—— 含三框架实测对比                   |
| **核心工程能力**     | Python 3.12、PostgreSQL、OpenAI SDK、agent 评估、JSONL trace 调试               |
| **方法论**        | 「确定性流程用确定性代码，LLM 只做主观判断」/「记忆是线索不是事实」/「评估器也可能错」                          |

---

## 项目经历

### 1. 会前简报 Agent ⭐ 独立开源项目

**GitHub**: [orange1029/meeting-prep-agent](https://github.com/orange1029/meeting-prep-agent) · **栈**: Python · DeepSeek · MCP

- **S（情境）**：售前顾问会前需查客户档案 / 行业动态 / 历史沟通，散落在 CRM、新闻、个人记忆里，凑齐要半小时且容易漏
- **T（任务）**：设计 Agent 工具，**30 秒产出一页可上会的 markdown 简报**，且数据缺失必须显式标注
- **A（行动）**：
  - 设计 6 个 Tool（4 业务 + 2 简报专用），通过 MCP 协议暴露——**Agent 代码里无任何 hardcode 工具定义**
  - **「代码控流程 + LLM 填内容」混合架构**：前 3 段（档案 / 趋势 / 历史）用模板代码渲染保证格式稳定，第 4 段（沟通建议）由 LLM 生成
  - 行业别名匹配（医院→医疗、银行→金融）+ 真实读长期记忆库
  - **JSONL trace 调试** + 14 case 评估体系（含 A 参数层 / B 行为层 / C 安全层）
- **R（结果）**：简报生成 7-8 秒，**建议直接引用具体日期 + 记忆 ID**（如"M0009 预算上限待确认"）；0 个 ⚠ 数据缺失标注
- **踩坑文档**：`README.md` 含 5 条已知限制与项目取舍说明

### 2. LangGraph 开源贡献 ⭐ PR #9319

**仓库**: [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) · **Issue**: [#9319](https://github.com/langchain-ai/langgraph/issues/9319)

- **S**：`add_conditional_edges` 的 router 返回非 `path_map` 键时 LangGraph 报裸 `KeyError`，无任何上下文（路由值、合法键、修复方向都没有）
- **T**：在 `langgraph/graph/_branch.py` 加 `_resolve_destination` 方法，把 `KeyError` 换成信息完整的 `ValueError`
- **A**：
  - 精准重构：1 个新方法 + 1 行调用点替换
  - 8 个测试 case：**2 错误测试（ValueError 内容）+ 6 兼容测试**（path_map 多种形式 / Send 对象 / 非 str 键）
  - **6 段 PR 描述**（Status / Summary / Details / Scope / Tests / Note on related reports）
  - **Note 段主动区分 4 个相似 issue**（#6770 #6888 #6771 #6926），说"orthogonal / composes with those fixes"——资深贡献者做法
- **R**：PR 被 `require_issue_link` gate 自动关闭（外部贡献者需先被分配到 issue）；已去 issue 评论请求分配，bot 会在分配后自动重开
- **协作理解**：完全理解 LangGraph 的 `pr_lint.yml` 与 issue-link 机制，**Title 遵守 `fix(langgraph): ...` 格式**

### 3. 多 Agent 协作系统（LangGraph 完整实战）

**栈**: LangGraph · Python · DeepSeek

- **S**：单 Agent 能做 80% 的事，但研究类任务需要 Planner / Researcher / Writer 多角色协作
- **T**：用 LangGraph 状态机搭建 3 Agent 协作系统，支持回退与重试
- **A**：
  - `StateGraph` + `TypedDict` + 条件路由 + 回退环（资料不足自动补查）
  - 死循环**三层防护**：`MAX_RETRY` 硬上限 + LangGraph `recursion_limit` + 计数器只增不减
  - **research 字段用 `Annotated[list, operator.add]` 累加语义**——避免 LangGraph 默认的覆盖语义把前一次结果清掉
  - 实测对比 LangGraph / AutoGen / CrewAI 三框架，**净代码行差距 76 : 82 : 120**（去掉 LangGraph 健壮性代码）
- **R**：`FRAMEWORK_COMPARISON.md` 三框架对比报告，含踩坑（`TextMentionTermination` 思考模式下必然误触发、AutoGen `model_info` 6 字段全必填、CrewAI 装包 pydantic 崩溃等）

### 4. MCP Agent + 评估 + 调试（生产化三件套）

**栈**: MCP · fastmcp · Python · JSONL

- **S**：Agent 上生产需要「标准化工具协议 + 评估体系 + 调试能力」三件套，缺一不可
- **T**：基于 MCP 协议构建可复用工具生态 + 14 case 评估框架 + JSONL trace 调试
- **A**：
  - 用 `fastmcp` 写 server + client，**Agent 通过 MCP 协议动态拉取工具**（一份 Agent 代码能接 N 个 server）
  - 14 case × 8 类场景的 Eval 框架：含 A 参数层（脏参数拦截）/ B 行为层（刁钻输入应对）/ C 安全层（**提示注入 + 越权检测**——99% Agent 教程不讲）
  - JSONL trace（每行 flush，崩了不丢）+ 自动分析器（**报"末轮输入 > 首轮"自动建议 prompt caching**）
- **R**：核心指标 14/14 通过；但**自我批判「100% 不能直接采信」**——14 个 case 都自己设计、mock 数据太规整、没有并发测试（评估的反面不是"通过"是"覆盖"）

### 5. 单 Agent 进阶（流式响应 + 长期记忆）

**栈**: Python · OpenAI SDK · DeepSeek

- **S**：基础 Agent 一次性问答，进阶要能多轮对话 + 流式输出 + 跨会话记忆
- **T**：实现可上线的单 Agent 聊天系统
- **A**：
  - **流式响应**：手动按 `index` 累积 `tool_calls` 分片（id/name 只在第一段、`arguments` 必须 `+=` 拼接）——这是项目最隐蔽的坑
  - 长期记忆**三层结构**：短期 messages（进程内）/ 长期 JSON（跨会话）/ 原始数据（事实来源）
  - 强/弱相关分离（单一阈值做不了二分类——调阈值只能解一头）+ 衰减遗忘
  - `reasoning_content` 必须回传——思考模式下丢了就 400
- **R**：3 层指代全理解（"他们"指上文客户 / "张伟"指上文联系人 / "具体数据"指上文数字）；跨会话回忆测试通过

---

## 工作经历

**XX 咨询**（20XX.XX – 至今）

- 咨询顾问 / 高级顾问
- 客户行业 / 项目经验（按实际情况填写：可填行业领域、做过的代表项目类型、积累的沟通/项目管理能力）
- *此处展示乙方咨询经验如何迁移到 FDE 角色：复杂需求拆解、客户期望管理、按期交付*

---

## 教育背景

**XX 大学** · XX 专业 · 本科 / 硕士（20XX – 20XX）

---

## 开源贡献 / 作品

| 链接                                                                                | 说明                                       |
| --------------------------------------------------------------------------------- | ---------------------------------------- |
| [orange1029/meeting-prep-agent](https://github.com/orange1029/meeting-prep-agent) | 独立开源项目：会前简报 Agent（已上线 GitHub）            |
| LangGraph PR #9319                                                                | 改 `KeyError` 为 `ValueError` 含上下文 + 8 个测试 |
| `orange1029/FDE-Agent-Learning`                                                   | 完整学习路径 4 天代码（LEARNING_JOURNEY.md 5 步路线图） |

---

## 简历使用说明

1. **基础信息**：把"orange1029"替换为真实 GitHub 用户名（已确认是 orange1029）；邮箱 / 电话填真实的
2. **工作经历**：填你之前咨询公司 + 客户行业 + 2-3 个代表项目（**重点：让招聘方看到「咨询经验能迁移到 FDE」**）
3. **教育背景**：填你真实的学校
4. **投递目标**：
   - **Agent 工程师** / **AI 应用工程师** / **FDE** / **AI 产品工程师**
   - 推荐公司：Anthropic、OpenAI 生态（Cursor / Cognition / Codeium）、Salesforce、字节跳动 AI、阿里通义、智谱 AI、出门问问、月之暗面、深势科技

---

## 简历亮点（3 个数字背书）

| 数据        | 怎么来的                                               |
| --------- | -------------------------------------------------- |
| **41/41** | `tools_impl.py` 工具自测 41 项全过                        |
| **26/26** | `test_errors.py` 错误注入测试 26 case 全过（含 C 组安全测试）      |
| **14/14** | `eval_mcp_agent.py` MCP Agent 评估 14 case × 8 类场景全过 |

---

> **简历口诀**：STAR 法则 = Situation（情境）/ Task（任务）/ Action（行动）/ Result（结果）。  
> 每条项目**必须包含可量化的 R**——"7-8 秒跑完" "14/14 通过" "41/41 全过"，不要只写"实现了 XXX 功能"。
