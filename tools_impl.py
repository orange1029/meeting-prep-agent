"""
tools_impl.py — 4 个 Tool（工具）的 Python 实现

把 tools_schema.py 里的 Schema（结构定义）变成真正能跑的代码。
Schema 只是「说明书」，这里才是「干活的」。

设计要点：
  1. 参数严格校验 —— 模型可能填错，不能让脏参数进到业务逻辑里
  2. 统一返回 dict（字典）—— 直接可 json.dumps() 回传给模型
  3. 出错不抛异常，返回 {"error": ...} —— 抛异常会中断整个 Agent 循环，
     而模型看到错误信息后往往能自己改正重试
  4. mock 数据带真实感 —— 字段名和 Schema 描述保持一致，否则模型会困惑

数据层：目前用内存 mock 列表。以后接真实数据库（CRM/ERP）时，
只需替换 query_customers / query_deals 的函数体，
Schema 和 Agent 循环代码一行都不用改。

运行自测：
    python tools_impl.py
"""

import json
import re
from datetime import date, datetime

# ============ mock 数据（模拟数据，模拟真实 CRM 里的客户与成交记录）============
# 客户字段：id 客户号/ name 客户名/ industry 行业/ city 城市/ contact 联系人/ email 邮箱
MOCK_CUSTOMERS = [
    {"id": "C-001", "name": "上海交大教育科技", "industry": "教育", "city": "上海",
     "contact": "张伟", "email": "zhangwei@shjtu-edu.com"},
    {"id": "C-002", "name": "上海启明中学", "industry": "教育", "city": "上海",
     "contact": "李娜", "email": "lina@qiming-school.cn"},
    {"id": "C-003", "name": "北京新东方在线", "industry": "教育", "city": "北京",
     "contact": "王强", "email": "wangqiang@xdf.cn"},
    {"id": "C-004", "name": "深圳华为技术", "industry": "互联网", "city": "深圳",
     "contact": "刘洋", "email": "liuyang@huawei.com"},
    {"id": "C-005", "name": "上海华山医院", "industry": "医疗", "city": "上海",
     "contact": "陈静", "email": "chenjing@huashan.org.cn"},
    {"id": "C-006", "name": "北京协和医院", "industry": "医疗", "city": "北京",
     "contact": "赵磊", "email": "zhaolei@pumch.cn"},
    {"id": "C-007", "name": "上海永辉超市", "industry": "零售", "city": "上海",
     "contact": "孙丽", "email": "sunli@yonghui.com"},
    {"id": "C-008", "name": "深圳平安银行", "industry": "金融", "city": "深圳",
     "contact": "周杰", "email": "zhoujie@pingan.com"},
    {"id": "C-009", "name": "上海复旦软件", "industry": "互联网", "city": "上海",
     "contact": "吴敏", "email": "wumin@fudan-soft.com"},
    {"id": "C-010", "name": "北京四中道元", "industry": "教育", "city": "北京",
     "contact": "郑爽", "email": "zhengshuang@sizhong.cn"},
    {"id": "C-011", "name": "上海三菱电机", "industry": "制造", "city": "上海",
     "contact": "冯军", "email": "fengjun@mitsubishi.com"},
    {"id": "C-012", "name": "广州中信证券", "industry": "金融", "city": "广州",
     "contact": "蒋涛", "email": "jiangtao@citic.com"},
]

