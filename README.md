# 会前简报 Agent · Meeting Prep Agent

> 售前顾问的会前准备助手。输入客户名 + 议题，30 秒拿到一页会前就能读完的简报。

```bash
python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "数据中台建设"
```
```
✓ 已保存：briefings/招商银行_2026-10-13.md（8.7s）
```

---

## 问题

售前顾问见客户前要做三件事：**查客户档案**、**看行业在发生什么**、**回忆上次聊到哪了**。

这三件事散落在 CRM（客户关系管理系统）、新闻网站和自己的记忆里。凑齐要花半小时，还经常漏——
最要命的是**漏掉历史沟通**，见面时把客户已经说过的要求重新问一遍。

## 做法

把这个流程自动化，产出一页 Markdown（标记文本语言）简报，四段固定结构：

```markdown
# 招商银行 · 数据中台建设 会前简报

## 一、客户档案
**招商银行**（C-013）
| 项目 | 内容 |
|---|---|
| 联系人 | 王志华 |
| 邮箱 | wangzhihua@cmbchina.com |

## 二、行业趋势（近 30 天）
- **央行发布金融数据基础设施指引**（2026-10-10，影响：高）
  要求 2027 年底前建成统一数据底座，直接影响银行数据中台采购节奏。

## 三、历史沟通
**已承诺事项**：
- 上次承诺：两周内提供数据中台建设方案与报价

## 四、本次沟通建议
- 开场切入：以央行指引起头，对照其「Oracle + 自研报表、未建数据底座」的现状…
- 本次必须确认（遗留问题）：一期建设范围——存档明确「尚未明确，需会上确认」
```

第四段不是泛泛而谈的模板，它**直接引用了前三段的事实**（"上次承诺两周内交方案"、
"遗留问题待确认"）——这是简报唯一需要主观判断的部分，所以只让模型写这一段。

---

## 核心设计：确定性流程用确定性代码

这是本项目最值得展开讲的一条取舍。

| 部分 | 由谁做 | 为什么 |
|---|---|---|
| 调用哪些工具 | **代码** | 简报的数据需求固定三个，让模型选会漏 |
| 工具参数 | **代码** | 参数已知（客户名 → 行业），模型填反而可能错 |
| 前三段格式 | **代码** | 模板化保证格式稳定，不漂移 |
| 缺失数据标注 | **代码** | 必须在代码里强制，不靠提示词祈祷 |
| 第四段建议 | **模型** | 唯一需要主观判断的部分 |

这样做有一个关键收益：**数据缺失时「⚠ 无历史记录」是代码写死的，模型没有任何机会用推测填补它。**

会前简报里一句编造的「上次王总同意了这个方案」可能直接造成商务事故。
凡是涉及**对外承诺**的事，就不能交给一个会概率性编造的组件。

---

## 技术栈

| 层| 选型 | 说明 |
|---|---|---|
| 工具协议 | MCP（Model Context Protocol，模型上下文协议）| Agent 代码里无任何硬编码工具定义，换数据源不改 Agent |
| 工具定义 | OpenAI Function Calling（函数调用）格式 | DeepSeek / GPT / Qwen 三家都兼容，一份代码跑多个模型 |
| 模型 | DeepSeek | 中文强、OpenAI 兼容、成本约为同级模型的 1/35 |
| 记忆 | 本地 JSON + 关键词检索 | 跨会话，含强弱相关分离与衰减遗忘 |

工具分两层：
- `prep_tools_schema.py` / `prep_tools_impl.py` —— 简报专用的 2 个工具（行业新闻、历史沟通）
- `tools_schema.py` / `tools_impl.py` —— 4 个业务工具（客户、成交、金额计算、发邮件）

历史沟通**真的读项目的长期记忆库**（`memory/memories.json`），不是另一份 mock 数据——
否则它和真实记忆系统就是两套割裂的东西，演示价值为零。

---

## 安装

需要 Python 3.10+。

```bash
git clone https://github.com/orange1029/meeting-prep-agent.git
cd meeting-prep-agent

python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
```

配置密钥（复制模板并填入）：
```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-xxx
```
密钥在 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请。

---

## 使用

```bash
# 基本用法
python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "数据中台建设"

# 让模型自己决定调哪些工具（对比模式）
python meeting_prep_agent.py --client "平安银行" --industry "金融" --topic "实时风控" --mode agentic

# 自定义输出路径
python meeting_prep_agent.py --client "兴业银行" --industry "金融" --topic "数据治理" --out ./draft.md
```

**参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| `--client` | 是 | 客户名称，支持别名（「招行」→「招商银行」）|
| `--industry` | 是 | 行业名称，支持别名（「银行」→「金融」）|
| `--topic` | 是 | 本次会议议题 |
| `--mode` | 否 | `template`（默认）代码控流程 / `agentic` 模型自主调工具 |
| `--out` | 否 | 自定义输出路径 |

输出文件名自动带日期：`briefings/{客户}_{日期}.md`

### 两种模式的区别

| | `template`（默认） | `agentic` |
|---|---|---|
| 谁决定调什么工具 | 代码固定调 3 次 | 模型自己决定 |
| 数据完整性 | 必齐（缺也显示「⚠ 缺失」）| 可能漏查 |
| 格式稳定性 | 固定四段 | 模型自由发挥，会漂移 |
| token 消耗 | 可控（约 1500）| 不确定 |
| 灵活性 | 采集项写死在代码里 | 可按场景调整 |

**建议默认用 `template`**。会前简报的数据需求是固定的三个，
让模型决定会引入「少查一个」的风险。`agentic` 适合简报要素不固定的场景。

---

## 数据来源与可信度

