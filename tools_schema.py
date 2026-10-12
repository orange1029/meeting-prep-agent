"""
tools_schema.py — 定义 4 个 Tool（工具）的 OpenAI function calling（函数调用）Schema

背景：
  Agent（智能体）本身不会查数据库、不会算账、不会发邮件。
  它只能「决定调哪个工具」，实际执行由我们的 Python 代码完成。
  这份 Schema（结构定义）就是 Agent 看的「工具说明书」——
  写得越清楚，Agent 选工具和填参数就越准。

4 个工具：
  1. query_customers — 按行业 / 城市查客户
  2. query_deals    — 按城市 / 日期范围 / 阶段查成交
  3. calc_sum       — 算金额总和
  4. send_email     — mock（模拟）发邮件

关键设计原则：
  · description 写给模型看，要说清「什么时候该用它」+「什么时候不该用」，
    不要只写「查询客户」这种废话
  · 参数描述同样重要，尤其是枚举值和格式（如 YYYY-MM-DD）
  · 必填参数放required，可选项给默认值描述（如 limit 默认 10）

配套使用（DeepSeek / OpenAI 均兼容）：
    from tools_schema import TOOLS, TOOL_FUNCTIONS
    resp = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "查一下上海的教育行业客户"}],
        tools=TOOLS,                      # 传工具清单
        tool_choice="auto",               # 让模型自己决定调不调
    )
    calls = resp.choices[0].message.tool_calls
    # 执行完把结果回传，模型才能继续回答用户
    messages.append(resp.choices[0].message)
    messages.append({
        "role": "tool",
        "tool_call_id": calls[0].id,
        "content": json.dumps({"customers": [...]}),
    })

直接运行本文件可打印完整 JSON 并校验格式：
    python tools_schema.py
"""

import json

# ============ 1. query_customers ============
query_customers = {
    "type": "function",
    "function": {
        "name": "query_customers",
        "description": (
            "按行业和/或城市查询客户名单。当用户询问「有哪些客户」「某行业有哪些公司」"
            "「某城市有哪些客户」时使用。不查询成交金额，成交请用 query_deals。"
            "两个参数都可以留空以查询全部客户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "industry": {
                    "type": "string",
                    "description": (
                        "行业名称，精确匹配客户档案中的行业字段。"
                        "常见取值：教育、医疗、零售、金融、制造、互联网。"
                        "用户未指定行业时传空字符串。"
                    ),
                },
                "city": {
                    "type": "string",
                    "description": (
                        "城市名称，精确匹配客户所在城市，如上海、北京、深圳。"
                        "用户未指定城市时传空字符串。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "返回记录条数上限，默认 10，最大 100。"
                        "用户说「前5 个」「最近的几个」等限定词时按需调整。"
                    ),
                },
            },
            "required": ["industry", "city"],
        },
    },
}

# ============ 2. query_deals ============
query_deals = {
    "type": "function",
    "function": {
        "name": "query_deals",
        "description": (
            "按城市、日期范围和/或阶段查询成交记录（deal，商机/成交单）的明细数据。"
            "仅当用户要「拿到成交明细列表/记录」时才用本工具。"
            "如果用户要的是「总数/合计金额」，先用本工具查出记录，再用 calc_sum 汇总。"
            "如果用户要的是「发邮件/通知」，请用 send_email，不要用本工具。"
            "日期格式为 YYYY-MM-DD。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，如上海。未指定时传空字符串。",
                },
                "date_range": {
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "string",
                            "description": (
                                "起始日期，格式 YYYY-MM-DD。"
                                "用户说「上个月」「今年三月」等相对时间时，"
                                "先换算成绝对日期再填。"
                            ),
                        },
                        "end": {
                            "type": "string",
                            "description": "结束日期，格式 YYYY-MM-DD。",
                        },
                    },
                    "required": ["start", "end"],
                    "description": (
                        "查询的日期区间（含首尾）。用户未限定时间时，"
                        "start 传最近一年初，end 传今天。"
                    ),
                },
                "stage": {
                    "type": "string",
                    "enum": ["lead", "proposal", "negotiation", "won", "lost"],
                    "description": (
                        "成交阶段。lead=线索，proposal=已报价，"
                        "negotiation=谈判中，won=已成交，lost=已丢失。"
                        "用户未指定阶段时传空字符串（表示不限）。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录条数上限，默认 20，最大 200。",
                },
            },
            "required": ["city", "date_range", "stage"],
        },
    },
}