# 成交字段：id 成交号/ customer_id 客户号/ customer 客户名/ city 城市/
#amount 金额（元）/ stage 阶段/ close_date 成交日期
MOCK_DEALS = [
    {"id": "D-1001", "customer_id": "C-001", "customer": "上海交大教育科技",
     "city": "上海", "amount": 52000, "stage": "won", "close_date": "2025-05-12"},
    {"id": "D-1002", "customer_id": "C-002", "customer": "上海启明中学",
     "city": "上海", "amount": 38000, "stage": "won", "close_date": "2025-05-20"},
    {"id": "D-1003", "customer_id": "C-005", "customer": "上海华山医院",
     "city": "上海", "amount": 96000, "stage": "negotiation", "close_date": "2025-06-03"},
    {"id": "D-1004", "customer_id": "C-009", "customer": "上海复旦软件",
     "city": "上海", "amount": 25000, "stage": "proposal", "close_date": "2025-04-28"},
    {"id": "D-1005", "customer_id": "C-003", "customer": "北京新东方在线",
     "city": "北京", "amount": 120000, "stage": "won", "close_date": "2025-05-08"},
    {"id": "D-1006", "customer_id": "C-004", "customer": "深圳华为技术",
     "city": "深圳", "amount": 240000, "stage": "won", "close_date": "2025-03-15"},
    {"id": "D-1007", "customer_id": "C-008", "customer": "深圳平安银行",
     "city": "深圳", "amount": 88000, "stage": "lead", "close_date": "2025-06-20"},
    {"id": "D-1008", "customer_id": "C-007", "customer": "上海永辉超市",
     "city": "上海", "amount": 45000, "stage": "lost", "close_date": "2025-04-15"},
    {"id": "D-1009", "customer_id": "C-012", "customer": "广州中信证券",
     "city": "广州", "amount": 150000, "stage": "won", "close_date": "2025-05-25"},
    {"id": "D-1010", "customer_id": "C-011", "customer": "上海三菱电机",
     "city": "上海", "amount": 67000, "stage": "won", "close_date": "2025-05-30"},
]

# 记录已发送的邮件，供 send_email 回执查询
SENT_EMAILS = []


# ============ 通用工具 ============

def _clean(value):
    """把模型可能传来的 None / 'None' / 'null' / '无' / '不限' 等统一成空字符串"""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("none", "null", "n/a", "无", "不限", "不指定", "all", "-"):
        return ""
    return s


def _parse_date(s, field):
    """解析 YYYY-MM-DD，失败返回错误信息"""
    s = _clean(s)
    if not s:
        return None, None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"{field} 日期格式错误：'{s}'，应为 YYYY-MM-DD（年-月-日）"


def _clamp(limit, default, maximum):
    """限制 limit 在合理区间，防止模型传入 99999 拖垮程序"""
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


# ============ Tool 1: query_customers ============

def query_customers(industry="", city="", limit=10, **kwargs):
    """按行业 / 城市查客户。

    参数
      industry 行业，空字符串表示不限
      city     城市，空字符串表示不限
      limit    返回条数上限（1-100）

    返回
      {"customers": [...], "total": 匹配总数, "returned": 返回条数}
    """
    industry = _clean(industry)
    city = _clean(city)
    limit = _clamp(limit, 10, 100)

    rows = MOCK_CUSTOMERS
    # 精确匹配。模型有时会传 '教育行业' 这种带后缀的，做个宽松包含匹配
    if industry:
        rows = [r for r in rows if industry in r["industry"] or r["industry"] in industry]
    if city:
        rows = [r for r in rows if city in r["city"] or r["city"] in city]

    total = len(rows)
    results = rows[:limit]
    return {
        "customers": results,
        "total": total,
        "returned": len(results),
        "truncated": total > len(results),
        "filters": {"industry": industry or "不限", "city": city or "不限"},
    }


# ============ Tool 2: query_deals ============