| 数据 | 来源 | 可信度 |
|---|---|---|
| 客户档案 | `tools_impl.py` 的 mock 客户库（14 家）| **模拟** |
| 行业动态 | `prep_tools_impl.py` 的 mock 新闻库（12 条，6 个行业）| **模拟** |
| 历史沟通 | **`memory/memories.json`（真实长期记忆库）** | 真实 |

**为什么敢用 mock 演示**：
数据层与工具层是分离的——换真实系统只需替换数据层的函数体，
Schema（工具定义）和 Agent 循环一行都不用改。行业库按 `days_ago`（相对天数）存储、
运行时换算成实际日期，所以 mock 数据不会随时间推移全部「过期」。

行业别名匹配：
```
金融 ← 银行、保险、证券、基金
医疗 ← 医院、制药、生物
零售 ← 超市、电商、连锁、商场
```

---

## 验证

```bash
# 各模块自测（不调 API，秒级）
python prep_tools_schema.py       # 工具定义格式校验
python prep_tools_impl.py         # 简报工具逻辑自测
python tools_impl.py              # 业务工具自测（41 项）
python trace_logger.py            # 链路追踪格式自测
python test_prep_tools.py         # 简报链路测试（100 项，不调 API）
python test_evals_config.py       # 评估用例配置自检（29 项，不调 API）

# 需要 API Key 的
python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "测试"

# 全部测试
pytest test_deepseek.py -q
```

**README 里承诺的命令全部实跑验证过。** 文档说假话比没文档更糟——
如果你发现某条跑不通，那是代码的问题，不是文档的问题。

### 简报链路有自己的测试

`test_prep_tools.py` 覆盖的是**会前简报这条链路**——
之前仓库里的测试全都只覆盖通用工具那套，简报本体（产品本体）
一个用例都没有。它已经修过 4 个真 bug（tag 重复 / 空参数 / 别名映射 /
无历史时编造），却零回归保护。

这个文件是纯逻辑测试，**不调 API、秒级跑完、不花钱**。
它锁住的关键行为：

| 行为 | 为什么重要 |
|---|---|
| 客户名分级匹配（精确 → 客户号 → 别名 → 子串） | 「银行」不该命中「深圳平安银行」——错的档案比没档案危险 |
| 行业填错时档案不能丢 | 客户名是精确锚点，不该被行业过滤掉 |
| 行业填错时给出 ⚠ 提示 | 趋势是按用户填的行业查的，填错了顾问得知道 |
| 记忆库无记录时诚实返回 | 不能编造过往会议 |
| 内部记忆 ID 不外泄 | 顾问看到「M0003 遗留的一期建设范围」只会以为简报没写完 |

### 评估用例在 YAML 里

`eval_mcp_agent.py` 的 14 个用例在 `evals/mcp_agent_cases.yaml`，
加用例改文件不改代码。`evals/baseline.json` 是入库的基线，
每次跑评估自动对比，**报出「上次通过、这次不通过」的回归用例**。

```bash
# 跑评估（自动与基线对比）
.venv-agents/Scripts/python.exe eval_mcp_agent.py

# 确认结果没问题后刷新基线
.venv-agents/Scripts/python.exe eval_mcp_agent.py --save-baseline
```

`eval_result.json` 每次跑都变（它在 `.gitignore` 里，这是对的——
一次运行的产物不该入库），但没有它就没法看退步，所以另有一份入库的基线。

---

## 已知限制

1. **客户库和新闻库是模拟数据** —— 接真实系统需替换数据层（架构已为此预留替换点）
2. **行业动态只覆盖 6 个行业** —— 查其他行业会明确返回「无数据」并列出可查行业，不返回空列表
3. **历史沟通依赖记忆库内容** —— 记忆库为空时显示「⚠ 首次接触或未存档」，
   这是正确行为，但意味着演示效果取决于记忆库里有没有相关记录
4. **客户名匹配是字面的，不是语义** —— 支持别名表（招行→招商银行）和子串匹配，
   但「招银银行」这种错字认不出，会明确报「未找到」
5. **单次运行，无并发** —— 真实场景需要考虑多个顾问同时查询

---

## 仓库里还有什么

会前简报是这个仓库里最完整的一个实战案例。同仓库还包含支撑它的完整技术栈，
以及另外两个多 Agent（智能体）框架的实测对比：

| 模块 | 内容 |
|---|---|
| **单 Agent（智能体）** | `tools_schema.py` / `tools_impl.py` / `run_agent.py` / `run_chat.py`（含流式响应）|
| **长期记忆** | `memory_store.py`（跨会话 + 强弱相关分离 + 衰减遗忘）|
| **链路追踪** | `trace_logger.py` / `trace_analyze.py`（JSONL 格式，自动定位瓶颈）|
| **评估体系** | `eval_mcp_agent.py` + `evals/`（14 用例 × 8 类场景，YAML 外置 + 基线对比）|
| **多 Agent 协作** | `multi_agent_graph.py`（LangGraph）/ `demo_autogen.py` / `demo_crewai.py` |
| **错误注入测试** | `test_errors.py`（26 例，含提示注入与越权检测）|
| **简报链路测试** | `test_prep_tools.py`（100 项，不调 API）|

配套文档：
- `LEARNING_JOURNEY.md` —— 从零到能交付 Agent 的完整路径复盘（含每步的目的与踩坑）
- `FRAMEWORK_COMPARISON.md` —— LangGraph / AutoGen / CrewAI 三框架实测对比
- `EVAL_REPORT.md` —— Agent 评估报告（含「100% 为什么不能直接采信」）
- `NOTES.md` —— 关键收获与踩坑记录

---

## License

MIT

---

<div align="center">
<sub>⚠ 本工具生成的简报仅供会前参考，带 ⚠ 的部分表示数据缺失，请人工核实。</sub>
</div>