# ============ 3. calc_sum ============
calc_sum = {
    "type": "function",
    "function": {
        "name": "calc_sum",
        "description": (
            "计算一组金额的总和。当用户询问「总共多少」「合计多少金额」"
            "「平均下来多少」时使用——必须先把查到的每笔金额都传给本工具，"
            "不要自己心算。只做求和，不做乘法、除法或折扣计算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amounts": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "待求和的金额列表，如[12000.5, 8000, 15600]。"
                        "必须来自查询工具返回的真实数据，单位为元。"
                        "列表至少包含 1 个数字。"
                    ),
                }
            },
            "required": ["amounts"],
        },
    },
}

# ============ 4. send_email ============
send_email = {
    "type": "function",
    "function": {
        "name": "send_email",
        "description": (
            "发送邮件（当前为 mock 模拟模式，不会真的发出去）。"
            "只要用户的诉求里出现「发邮件」「发通知」「告知某人」「汇报给某人」"
            "等字样，或用户给出了一个邮箱地址，就用本工具。"
            "注意：用户要求「汇报」某数据时，若同时给了邮箱，先用其他工具取数、"
            "再用本工具发送。"
            "本工具只负责发送，不负责取数——正文内容缺失时可以合理概括，"
            "但如果用户要的是精确数据，请先用查询工具取数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
"description": (
            "收件人邮箱地址，格式 name@company.com。"
            "如果用户已经给出了邮箱地址，必须直接使用，不要再反问确认。"
            "仅当用户只给了姓名、没给任何邮箱时，才需要先向用户询问。"
        ),
                },
                "subject": {
                    "type": "string",
                    "description": "邮件主题，简明扼要，如「上海区Q3 成交汇总」。",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "邮件正文，用中文书写。开头要有称呼，"
                        "正文说明关键数据，结尾有落款或行动项。"
                    ),
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
}

# ============ 工具清单（传给 API 的就是这个列表）============
TOOLS = [query_customers, query_deals, calc_sum, send_email]

# 名称列表，方便按名字取工具
TOOL_FUNCTIONS = {t["function"]["name"]: t["function"] for t in TOOLS}

# 工具名 -> 真实 Python 函数的映射表。
# Agent 返回 tool_calls（工具调用请求）后，用这里的 key 找到对应函数去执行。
TOOL_DISPATCH = {
    "query_customers": "实现见 tools_impl.py 中的 query_customers()",
    "query_deals": "实现见 tools_impl.py 中的 query_deals()",
    "calc_sum": "实现见 tools_impl.py 中的 calc_sum()",
    "send_email": "实现见 tools_impl.py 中的 send_email()",
}


def get_tool(name):
    """按工具名取Schema，找不到返回 None"""
    return TOOL_FUNCTIONS.get(name)


def validate():
    """校验所有 Schema 是否符合 OpenAI function calling 格式要求"""
    errors = []
    names = set()

    for tool in TOOLS:
        fn = tool.get("function", {})
        name = fn.get("name", "<无名>")

        # 顶层结构
        if tool.get("type") != "function":
            errors.append(f"{name}: type 必须是 'function'")
        if not fn.get("description"):
            errors.append(f"{name}:缺少 description")
        if name in names:
            errors.append(f"{name}: 工具名重复")
        names.add(name)

        # 参数结构
        params = fn.get("parameters", {})
        if params.get("type") != "object":
            errors.append(f"{name}: parameters.type 必须是 'object'")

        props = params.get("properties", {})
        required = params.get("required", [])

        if not props:
            errors.append(f"{name}: properties 为空")

        for pname, pspec in props.items():
            if "type" not in pspec:
                errors.append(f"{name}.{pname}: 缺少 type")
            if "description" not in pspec:
                errors.append(f"{name}.{pname}: 缺少 description")
            if pname not in required and "default" not in pspec.get("description", ""):
                # 非必填参数最好在描述里说明默认值
                pass

        # required 必须都在 properties 里
        for r in required:
            if r not in props:
                errors.append(f"{name}: required 里的 '{r}' 未在 properties 中定义")

    return errors


if __name__ == "__main__":
    print(f"共{len(TOOLS)} 个工具：{', '.join(TOOL_FUNCTIONS)}\n")

    errors = validate()
    if errors:
        print("✗ 格式校验失败：")
        for e in errors:
            print(f"  - {e}")
    else:
        print("✓ 格式校验通过")

    print("\n" + "=" * 60)
    print(json.dumps(TOOLS, ensure_ascii=False, indent=2))