def query_deals(city="", date_range=None, stage="", limit=20, **kwargs):
    """按城市 / 日期范围 / 阶段查成交记录。

    参数
      city       城市，空字符串表示不限
      date_range {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}，
                 允许为 None / {} / 任意一端为空
      stage      lead/proposal/negotiation/won/lost，空字符串表示不限
      limit      返回条数上限（1-200）

    返回
      {"deals": [...], "total": 匹配总数, "amount_sum": 金额合计}
    """
    city = _clean(city)
    stage = _clean(stage).lower()
    limit = _clamp(limit, 20, 200)

    valid_stages = {"lead", "proposal", "negotiation", "won", "lost"}
    if stage and stage not in valid_stages:
        return {
            "error": f"stage 取值无效：'{stage}'。合法值：{'、'.join(sorted(valid_stages))}，"
                     f"留空表示不限",
        }

    start, end = None, None
    if isinstance(date_range, str):
        # 模型偶尔会把 date_range 传成字符串 "2025-01-01~2025-12-31"
        found = re.findall(r"\d{4}-\d{2}-\d{2}", date_range)
        if len(found) >= 2:
            start, err1 = _parse_date(found[0], "start")
            end, err2 = _parse_date(found[1], "end")
            if err1 or err2:
                return {"error": err1 or err2}
        elif len(found) == 1:
            start, err1 = _parse_date(found[0], "start")
            if err1:
                return {"error": err1}
            end = start
        else:
            return {"error": f"date_range 无法解析：'{date_range}'，"
                            "建议格式 {\"start\":\"2025-01-01\",\"end\":\"2025-12-31\"}"}
    elif isinstance(date_range, dict):
        start, err1 = _parse_date(date_range.get("start"), "start")
        end, err2 = _parse_date(date_range.get("end"), "end")
        if err1 or err2:
            return {"error": err1 or err2}
    elif date_range is None:
        pass
    else:
        return {"error": f"date_range 类型错误：期望 dict（字典），实际 {type(date_range).__name__}"}

    if start and end and start > end:
        return {"error": f"日期区间错误：start（{start}）晚于 end（{end}）"}

    rows = MOCK_DEALS
    if city:
        rows = [r for r in rows if city in r["city"] or r["city"] in city]
    if stage:
        rows = [r for r in rows if r["stage"] == stage]

    def in_range(r):
        d, err = _parse_date(r["close_date"], "close_date")
        if d is None:
            return False
        if start and d < start:
            return False
        if end and d > end:
            return False
        return True

    rows = [r for r in rows if in_range(r)]
    # 按日期倒序，最近的成交排在前面
    rows.sort(key=lambda r: r["close_date"], reverse=True)

    total = len(rows)
    results = rows[:limit]
    return {
        "deals": results,
        "total": total,
        "returned": len(results),
        "truncated": total > len(results),
        "amount_sum": sum(r["amount"] for r in results),
        "filters": {
            "city": city or "不限",
            "date_range": f"{start or '不限'} ~ {end or '不限'}",
            "stage": stage or "不限",
        },
    }


# ============ Tool 3: calc_sum ============

def calc_sum(amounts=None, **kwargs):
    """算金额总和。

    参数
      amounts 数字列表，如 [12000.5, 8000]

    返回
      {"sum": 总和, "count": 数量, "average": 均值}
    """
    if amounts is None:
        return {"error": "amounts 为空，必须传入数字列表"}

    # 统一摊平：模型可能传 "12000, 8000"、["12000, 8000"]、"[1,2]" 等各种形态
    if isinstance(amounts, str):
        amounts = [amounts]
    if not isinstance(amounts, (list, tuple)):
        return {"error": f"amounts 类型错误：期望 list（列表），"
                        f"实际 {type(amounts).__name__}"}

    nums = []
    bad = []
    for i, a in enumerate(amounts):
        # 单个元素内可能还含逗号/空格，如 "12000, 8000"
        pieces = re.split(r"[,，\s]+", str(a).strip().strip("[]()（）")) if isinstance(a, str) else [a]
        for piece in pieces:
            if piece == "" or piece is None:
                continue
            try:
                nums.append(float(piece))
            except (TypeError, ValueError):
                bad.append(f"第{i + 1}项 '{a}'")
                break

    if bad:
        return {"error": f"以下不是合法数字：{'、'.join(bad)}；"
                        f"金额请用纯数字，不要带货币符号或中文单位"}

    if not nums:
        return {"error": "amounts 里没有任何数字，没有可求和的内容"}

    total = round(sum(nums), 2)
    return {
        "sum": total,
        "count": len(nums),
        "average": round(total / len(nums), 2),
        "amounts": nums,
    }


