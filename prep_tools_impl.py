"""
prep_tools_impl.py — 会前简报 2 个新 Tool（工具）的实现

对应 prep_tools_schema.py 里的 Schema。

两个工具的数据来源不同，这是刻意的设计：
  search_industry_news  → mock 数据（行业新闻本来就该接新闻 API，
                          mock 只是为了让demo 能独立跑通）
  get_meeting_history   → **真实读取 memory/memories.json**
                          这是本项目已有的长期记忆模块。
                          会前简报的历史沟通如果靠 mock，
                          那它和真实的记忆系统就是两套割裂的东西，
                          演示价值为零。

错误处理原则（承接 tools_impl.py 的经验）：
  出错返回 {"error": ...}，不抛异常。
  错误信息必须精确到「模型照着能改对」——列出合法值、给出格式示例。
"""

import json
import os
import re
from datetime import date, datetime, timedelta

# ============ mock 行业新闻 ============
# date 字段用相对天数生成，避免 mock 数据随时间推移全部过期。
# anchor_date 是"今天"，运行时计算实际日期。

NEWS_TEMPLATES = [
    {  # 金融
        "industry": "金融", "days_ago": 3,
        "title": "央行发布金融数据基础设施指引，明确数据治理框架要求",
        "impact": "高",
        "summary": "指引要求金融机构 2027 年底前建成统一数据底座，"
                   "直接影响银行的数据中台采购节奏。",
        "source": "央行官网",
    },
    {  # 金融
        "industry": "金融", "days_ago": 8,
        "title": "多家银行启动数据中台二期建设，预算集中在实时风控场景",
        "impact": "高",
        "summary": "已公开招标项目中，约六成将实时决策能力列为第一优先级。",
        "source": "招标网",
    },
    {  # 金融
        "industry": "金融", "days_ago": 15,
        "title": "《数据安全管理办法》修订草案征求意见，新增跨境数据条款",
        "impact": "中",
        "summary": "跨境数据流转需单独申报，银行海外业务数据架构面临调整。",
        "source": "监管公告",
    },
    {  # 金融
        "industry": "金融", "days_ago": 22,
        "title": "同业加速接入 MCP 等 Agent 协议，内部智能体试点扩大",
        "impact": "中",
        "summary": "已有银行将 Agent 接入信贷材料审核流程，准确率提升约 20%。",
        "source": "行业媒体",
    },
    {  # 医疗
        "industry": "医疗", "days_ago": 5,
        "title": "电子病历分级评价新标准落地，2027 年起强制要求",
        "impact": "高",
        "summary": "评价标准从「有无」转向「数据可用性」，推动院内数据治理投入。",
        "source": "卫健委",
    },
    {  # 医疗
        "industry": "医疗", "days_ago": 12,
        "title": "多家三甲医院启动 AI 辅助诊断平台建设",
        "impact": "中",
        "summary": "采购重点从算法模型转向数据治理与集成能力。",
        "source": "医院采购公告",
    },
    {  # 教育
        "industry": "教育", "days_ago": 6,
        "title": "教育信息化 2.0 政策强调数据贯通，禁止校级系统重复建设",
        "impact": "高",
        "summary": "省级统筹成为趋势，厂商需提供跨校数据对接能力。",
        "source": "教育部",
    },
    {  # 教育
        "industry": "教育", "days_ago": 18,
        "title": "高校采购预算向数据治理倾斜，硬件采购占比下降",
        "impact": "中",
        "summary": "采购清单中出现「数据标准制定」「数据质量治理」等新条目。",
        "source": "招标统计",
    },
    {  # 零售
        "industry": "零售", "days_ago": 4,
        "title": "零售连锁企业加速统一会员数据，解决多渠道数据割裂",
        "impact": "高",
        "summary": "线上线下会员 ID 未打通是普遍痛点，数据中台需求集中释放。",
        "source": "行业报告",
    },
    {  # 零售
        "industry": "零售", "days_ago": 20,
        "title": "多家零售企业接入大模型做导购，侧重供应链预测场景",
        "impact": "中",
        "summary": "实际落地以预测类场景为主，生成式对话仍处试点。",
        "source": "连锁协会",
    },
    {  # 互联网
        "industry": "互联网", "days_ago": 7,
        "title": "平台企业收紧数据接口调用，第三方数据获取成本上升",
        "impact": "高",
        "summary": "数据合规成为竞争门槛，拥有自有数据资产的厂商更受青睐。",
        "source": "平台公告",
    },
    {  # 制造
        "industry": "制造", "days_ago": 10,
        "title": "工业企业数据中台建设进入深水区，集成问题占主要抱怨",
        "impact": "中",
        "summary": "存量系统多、标准不一，集成成本可达预算的一半。",
        "source": "制造业协会",
    },
]

