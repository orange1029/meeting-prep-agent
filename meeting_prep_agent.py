"""
meeting_prep_agent.py — 会前简报 Agent

输入客户 + 行业 + 议题，自动产出一页markdown（轻量标记语言）简报。

用法：
    python meeting_prep_agent.py --client "招商银行" --industry "金融" --topic "数据中台建设"
    python meeting_prep_agent.py --client "平安银行" --industry "金融" --topic "实时风控" --no-stream

输出：
    briefings/招商银行_2026-10-13.md

设计取舍（三个刻意的选择）：

1. **不用流式**做默认输出。
   流式在这里体验很差——简报是长markdown，终端里逐字打印没法看。
   真正该流式的是「进度」（正在查客户档案…），所以这里保留进度输出，
   正文一次性打印。加 --no-stream 可关掉进度。

2. **简报模板是固定的，LLM 只负责填内容。**
   让 LLM 自由生成整篇 markdown 会出现格式漂移（这次有表格下次没表格），
   且没法在模板里强制「无历史记录必须显式说明」这类硬要求。
   所以模板由代码控制，LLM 只填四个段落。

3. **强制诚实检查。**
   如果某个数据源返回了 error 或空，简报里必须显式写明，
   不能让 LLM 用推测填补。这一步在代码里做，不靠 prompt 祈祷。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

from prep_tools_impl import find_customer, run_prep_tool
from prep_tools_schema import ALL_PREP_TOOLS


# ============ Windows 中文控制台编码处理 ============
# ★ 这是交付给客户时踩过的真坑：
#   打包成 exe 在客户中文 Windows 上跑，直接崩：
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
#   原因：Windows 控制台默认 GBK 编码，而程序里用了 ✓ ⚠ ▶ 这些符号，
#   GBK 编不出来。源码运行时因为终端是 UTF-8 所以没事，**打包后才暴露**。
#
#   而且中文输出也会变乱码。
#
# 修法：启动时把标准输出强制成 UTF-8。
#   reconfigure 是 Python 3.7+ 的方法，对已创建的流也生效。
#   必须在任何 print 之前做。
def _修控制台编码():
    for 流 in (sys.stdout, sys.stderr):
        if 流 is None:
            continue
        try:
            流.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 老 Python 或特殊流（被重定向到管道等）可能不支持，忽略
            pass
    # Windows 控制台要额外设代码页，否则 reconfigure 也可能不够
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # UTF-8
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


_修控制台编码()

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
BRIEFING_DIR = "briefings"
MAX_ROUNDS = 6

SYSTEM_PROMPT = (
    "你是一名资深售前顾问的助手，负责在会面客户前整理简报。"
    "工作准则："
    "1. 只使用工具返回的数据，绝不编造人名、数字、时间点。"
    "2. 工具返回 error 时，不要猜测内容，直接说明该信息暂缺及原因。"
    "3. 找不到历史沟通记录时，明确写「无历史记录（首次接触或未存档）」，"
    "不要虚构过往会议。"
    "4. 输出简洁、信息密度高，顾问要在会前 3 分钟读完。"
    "5. 引用历史沟通时用可读标签（如「招商银行·遗留问题」），"
    "不要出现 M0003 这类内部编号——那是系统内部主键，顾问看不懂。"
)

# 历史沟通的分类顺序。渲染简报和喂给 LLM 用同一份，避免两处说法不一致。
# 键名对应 get_meeting_history 返回的字段名。
HISTORY_GROUPS = [
    ("过往会议", "meetings"),
    ("已承诺事项", "commitments"),
    ("遗留问题", "open_issues"),
    ("客户关注点", "focus"),
    ("其他记录", "others"),
]


# ============ 数据采集 ============

def collect_data(client, industry, topic, client_llm, trace_progress=True):
    """
    第一步：直接把三个工具各调一次，把原始数据拉齐。

    为什么不靠 LLM 自己去调工具？
    因为简报的**数据需求是固定的**（档案/趋势/历史各一份），
    让 LLM 决定调什么，会出现「少查一个」或「重复查」的情况，
    而这三个数据缺任何一个简报都不完整。
    确定性的流程就用确定性代码，把 LLM 留给「怎么写」。

    但注意：这里仍然保留 LLM 自主调工具的版本（见 run_agentic），
    两者的差别在报告里说明了。
    """
    p = (lambda s: print(f"  · {s}", flush=True)) if trace_progress \
        else (lambda s: None)

    data = {}

    p("查客户档案…")
    # 客户档案用「客户名」找，不按行业过滤。
    #
    # 为什么要分两步：
    #   第一步按客户名找目标客户 —— 客户名是精确锚点，
    #     用行业去筛会误杀：用户把「金融」写成「银行」、或客户跨行业时，
    #     档案就整个丢了（实测 --client 招商银行 --industry 医疗 → 找不到）
    #   第二步再按行业取同类客户 —— 这才是「同行业参考客户」的用途
    #
    # 两个查询都拉全量（limit=100），在内存里筛，
    # 免得因为 mock 数据少而被 limit 截断。
    r = run_prep_tool("query_customers", {"limit": 100})
    all_customers = r.get("customers", []) if "error" not in r else []
    data["profile_error"] = r.get("error")

    target = find_customer(client, all_customers)
    data["customer"] = target

    # 同行业参考客户：优先按目标客户自己的行业取，
    # 目标客户没找到时退回用户给的行业
    peer_industry = (target.get("industry") if target else industry) or ""
    peers = [c for c in all_customers
             if c is not target and c.get("industry") == peer_industry]
    data["peers"] = peers
    p(f"命中 {len(all_customers)} 家，识别出目标客户："
      f"{target['name'] if target else '（档案中未找到）'}")

    p("查行业趋势…")
    r = run_prep_tool("search_industry_news", {"industry": industry, "days": 30})
    data["news_error"] = r.get("error")
    data["news"] = r.get("news", [])
    p(f"近 30 天 {data['news'].__len__()} 条行业动态")

    p("查历史沟通…")
    r = run_prep_tool("get_meeting_history", {"client_name": client})
    data["history_error"] = r.get("error")
    data["history"] = r
    p(f"历史记录 {r.get('count', 0)} 条"
      + ("（首次接触）" if not r.get("has_history") else ""))

    return data


# ============ 简报生成 ============

BRIEF_TEMPLATE = """# {client} · {topic} 会前简报