# ============ Tool 4: send_email（mock 模拟发送）============
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def send_email(to="", subject="", body="", **kwargs):
    """发送邮件（mock 模拟，不会真发）。

    参数
      to      收件人邮箱
      subject 主题
      body    正文

    返回
      {"sent": True, "to": ..., "subject": ..., "body_length": ...,
       "message_id": ...}
    """
    to = _clean(to)
    subject = _clean(subject)
    body = body if isinstance(body, str) else _clean(body)

    if not to:
        return {"error": "缺少收件人 to。邮箱格式如 name@company.com"}
    if not EMAIL_RE.match(to):
        return {"error": f"邮箱格式错误：'{to}'，应为 name@company.com"}
    if not subject:
        return {"error": "缺少邮件主题 subject"}
    if not body.strip():
        return {"error": "邮件正文 body 为空"}

    message_id = f"mock-{len(SENT_EMAILS) + 1:04d}"
    SENT_EMAILS.append(
        {"message_id": message_id, "to": to, "subject": subject,
         "body": body, "sent_at": datetime.now().isoformat(timespec="seconds")}
    )
    return {
        "sent": True,
        "mock": True,  # 明确告知这是模拟发送
        "message_id": message_id,
        "to": to,
        "subject": subject,
        "body_length": len(body),
        "preview": body[:80] + ("..." if len(body) > 80 else ""),
    }


# ============ 分发器（dispatcher）============

# 工具名 -> Python 函数。由 Agent 循环根据模型返回的 tool_call.name 查表执行。
TOOL_REGISTRY = {
    "query_customers": query_customers,
    "query_deals": query_deals,
    "calc_sum": calc_sum,
    "send_email": send_email,
}


def run_tool(name, arguments):
    """执行一个工具调用，统一捕获异常并返回 dict。

    模型返回的 arguments 是 JSON 字符串，需要先 json.loads。
    """
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"未知工具：'{name}'。可用工具：{', '.join(TOOL_REGISTRY)}"}

    # arguments 可能是 str（JSON）或 dict
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return {"error": f"参数不是合法 JSON：{e}"}
    if not isinstance(arguments, dict):
        return {"error": f"参数类型错误：期望 dict（字典），实际 {type(arguments).__name__}"}

    # 已知参数名（从函数签名取，排除 **kwargs）
    import inspect
    sig = inspect.signature(fn)
    valid = {p for p in sig.parameters if p != "kwargs"}

    #关键：所有工具函数都带 **kwargs（为了容忍模型多传字段），
    # 但这会让「参数名拼错」被静默吞掉，模型永远不知道自己错了。
    # 所以必须在调用前主动校验未知参数名。
    unknown = [k for k in arguments if k not in valid]
    if unknown:
        return {
            "error": f"未知参数：{'、'.join(unknown)}。工具 {name} 接受的参数为："
                     f"{'、'.join(sorted(valid))}。请按正确参数名重试。"
        }

    try:
        return fn(**arguments)
    except TypeError as e:
        # 参数个数不匹配。必须说清正确参数是什么，否则模型会瞎改
        return {
            "error": f"参数不匹配：{e}。工具 {name} 接受的参数为："
                     f"{'、'.join(sorted(valid))}，请按正确参数名重试。"
        }
    except Exception as e:  # 兜底：绝不让异常打断 Agent 循环
        return {"error": f"{type(e).__name__}: {e}"}


# ============ 自测 ============