# 行业别名，做模糊匹配
INDUSTRY_ALIASES = {
    "银行": ["金融"], "保险": ["金融"], "证券": ["金融"], "基金": ["金融"],
    "医院": ["医疗"], "制药": ["医疗"], "生物": ["医疗"],
    "学校": ["教育"], "高校": ["教育"], "培训": ["教育"],
    "超市": ["零售"], "电商": ["零售"], "连锁": ["零售"], "商场": ["零售"],
    "制造": ["制造", "制造业"], "工厂": ["制造"],
}


def _clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("none", "null", "n/a", "无", "不限", "-") else s


def _resolve_industries(industry: str):
    """
    把用户的行业输入解析成一组标准行业名。
    支持：标准名直接命中、别名映射、模糊包含。
    返回 (标准行业列表, 是否匹配到任何已知行业)
    """
    key = _clean(industry)
    if not key:
        return [], False

    known = {t["industry"] for t in NEWS_TEMPLATES}

    # 1. 精确 / 别名
    if key in known:
        return [key], True
    if key in INDUSTRY_ALIASES:
        return INDUSTRY_ALIASES[key], True
    for alias, targets in INDUSTRY_ALIASES.items():
        if alias in key:
            return targets, True
    # 2. 模糊：已知行业名出现在输入里
    for k in known:
        if k in key or key in k:
            return [k], True
    return [], False


def search_industry_news(industry="", days=30, **kwargs):
    """查询行业近 N 天的关键事件。

    参数
      industry 行业名称，支持别名（银行→金融）
      days     回溯天数，默认 30，范围 1~365

    返回
      {"news": [...], "total": n, "industry": "标准行业名", "days": n}
    """
    industry = _clean(industry)
    if not industry:
        return {"error": "industry 为空，请指定行业名称，如 金融、医疗、教育"}

    try:
        days = int(days if days is not None else 30)
    except (TypeError, ValueError):
        return {"error": f"days 必须是整数，收到 '{days}'"}
    if days < 1 or days > 365:
        return {"error": f"days 取值 {days} 超出范围，请传 1~365 之间的整数"}

    targets, matched = _resolve_industries(industry)

    if not matched:
        # 不认识这个行业 —— 明确说没有，并列出能查哪些。
        # 不要返回空列表让模型自己猜，那样它可能编造新闻。
        available = sorted({t["industry"] for t in NEWS_TEMPLATES})
        return {
            "error": f"没有 '{industry}' 这个行业的新闻数据。"
                     f"当前可查询的行业：{'、'.join(available)}。"
                     f"如需查其他行业，请先补充数据源。",
            "available_industries": available,
        }

    today = date.today()
    out = []
    for tpl in NEWS_TEMPLATES:
        if tpl["industry"] not in targets:
            continue
        actual_date = today - timedelta(days=tpl["days_ago"])
        item = dict(tpl)
        item.pop("days_ago")
        item["date"] = actual_date.isoformat()
        # 真实日期与相对天数的差值超过用户要的回溯窗口，就不算数
        if tpl["days_ago"] > days:
            continue
        out.append(item)

    out.sort(key=lambda x: x["date"], reverse=True)
    return {
        "news": out,
        "total": len(out),
        "industry": "/".join(targets),
        "days": days,
        "date_range": f"{(today - timedelta(days=days)).isoformat()} ~ "
                      f"{today.isoformat()}",
    }