> 生成时间：{today} ｜ 数据来源：CRM 系统 + 行业情报库 + 长期记忆

## 一、客户档案

{profile_block}

## 二、行业趋势（近 30 天）

{news_block}

## 三、历史沟通

{history_block}

## 四、本次沟通建议

{suggestion_block}

---
<sub>本简报由 AI 生成，仅供会前参考。带 ⚠ 的部分表示数据缺失，请人工核实。</sub>
"""


def _block(items, fmt, empty_note):
    """把数据渲染成 markdown 列表，空时给出明确说明"""
    if not items:
        return f"⚠ {empty_note}"
    return "\n".join(fmt(x) for x in items)


def build_blocks(data, client, industry, topic):
    """构造四个段落的markdown。模板由代码控制，保证格式稳定"""

    # --- 档案 ---
    c = data.get("customer")
    if c:
        profile = (f"**{c['name']}**（{c['id']}）\n\n"
                   f"| 项目 | 内容 |\n|---|---|\n"
                   f"| 行业 | {c.get('industry', '—')} |\n"
                   f"| 城市 | {c.get('city', '—')} |\n"
                   f"| 联系人 | {c.get('contact', '—')} |\n"
                   f"| 邮箱 | {c.get('email', '—')} |\n")
        if data.get("peers"):
            profile += (f"\n**同行业参考客户**（{len(data['peers'])} 家）："
                        + "、".join(x["name"] for x in data["peers"][:4]))
        # 用户给的行业和档案里的不一致时，明确提示。
        # 行业错了会影响第二段行业趋势（按用户给的行业查的），
        # 不提示的话顾问会拿着错的行业情报去开会。
        actual = c.get("industry")
        if actual and industry and actual != industry:
            profile += (f"\n\n⚠ 你填的行业是「{industry}」，"
                        f"但「{c['name']}」档案里是「{actual}」。"
                        f"下面的行业趋势按「{industry}」查询，"
                        f"若不对请重新生成。")
    else:
        profile = (f"⚠ 档案库中未找到「{client}」。"
                   + (f"（检索错误：{data.get('profile_error')}）"
                      if data.get("profile_error") else "")
                   + "请人工核对客户名称，或确认该客户是否已建档。")

    # --- 行业趋势 ---
    news = data.get("news") or []
    news_block = _block(
        news,
        lambda n: (f"- **{n['title']}**（{n['date']}，影响：{n['impact']}）\n"
                   f"  {n['summary']}　<sub>来源：{n['source']}</sub>"),
        f"未获取到{industry}行业近 30 天的动态情报。"
        + (f"（原因：{data.get('news_error')}）" if data.get("news_error") else "")
    )

    # --- 历史沟通 ---
    h = data.get("history") or {}
    if not h.get("has_history"):
        hist = ("⚠ **无历史记录**（首次接触或过往沟通未存档）\n\n"
                f"> {h.get('message', '')}\n\n"
                "**建议**：本次会议需覆盖基础情况介绍，"
                "并主动确认对方的现状与痛点。")
    else:
        parts = [f"记忆库中命中 {h['count']} 条相关记录。"]
        for label, key in HISTORY_GROUPS:
            if h.get(key):
                parts.append(f"\n**{label}**：")
                # 记忆内容理论上不含内部编号，但纵深防御：
                # 万一某条记忆正文里写了「见 M0003」，不该漏到顾问眼前
                parts.extend(f"- {_clean_memory_ids(m['content'])}"
                             for m in h[key])
        hist = "\n".join(parts)

    return profile, news_block, hist


def _clean_memory_ids(text):
    """把记忆内部编号（M0003 这种）从文本里擦掉。

    为什么需要这个：
    记忆库里每条记录有形如 M0003 的内部编号。那是存储层的主键，
    对顾问毫无意义——顾问看到「M0003 遗留的一期建设范围」根本不知道
    那是什么，只会觉得简报没写完。

    LLM 之所以会引用这些编号，是因为它拿到的原始记忆 JSON 里带着
    id 字段。修法是双保险：
      1. 喂事实时不喂 id（见 _history_facts）
      2. 生成后再扫一遍，兜住漏网的引用

    注意只擦 M+数字 这种形态，且要求前后不是字母数字，
    避免误伤正常文本（比如 "M30 机型"）。

    擦完还要收拾残局：空括号「（）」和多余空格。
    """
    s = re.sub(r"(?<![A-Za-z0-9])M\d{3,4}(?![A-Za-z0-9])", "", text or "")
    # 编号被擦掉后可能留下空括号
    s = re.sub(r"[（(]\s*[）)]", "", s)
    # 连续空格压成一个，行首的删掉
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _history_facts(history):
    """把历史沟通整理成给 LLM 看的可读事实列表。

    原始记忆的 content 字段本身是干净的（不含 ID），
    但 id 字段会被 LLM 顺手引用。所以这里只给「内容 + 可读标签」，
    不给 id。

    标签用「客户名·分类」而不是编号，这样 LLM 引用时写出来的是
    「招商银行·遗留问题」，顾问一看就懂。
    """
    h = history or {}
    if not h.get("has_history"):
        return []

    client = h.get("client") or ""
    facts = []
    for label, key in HISTORY_GROUPS:
        for m in (h.get(key) or []):
            content = _clean_memory_ids(m.get("content", ""))
            if not content:
                continue
            facts.append({
                "标签": f"{client}·{label}" if client else label,
                "内容": content,
            })
    return facts


def generate_suggestion(client, industry, topic, data, client_llm):
    """
    第四段「沟通建议」交给 LLM —— 这是唯一需要主观判断的部分。

    把已采集的数据作为事实基础喂给它，并明确列出「哪些是缺失的」，
    让它在缺失的前提下给建议，而不是假装信息完整。
    """
    facts = {
        "客户": data.get("customer"),
        "行业": industry,
        "议题": topic,
        "行业动态": [{"日期": n["date"], "标题": n["title"],
                     "影响": n["impact"], "摘要": n["summary"]}
                    for n in (data.get("news") or [])],
        # 历史沟通用可读标签，不喂原始 id —— 否则 LLM 会引用
        # 「M0003」这种内部编号，顾问看不懂
        "历史沟通": _history_facts(data.get("history")),
        "数据缺失项": [k for k in ("profile_error", "news_error", "history_error")
                       if data.get(k)],
        "是否首次接触": not (data.get("history") or {}).get("has_history", False),
    }

    prompt = (
        f"客户：{client}\n行业：{industry}\n本次议题：{topic}\n\n"
        f"已采集的事实（JSON，只以此为准）：\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
        f"请写一份会前沟通建议，要求：\n"
        f"1. 3-5 条要点，每条一行，以「- 」开头\n"
        f"2. 必须包含：开场切入建议、本次要确认的问题、需要带的数据或材料\n"
        f"3. 若无历史记录，第一条要说明这是首次深度沟通，应侧重建立信任与摸清现状\n"
        f"4. 不要写「建议进一步了解」这类空话，每条都要具体可执行\n"
        f"5. 只用上面事实里有的信息，不要编造客户背景\n"
        f"6. 引用历史沟通时用事实里的「标签」原文（如「招商银行·遗留问题」），"
        f"不要引用任何形如 M0003 的内部编号"
    )

    try:
        r = client_llm.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        # 兜底：即使 LLM 还是吐出了编号（历史记忆 content 里本身可能带），
        # 也在最终输出里擦掉
        return _clean_memory_ids((r.choices[0].message.content or "").strip())
    except Exception as e:
        return f"⚠ 建议生成失败（{type(e).__name__}: {e}），请人工补充。"


# ============ LLM 自主调工具的版本（对比用）============

def run_agentic(client, industry, topic, client_llm, verbose=True):
    """
    另一种做法：让 LLM 自己决定调哪些工具、调几次。

    保留它是为了做对比——同样的任务，两种范式的实际差别：
      collect_data 版：数据必齐、格式稳定、token 可控，但流程写死在代码里
      run_agentic 版：灵活、能处理意外需求，但可能漏查、可能重复查

    生产环境建议用 collect_data 版。agentic 版适合「简报要素不固定」
    的场景（比如要按客户类型调整采集项）。
    """
    question = (
        f"我准备和「{client}」开会，议题是「{topic}」，"
        f"该公司属于{industry}行业。"
        f"请查询所需信息，然后写一份会前简报。"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    rounds = 0
    for _ in range(MAX_ROUNDS):
        resp = client_llm.chat.completions.create(
            model=MODEL, messages=messages,
            tools=ALL_PREP_TOOLS, tool_choice="auto", max_tokens=2000,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", rounds
        rounds += 1
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            if verbose:
                print(f"  · LLM 调用 {name}({args})")
            result = run_prep_tool(name, args)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
    return "（达到最大轮次未完成）", rounds


# ============ 主流程 ============

def main():
    ap = argparse.ArgumentParser(
        description="会前简报 Agent：输入客户/行业/议题，输出一页 markdown 简报")
    # ★ 体检模式不需要必填参数，所以这几个参数不能设 required。
    # argparse 的 required=True 是在解析阶段强制检查的，
    # 而体检模式恰恰要能在「参数不全」的场景下跑起来——
    # 客户现场就是「我啥都没配，你告诉我缺什么」。
    ap.add_argument("--client", help="客户名称，如 招商银行")
    ap.add_argument("--industry", help="行业，如 金融")
    ap.add_argument("--topic", help="本次议题，如 数据中台建设")
    ap.add_argument("--no-stream", action="store_true",
                    help="关闭进度输出（脚本内被调用时用）")
    ap.add_argument("--mode", choices=["template", "agentic"], default="template",
                    help="template=代码控流程(默认)；agentic=LLM 自主调工具")
    ap.add_argument("--out", help="自定义输出路径")
    ap.add_argument("--config", help="指定配置文件路径（默认自动查找 config.toml）")
    ap.add_argument("--check", action="store_true",
                    help="只做部署体检，不跑简报")
    ap.add_argument("--kill-switch", choices=["on", "off", "status"],
                    help="紧急停止开关：off=立刻停用，on=恢复，status=看状态"
                         "（内置到 exe 里，客户手上只有 exe 时也能急停）")
    args = ap.parse_args()

    # ============ 紧急停止开关（内置模式）============
    # ★ 为什么要有这个内置入口：
    #   exe 是发给「客户电脑上没有 Python」的人的。
    #   但急停开关恰恰是这种场景最需要的能力——
    #   如果开关只能靠 kill_switch.py 控制，那客户急停时还得先装 Python，
    #   等于没有开关。所以开关必须和主程序同一个 exe。
    if args.kill_switch:
        import kill_switch
        if args.kill_switch == "status":
            启用, 说明 = kill_switch.读状态()
            print(f"{'✓ 已启用' if 启用 else '⏸ 已停用'}。{说明}")
            sys.exit(0 if 启用 else 3)
        elif args.kill_switch == "off":
            路径 = kill_switch.停用("通过程序内置命令停用")
            print(f"✓ 已停用。开关文件：{路径}")
            print("  恢复：简报助手.exe --kill-switch on")
            sys.exit(0)
        else:
            路径 = kill_switch.启用("通过程序内置命令恢复")
            print(f"✓ 已启用。开关文件：{路径}")
            sys.exit(0)

    # ============ 部署体检模式 ============
    # 客户现场最耗时间的往往不是跑程序，是「到底哪里配错了」。
    # 这个模式一次性把所有配置状态说清楚。
    if args.check:
        import deploy_config
        配置 = deploy_config.读配置(args.config)
        问题, 信息 = deploy_config.检查(配置)
        print("=" * 66)
        print("  部署体检")
        print("=" * 66)
        for 行 in 信息:
            print(f"  {行}")
        if 问题:
            print("\n【发现问题】")
            for 行 in 问题:
                print(f"  ✗ {行}")
            sys.exit(1)
        print("\n  ✓ 一切正常")
        sys.exit(0)

    # 体检之外，三个业务参数必须齐
    缺 = [名 for 名, 值 in (("--client", args.client),
                          ("--industry", args.industry),
                          ("--topic", args.topic)) if not 值]
    if 缺:
        print(f"✗ 缺少必填参数：{' '.join(缺)}")
        print("  例：--client 招商银行 --industry 金融 --topic 数据中台建设")
        sys.exit(1)

    # ============ 紧急停止开关检查 ============
    # ★ 每次运行前都查，不缓存。
    # 客户现场要能「立刻停下来」，所以这个检查必须在最前面，
    # 而且不能因为上次读过就跳过。
    import kill_switch
    启用, 开关说明 = kill_switch.读状态()
    if not 启用:
        print(f"⏸ 当前处于停用状态，未执行任何操作。")
        print(f"  {开关说明}")
        # 提示两种恢复方式：打包成 exe 时用内置命令，
        # 源码运行时用 kill_switch.py（客户电脑上没有 Python 也能用）。
        if getattr(sys, "frozen", False):
            print("  如需恢复：简报助手.exe --kill-switch on")
        else:
            print("  如需恢复：python kill_switch.py on 恢复")
        sys.exit(3)   # 专用退出码，方便编排系统区分「停用」和「出错」

    # ============ 读配置 ============
    # 配置来源：命令行 --config > 环境变量 DEPLOY_CONFIG > 配置文件 > 环境变量 > 默认值
    import deploy_config
    配置 = deploy_config.读配置(args.config)

    if not 配置.get("api_key"):
        print("✗ 未找到大模型 API 密钥")
        print(f"  配置来源：{配置.get('_config_source')}")
        print("  两种配法（任选其一）：")
        print("    ① 设环境变量 DEEPSEEK_API_KEY=sk-xxx")
        print("    ② 在 config.toml 的 [llm] 段加 api_key = \"sk-xxx\"")
        print("  跑体检看详细情况：python meeting_prep_agent.py --check")
        sys.exit(1)

    # 覆盖模块级默认值（读配置后才知道真实值）
    global MODEL, BASE_URL
    MODEL = 配置.get("model", MODEL)
    BASE_URL = 配置.get("base_url", BASE_URL)

    client_llm = OpenAI(api_key=配置["api_key"],
                        base_url=BASE_URL,
                        timeout=配置.get("timeout", 60))
    verbose = not args.no_stream

    print("=" * 66)
    print("  会前简报 Agent")
    print("=" * 66)
    print(f"  客户：{args.client}")
    print(f"  行业：{args.industry}")
    print(f"  议题：{args.topic}")
    print(f"  模式：{args.mode}")
    print(f"  模型：{MODEL}")

    t0 = time.perf_counter()
    if verbose:
        print("\n[1/2] 采集数据")

    data = collect_data(args.client, args.industry, args.topic,
                        client_llm, trace_progress=verbose)

    if verbose:
        print("\n[2/2] 生成简报")

    if args.mode == "agentic":
        content, rounds = run_agentic(args.client, args.industry, args.topic,
                                      client_llm, verbose=verbose)
        dt = time.perf_counter() - t0
        path = save(content, args, rounds=rounds, elapsed=dt)
        print("\n" + "=" * 66)
        print(content)
        print("=" * 66)
        print(f"\n✓ 已保存：{path}（agentic 模式，{rounds} 轮工具调用，"
              f"{dt:.1f}s）")
        return

    # template 模式：模板 + LLM 只填第四段
    profile, news_block, hist = build_blocks(
        data, args.client, args.industry, args.topic)

    suggestion = generate_suggestion(
        args.client, args.industry, args.topic, data, client_llm)

    md = BRIEF_TEMPLATE.format(
        client=args.client,
        topic=args.topic,
        today=date.today().isoformat(),
        profile_block=profile,
        news_block=news_block,
        history_block=hist,
        suggestion_block=suggestion or "⚠ 未生成",
    )

    dt = time.perf_counter() - t0
    path = save(md, args, elapsed=dt)

    print("\n" + "=" * 66)
    print(md)
    print("=" * 66)
    print(f"\n✓ 已保存：{path}（{dt:.1f}s）")


def save(content, args, rounds=None, elapsed=0.0):
    """保存简报。文件名带日期，避免多次运行互相覆盖"""
    # 输出目录走配置（客户可能改成绝对路径，比如客户的共享盘）
    输出目录 = BRIEFING_DIR
    try:
        import deploy_config
        配置 = deploy_config.读配置(getattr(args, "config", None))
        输出目录 = 配置.get("output_dir", BRIEFING_DIR)
    except Exception:
        # 配置读不了就用默认值，别因为这个把整个运行搞挂
        pass

    # 相对路径以「程序所在目录」为基准，不是当前工作目录。
    # 客户双击 exe 时工作目录可能是任意路径，用 cwd 会存到奇怪的地方。
    if not os.path.isabs(输出目录):
        import deploy_config
        基准 = deploy_config._程序目录()
        输出目录 = os.path.join(基准, 输出目录)

    os.makedirs(输出目录, exist_ok=True)
    if args.out:
        path = args.out
    else:
        safe = args.client.replace("/", "_").strip()
        path = os.path.join(输出目录,
                            f"{safe}_{date.today().isoformat()}.md")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


if __name__ == "__main__":
    main()