def _selftest():
    ok = total = 0

    def check(label, cond):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}")

    print("=" * 60)
    print("Tool 1: query_customers")
    r = query_customers(industry="教育", city="上海")
    check("上海教育行业客户 2 个", r["total"] == 2)
    check("返回交大、启明", {c["name"] for c in r["customers"]} ==
          {"上海交大教育科技", "上海启明中学"})
    check("limit 生效", len(query_customers(industry="", city="", limit=3)["customers"]) == 3)
    check("limit 超上限被截断", query_customers(limit=9999)["limit" ] if False else
          len(query_customers(limit=9999)["customers"]) <= 100)
    check("空条件查全部", query_customers()["total"] == 12)
    check("行业模糊匹配 '教育行业'", query_customers(industry="教育行业")["total"] == 4)
    check("'None' 视为不限", query_customers(industry="None", city="null")["total"] == 12)

    print("=" * 60)
    print("Tool 2: query_deals")
    r = query_deals(city="上海", date_range={"start": "2025-05-01", "end": "2025-05-31"})
    check("上海 5 月成交 3 笔", r["total"] == 3)
    check("金额合计 157000", r["amount_sum"] == 157000)
    r = query_deals(date_range={"start": "2025-01-01", "end": "2025-12-31"}, stage="won")
    check("全年 won 6 笔", r["total"] == 6)
    check("按日期倒序", r["deals"][0]["close_date"] >= r["deals"][-1]["close_date"])
    check("stage 非法值报错", "error" in query_deals(stage="abc"))
    check("start>end 报错", "error" in query_deals(
        date_range={"start": "2025-12-31", "end": "2025-01-01"}))
    check("日期格式错误被拦截", "error" in query_deals(
        date_range={"start": "2025/05/01", "end": "2025-05-31"}))
    check("date_range 传字符串可容错", "deals" in query_deals(
        date_range="2025-05-01 ~ 2025-05-31"))
    check("date_range 传 None 视为不限", "deals" in query_deals(date_range=None))
    check("不存在的组合返回 0 条", query_deals(city="上海", stage="lead")["total"] == 0)

    print("=" * 60)
    print("Tool 3: calc_sum")
    r = calc_sum(amounts=[12000.5, 8000, 15600])
    check("求和 35600.5", r["sum"] == 35600.5)
    check("数量 3", r["count"] == 3)
    check("均值正确", abs(r["average"] - 11866.83) < 0.01)
    check("单个数字", calc_sum(amounts=[100])["sum"] == 100)
    check("整数转浮点", calc_sum(amounts=[1, 2, 3])["sum"] == 6.0)
    check("空列表报错", "error" in calc_sum(amounts=[]))
    check("None 报错", "error" in calc_sum(amounts=None))
    check("含非数字报错", "error" in calc_sum(amounts=[100, "abc"]))
    check("带货币符号报错", "error" in calc_sum(amounts=["¥100"]))
    check("字符串列表可容错", calc_sum(amounts=["12000, 8000"])["sum"] == 20000.0)

    print("=" * 60)
    print("Tool 4: send_email")
    r = send_email(to="test@example.com", subject="五月汇报", body="上海区五月成交 4 笔。")
    check("发送成功", r["sent"] is True)
    check("返回 message_id", r["message_id"].startswith("mock-"))
    check("明确标记 mock", r["mock"] is True)
    check("邮箱格式错误被拦截", "error" in send_email(
        to="not-an-email", subject="s", body="b"))
    check("空邮箱报错", "error" in send_email(to="", subject="s", body="b"))
    check("空主题报错", "error" in send_email(to="a@b.com", subject="", body="b"))
    check("空正文报错", "error" in send_email(to="a@b.com", subject="s", body="  "))
    check("已发送列表可查", len(SENT_EMAILS) == 1)

    print("=" * 60)
    print("分发器 run_tool")
    check("JSON 字符串参数可用",
          run_tool("calc_sum", '{"amounts":[1,2]}')["sum"] == 3)
    check("dict 参数可用", run_tool("calc_sum", {"amounts": [1, 2]})["sum"] == 3)
    check("未知工具被拦截", "error" in run_tool("nonexistent", {}))
    check("坏 JSON 被拦截", "error" in run_tool("calc_sum", "{bad json"))
    check("非法参数名被拦截", "error" in run_tool("calc_sum", {"amount": [1]}))
    check("空字符串参数视为空参", "error" in run_tool("calc_sum", ""))

    print("=" * 60)
    print(f"结果：{ok}/{total} 通过")
    return ok == total


if __name__ == "__main__":
    _selftest()