def get_meeting_history(client_name="", **kwargs):
    """查询客户历史沟通摘要。

    数据来自 memory/memories.json（项目的长期记忆库），
    筛出与该客户相关的条目并按时间组织。

    参数
      client_name 客户名，支持部分匹配（招行→招商银行）

    返回
      {"client": 命中客户名, "meetings": [...], "commitments": [...],
       "open_issues": [...], "focus": [...], "count": n}
    """
    client_name = _clean(client_name)
    if not client_name:
        return {"error": "client_name 为空，请传入客户名称"}

    # 延迟导入，避免模块级耦合，也方便测试时替换记忆库路径
    from memory_store import MemoryStore

    store = MemoryStore()

    # 客户名别名，让「招行」能命中「招商银行」
    aliases = {"招行": "招商银行", "招商": "招商银行",
               "平安": "平安银行", "工行": "工商银行", "建行": "建设银行",
               "交行": "交通银行", "中信": "中信银行"}
    matched_name = aliases.get(client_name, client_name)

    # 匹配记忆条目：内容里出现客户名或别名的都算
    related = []
    for m in store.memories:
        text = m["content"]
        tags = " ".join(m.get("tags", []))
        hay = text + " " + tags
        if client_name in hay or (
                matched_name != client_name and matched_name in hay):
            related.append(m)

    if not related:
        # 诚实返回「无历史记录」。这是会前简报里最重要的诚实点——
        # 没有历史沟通就说没有，让分析师去问，而不是编一段。
        return {
            "client": client_name,
            "has_history": False,
            "count": 0,
            "meetings": [],
            "message": f"记忆库中没有找到「{client_name}」的历史沟通记录。"
                       f"这可能意味着：首次接触，或过往记录未被记录。"
                       f"建议在会前确认沟通历史。",
        }

    # 按 tag 分组，但**必须去重**。
    # 第一版直接 `by_tag("会议") + by_tag("沟通")`，结果一条同时打了
    # 两个 tag 的记忆会在「过往会议」里出现两次。
    # 正确做法：一条记忆只归入它最匹配的那一类。
    def take(*tag_groups):
        """按优先级取出匹配任一 tag 的记忆，已取的排除，保证不重复"""
        picked, taken = [], set()
        for tags in tag_groups:
            for m in related:
                if m["id"] in taken:
                    continue
                if set(tags) & set(m.get("tags", [])):
                    picked.append(m)
                    taken.add(m["id"])
        return picked

    meetings = take(("会议",), ("沟通",))
    commitments = take(("承诺",), ("行动项",))
    open_issues = take(("问题",), ("风险",), ("待跟进",))
    focus = take(("关注点",), ("诉求",))
    # 没打任何分类 tag 的，单独归入「其他事实」，避免丢失信息
    classified = {m["id"] for m in
                  meetings + commitments + open_issues + focus}
    others = [m for m in related if m["id"] not in classified]

    return {
        "client": matched_name,
        "has_history": True,
        "count": len(related),
        "meetings": meetings,
        "commitments": commitments,
        "open_issues": open_issues,
        "focus": focus,
        "others": others,
        "all_memories": related,
        "source": store.path,
    }


# ============ 分发 ============
PREP_REGISTRY = {
    "search_industry_news": search_industry_news,
    "get_meeting_history": get_meeting_history,
}


def run_prep_tool(name, arguments):
    """执行简报工具调用。与 tools_impl.run_tool 同风格。"""
    from tools_impl import run_tool

    if name in PREP_REGISTRY:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return {"error": f"参数不是合法 JSON：{e}"}
        if not isinstance(arguments, dict):
            return {"error": f"参数类型错误：期望 dict，实际 {type(arguments).__name__}"}
        # 校验未知参数名——模型传错参数时必须让它知道
        import inspect
        valid = {p for p in inspect.signature(PREP_REGISTRY[name]).parameters
                 if p != "kwargs"}
        unknown = [k for k in arguments if k not in valid]
        if unknown:
            return {"error": f"未知参数：{'、'.join(unknown)}。"
                             f"{name} 接受的参数为：{'、'.join(sorted(valid))}"}
        try:
            return PREP_REGISTRY[name](**arguments)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # 非简报工具走统一分发（query_customers / query_deals 等）
    return run_tool(name, arguments)


if __name__ == "__main__":
    print("=" * 66)
    print("简报工具自测")
    print("=" * 66)

    print("\n【search_industry_news】")
    r = search_industry_news(industry="金融", days=30)
    print(f"  金融 30 天 → {r['total']} 条")
    for n in r["news"][:2]:
        print(f"    [{n['date']}] {n['title'][:38]}")
    print(f"  别名「银行」→ {search_industry_news('银行')['total']} 条")
    print(f"  窗口 7 天 → {search_industry_news('金融', 7)['total']} 条")
    print(f"  未知行业 → {search_industry_news('航空')['error'][:50]}…")
    print(f"  days 越界 → {search_industry_news('金融', 999)['error'][:40]}")
    print(f"  空行业 → {search_industry_news('')['error'][:40]}")

    print("\n【get_meeting_history】")
    r = get_meeting_history(client_name="招商银行")
    if r["has_history"]:
        print(f"  命中 {r['count']} 条记忆")
    else:
        print(f"  无历史（真实记忆库里确实没有）")
        print(f"  提示：{r['message'][:50]}…")
    r = get_meeting_history(client_name="不存在的客户")
    print(f"  未命中客户 → has_history={r['has_history']}")
    print(f"  空参数 → {get_meeting_history('')['error'][:36]}")

    print("\n【分发器】")
    print(f"  简报工具 → {run_prep_tool('search_industry_news', {'industry':'医疗'})['total']} 条")
    print(f"  业务工具 → {run_prep_tool('query_customers', {'industry':'教育','city':'上海'})['total']} 个")
    print(f"  未知工具 → {run_prep_tool('nope', {})['error'][:30]}")

    print("\n✓ 自测完成")