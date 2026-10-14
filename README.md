# 会前简报 Agent

输入一个客户名，30 秒拿到一页会前就能读完的 markdown 简报。

```bash
python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "数据中台建设"
```

```
✓ 已保存：briefings/招商银行_2026-10-13.md（8.7s）
```

---

## 这是什么

一个**售前顾问的会前准备助手**。

顾问见客户前通常要做三件事：查客户档案、看行业在发生什么、回忆上次聊到哪了。
这三件事分散在 CRM、新闻网站和自己的记忆里，凑齐要花半小时，还经常漏。

这个工具把这三件事自动化，产出一页简报：

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

最后那段建议是**基于前三段的事实生成的**，不是泛泛而谈。
它直接引用了存档里的「上次承诺两周内交方案」和「遗留问题待确认」。

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

配置 API Key（复制模板并填入）：

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

# 让 LLM 自己决定调哪些工具（对比模式）
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
| `--mode` | 否 | `template`（默认）代码控流程 / `agentic` LLM 自主调工具 |
| `--out` | 否 | 自定义输出路径 |

输出文件名自动带日期：`briefings/{客户}_{日期}.md`

### 两种模式的区别

| | `template`（默认） | `agentic` |
|---|---|---|
| 谁决定调什么工具 | 代码固定调 3 次 | LLM 自己决定 |
| 数据完整性 | 必齐（缺也显示「⚠ 缺失」）| 可能漏查 |
| 格式稳定性 | 固定四段 | LLM 自由发挥，会漂移 |
| token 消耗 | 可控（约 1500）| 不确定 |
| 灵活性 | 采集项写死在代码里 | 可按场景调整 |

**建议默认用 `template`**。会前简报的数据需求是固定的三个，
让 LLM 决定会引入「少查一个」的风险。`agentic` 适合简报要素不固定的场景。

---

## 架构

```
┌──────────────────────────────────────────────┐
│  meeting_prep_agent.py（主程序）              │
│                                              │
│  ① collect_data()  ← 代码固定采集三份数据     │
│         ↓                                    │
│     query_customers    客户档案               │
│     search_industry_news  行业动态            │
│     get_meeting_history   历史沟通            │
│         ↓                                    │
│  ② build_blocks()   ← 模板渲染前三段         │
│         ↓                                    │
│  ③ generate_suggestion() ← LLM 只写第四段     │
│         ↓                                    │
│     briefings/{客户}_{日期}.md                │
└──────────────────────────────────────────────┘
```

### 为什么大部分逻辑用代码而不是 LLM

**这是本项目最核心的设计取舍**，值得展开说：

| 部分 | 由谁做 | 为什么 |
|---|---|---|
| 调用哪些工具 | **代码** | 数据需求固定，让 LLM 选会漏 |
| 工具参数 | **代码** | 参数已知（客户名=industry），LLM 填反而可能错 |
| 前三段格式 | **代码** | 模板化保证格式稳定，不漂移 |
| 缺失数据标注 | **代码** | 必须在代码里强制，不靠 prompt 祈祷 |
| 第四段建议 | **LLM** | 这是唯一需要主观判断的部分 |

一句话：**确定性流程用确定性代码，只把真正需要判断的部分交给 LLM。**

这样做的额外好处：数据缺失时「⚠ 无历史记录」是代码写死的，
LLM 没有任何机会用推测填补它。会前简报里一句编造的「上次王总同意了这个方案」
可能直接造成商务事故。

---

## 数据来源

| 数据 | 来源 | 可信度 |
|---|---|---|
| 客户档案 | `tools_impl.py` 的 mock 客户库（14 家）| 模拟 |
| 行业动态 | `prep_tools_impl.py` 的 mock 新闻库（12 条，6 个行业）| 模拟 |
| 历史沟通 | **`memory/memories.json`（真实长期记忆库）** | 真实 |

历史沟通是**真的**读项目的记忆模块，不是另一份 mock。
否则它和真实记忆系统就是两套割裂的东西，演示价值为零。

### 行业动态的 mock 数据

按 `days_ago` 相对天数存储，运行时换算成实际日期，
所以 mock 数据不会随时间推移全部「过期」。

支持别名匹配：

```
金融 ← 银行、保险、证券、基金
医疗 ← 医院、制药、生物
零售 ← 超市、电商、连锁、商场
```

---

## 项目背景

这个仓库是一个 FDE（Forward Deployed Engineer）学习项目，包含了从
「调通一个 LLM API」到「构建多 Agent 系统」的完整学习路径。
本项目（会前简报）是其中一个独立的实战案例。

完整项目结构：

| 文件 | 作用 |
|---|---|
| `meeting_prep_agent.py` | **本项目**：会前简报主程序 |
| `prep_tools_schema.py` | 简报专用 2 工具的 Schema |
| `prep_tools_impl.py` | 简报工具实现 |
| `memory_store.py` | 长期记忆存储层（跨会话）|
| `tools_schema.py` / `tools_impl.py` | 4 个业务工具的 Schema 与实现 |
| `mcp_server.py` / `mcp_client_demo.py` | MCP（模型上下文协议）Server 与 Client |
| `multi_agent_graph.py` | LangGraph 三 Agent 协作 |
| `demo_autogen.py` / `demo_crewai.py` | AutoGen / CrewAI 最小 Demo |
| `trace_logger.py` / `trace_analyze.py` | 链路追踪与分析 |
| `test_*.py` | 各模块测试 |

配套文档：`NOTES.md`（收获总结）、`FRAMEWORK_COMPARISON.md`（三框架实测对比）、
`EVAL_REPORT.md`（MCP Agent 评估报告）。

---

## 开发

```bash
# 各模块自测（不调 API，秒级）
python prep_tools_schema.py       # Schema 格式校验
python prep_tools_impl.py         # 工具逻辑自测
python tools_impl.py              # 业务工具自测（41 项）
python trace_logger.py            # trace 格式自测

# 需要 API Key 的
python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "测试"

# 跑全部测试
pytest test_deepseek.py -q
```

---

## 已知限制

1. **数据全是 mock** —— 客户库和新闻库是模拟的，接真实系统需替换数据层
2. **行业动态只覆盖 6 个行业** —— 查其他行业会明确返回「无数据」并列出可查行业
3. **历史沟通依赖记忆库内容** —— 记忆库为空时会显示「首次接触」，
   这是正确行为，但意味着 demo 效果取决于记忆库里有没有相关记录
4. **关键词匹配而非语义检索** —— 客户名匹配是字符串包含，
   客户名歧义大时可能漏匹配
5. **单次运行，无并发** —— 真实场景需要考虑多个顾问同时查

---

## License

MIT

---

<div align="center">
<sub>⚠ 本工具生成的简报仅供会前参考，带 ⚠ 的部分表示数据缺失，请人工核实。</sub>
</div>