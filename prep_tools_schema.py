"""
prep_tools_schema.py — 会前简报专用的 2 个 Tool（工具）Schema

与 tools_schema.py 的 4 个业务工具并列，构成会前简报所需的完整能力：

  复用   query_customers  — 查客户档案
  新增   search_industry_news(industry, days)
           查行业近 N 天的关键事件，用于判断客户所处赛道的大势
  新增   get_meeting_history(client_name)
           查历史沟通记录，用于「这次该聊什么、不该重复问什么」

为什么需要后两个工具？会前简报的三个信息缺一不可：
  客户是谁（档案）→ 我方跟他谈到哪了（历史）→ 赛道在发生什么（趋势）
  只查档案得到的是一张名片，不是简报。

Schema 设计要点（承接 tools_schema.py 的经验）：
  · description 写「何时该用」，并注明与相邻工具的边界
  · 参数描述里给出格式示例与相对时间的处理方式
  · 工具之间互相点名，减少误选

运行自检：
    python prep_tools_schema.py
"""

PREP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_industry_news",
            "description": (
                "查询某个行业近期的关键事件与市场动态（默认近 30 天）。"
                "当需要了解客户所处赛道的大势、新政策、竞品动作时使用。"
                "注意：这是行业层面的情报，不是单个客户的信息——"
                "查具体客户请用 query_customers。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "industry": {
                        "type": "string",
                        "description": (
                            "行业名称，如 金融、医疗、教育、零售、互联网、"
                            "制造业。会对输入做模糊匹配，"
                            "如传「银行」也能命中「金融」相关条目。"
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "回溯天数，默认 30。用户说「最近一周」传 7，"
                            "「今年以来」传 365。取值范围 1~365。"
                        ),
                    },
                },
                "required": ["industry", "days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_history",
            "description": (
                "查询某个客户的历史沟通记录摘要，包含过往会议、已承诺事项、"
                "遗留问题与客户关注点。当准备与某客户开会、需要承接上次沟通"
                "时使用。客户名支持模糊匹配，如传「招商银行」或「招行」均可。"
                "如果没有历史记录，会明确返回「无历史记录」而不是编造。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {
                        "type": "string",
                        "description": (
                            "客户名称，如 招商银行。支持部分匹配，"
                            "传「招行」也能命中「招商银行」。"
                            "不要传空字符串。"
                        ),
                    },
                },
                "required": ["client_name"],
            },
        },
    },
]

PREP_TOOL_FUNCTIONS = {t["function"]["name"]: t["function"]
                       for t in PREP_TOOLS}

# 完整工具集：4 个原有业务工具 + 2 个简报专用工具
from tools_schema import TOOLS as BASE_TOOLS  # noqa: E402

ALL_PREP_TOOLS = BASE_TOOLS + PREP_TOOLS


if __name__ == "__main__":
    print(f"简报专用工具：{len(PREP_TOOLS)} 个")
    print(f"完整工具集  ：{len(ALL_PREP_TOOLS)} 个\n")

    errors = []
    for tool in ALL_PREP_TOOLS:
        fn = tool.get("function", {})
        name = fn.get("name", "<无名>")
        if tool.get("type") != "function":
            errors.append(f"{name}: type 必须是 function")
        if not fn.get("description"):
            errors.append(f"{name}: 缺 description")
        p = fn.get("parameters", {})
        if p.get("type") != "object":
            errors.append(f"{name}: parameters.type 必须是 object")
        props = p.get("properties", {})
        for k, v in props.items():
            if "type" not in v:
                errors.append(f"{name}.{k}: 缺 type")
            if "description" not in v:
                errors.append(f"{name}.{k}: 缺 description")
        for r in p.get("required", []):
            if r not in props:
                errors.append(f"{name}: required 里的 {r} 未定义")

    for fn in PREP_TOOL_FUNCTIONS.values():
        print(f"  {fn['name']}("
              f"{', '.join(fn['parameters']['properties'])})")
        print(f"    必填：{fn['parameters']['required']}")

    print("\n" + ("✗ " + "; ".join(errors) if errors
                  else "✓ 全部 Schema 校验通过"))