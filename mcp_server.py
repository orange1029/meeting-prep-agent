"""
mcp_server.py — MCP（Model Context Protocol，模型上下文协议）服务器

MCP 是什么（一句话）：
  Anthropic 2025 年推出的 Agent 工具标准协议。
  之前 Agent 要接外部能力，得在代码里 hardcode（硬编码）工具定义。
  有了 MCP，Agent 启动时向 server「要一份工具清单」，用完就断，
  中间换成别的 server 也不用改 Agent 代码。类比 USB：写一次，到处可插。

这个 server 暴露 2 个工具：
  query_customers — 按行业/城市查客户
  query_deals    — 按城市/日期/阶段查成交

数据是 mock（模拟）的，但工具定义、参数校验、错误处理都是真实的，
换成真数据库只需改 query_customers 的函数体，协议层一行不用动。

启动方式：
  stdio（标准输入输出，默认，适合被 client 拉起）：
      python mcp_server.py
  SSE（服务器推送事件，适合独立部署后用 HTTP 连）：
      python mcp_server.py --sse
      然后 client 端用 http://127.0.0.1:8000/mcp 连接

传输方式由 client 决定，不是 server 自己选的——
server 只负责「把工具暴露出去」。
"""

import sys

from fastmcp import FastMCP

# ============ mock 数据（与 tools_impl.py 同源）============

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
]

MOCK_DEALS = [
    {"id": "D-1001", "customer": "上海交大教育科技", "city": "上海",
     "amount": 52000, "stage": "won", "close_date": "2025-05-12"},
    {"id": "D-1002", "customer": "上海启明中学", "city": "上海",
     "amount": 38000, "stage": "won", "close_date": "2025-05-20"},
    {"id": "D-1003", "customer": "上海华山医院", "city": "上海",
     "amount": 96000, "stage": "negotiation", "close_date": "2025-06-03"},
    {"id": "D-1004", "customer": "上海复旦软件", "city": "上海",
     "amount": 25000, "stage": "proposal", "close_date": "2025-04-28"},
    {"id": "D-1005", "customer": "北京新东方在线", "city": "北京",
     "amount": 120000, "stage": "won", "close_date": "2025-05-08"},
    {"id": "D-1006", "customer": "深圳华为技术", "city": "深圳",
     "amount": 240000, "stage": "won", "close_date": "2025-03-15"},
]

# 创建 server。instructions 会随工具清单一起发给 client，
# client 可以用它决定「什么场景该用这个 server」
mcp = FastMCP(
    name="crm-mock-server",
    instructions="提供 CRM（客户关系管理）模拟数据查询能力。"
                "问客户名单用 query_customers，问成交记录用 query_deals。",
)


# ============ 工具 1：查客户 ============

@mcp.tool()
def query_customers(industry: str = "", city: str = "", limit: int = 10) -> dict:
    """按行业和/或城市查询客户名单。

    当用户问「有哪些客户」「某行业有哪些公司」「某城市有哪些客户」时使用。
    要查成交金额请改用 query_deals。

    Args:
        industry: 行业名称，如 教育、医疗、零售。留空表示不限。
        city: 城市名称，如 上海、北京。留空表示不限。
        limit: 返回条数上限，默认 10，最大 100。
    """
    industry = (industry or "").strip()
    city = (city or "").strip()
    limit = max(1, min(int(limit or 10), 100))

    rows = MOCK_CUSTOMERS
    if industry:
        rows = [r for r in rows
                if industry in r["industry"] or r["industry"] in industry]
    if city:
        rows = [r for r in rows if city in r["city"] or r["city"] in city]

    return {
        "customers": rows[:limit],
        "total": len(rows),
        "filters": {"industry": industry or "不限", "city": city or "不限"},
    }


# ============ 工具 2：查成交 ============

@mcp.tool()
def query_deals(city: str = "", stage: str = "", limit: int = 20) -> dict:
    """按城市和/或阶段查询成交记录。

    当用户问「成交了多少」「有哪些商机」「业绩情况」时使用。

    Args:
        city: 城市名称。留空表示不限。
        stage: 成交阶段，lead=线索 / proposal=已报价 /
               negotiation=谈判中 / won=已成交 / lost=已丢失。
               留空表示不限。
        limit: 返回条数上限，默认 20，最大 200。
    """
    city = (city or "").strip()
    stage = (stage or "").strip().lower()
    limit = max(1, min(int(limit or 20), 200))

    valid = {"lead", "proposal", "negotiation", "won", "lost"}
    if stage and stage not in valid:
        # 返回错误信息而不是抛异常——LLM 能读懂错误并自己纠正参数
        return {"error": f"stage 取值无效：'{stage}'。"
                          f"合法值：{'、'.join(sorted(valid))}，留空表示不限"}

    rows = MOCK_DEALS
    if city:
        rows = [r for r in rows if city in r["city"] or r["city"] in city]
    if stage:
        rows = [r for r in rows if r["stage"] == stage]

    return {
        "deals": rows[:limit],
        "total": len(rows),
        "amount_sum": sum(r["amount"] for r in rows[:limit]),
    }


if __name__ == "__main__":
    # 传输方式。fastmcp 4.x 支持 4 种：
    #   stdio（默认，client 用子进程拉起）
    #   sse（旧的 HTTP 方案，已逐步废弃）
    #   http / streamable-http（MCP 新规范，推荐）
    #
    # 注意：我最初只判断了 --sse，传 --http 会静默回落到 stdio，
    # 表现为「启动了但客户端连不上」——stdio 会一直等 stdin。
    # 所以这里把 http / streamable-http 都显式支持。
    if "--http" in sys.argv or "--streamable-http" in sys.argv:
        # MCP 新规范，Claude Desktop / 各类 IDE 都已支持
        mcp.run(transport="http", host="127.0.0.1", port=8000)
    elif "--sse" in sys.argv:
        # 旧方案，保留兼容性
        mcp.run(transport="sse", host="127.0.0.1", port=8000)
    else:
        # 注意：stdio 模式下**绝对不能有多余的 print 到 stdout**！
        # stdout 是 JSON-RPC 协议通道，任何打印都会污染报文，
        # 导致 client 解析失败。调试信息只能写 stderr。
        print("[mcp_server] 以 stdio 模式启动，暴露 2 个工具", file=sys.stderr)
        mcp.run()