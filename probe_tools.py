"""
probe_tools.py — 验证 4 个 Tool（工具）Schema 是否好用的探测脚本

为什么需要这个文件？
  Schema（结构定义）写完不代表能用。真实情况是：模型看完描述后
  会不会选对工具？参数填得对不对？嵌套对象和枚举会不会填错？
  这些只能真调一次 API 才知道。

  踩过的坑：初版 send_email（发邮件）的描述写得不够强，
  用户说「给 xxx 发邮件汇报五月业绩」时，模型错选了 query_deals（查成交）。
  原因是 query_deals 描述里「业绩」类词权重更高，抢走了匹配。

  所以每次改完 description，都要跑一遍本脚本看选择是否正确。

运行：
    python probe_tools.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from tools_schema import TOOLS

load_dotenv()

# 测试用例：(自然语言输入, 期望调用的工具名 或 可接受的工具名元组)
# 期望值为 None 表示「期望模型直接回答，不调工具」
#
# 重要认知：模型选工具是概率行为，同一句话可能选 A 也可能选 B。
# 两种选择都合理的用例，要写成元组，否则会误报失败。
CASES = [
    ("查上海有哪些客户", "query_customers"),
    ("帮我查上海的教育行业客户，然后把他们的成交金额加总", "query_customers"),
    ("查一下今年三月到五月的谈判中商机", "query_deals"),
    ("把 12000.5、8000、15600 加起来", "calc_sum"),
    (
        "帮我给 wangwu@example.com 发封邮件：说上海区本月新增 3 个教育行业客户，重点跟进",
        # 数字已给定，模型可以直接发邮件（send_email），
        # 也可以先核实客户数（query_customers）。两种都对。
        ("query_customers", "send_email"),
    ),
    (
        "帮我给 wangwu@example.com 发封邮件，就说客户已全部确认，辛苦跟进",
        "send_email",
    ),
]


def new_client():
    return OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


def probe_single_tool_choice(client):
    """测试 1：单轮工具选择是否正确"""
    print("=" * 60)
    print("测试 1：工具选择与参数填充")
    passed = 0
    for query, expected in CASES:
        r = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            messages=[{"role": "user", "content": query}],
            tools=TOOLS,
            tool_choice="auto",
        )
        calls = r.choices[0].message.tool_calls or []
        accepted = (expected,) if isinstance(expected, str) else expected
        if expected is None:
            ok = not calls
            got = calls[0].function.name if calls else "（未调工具，直接回答）"
            label = "（不应调工具）"
        else:
            got = calls[0].function.name if calls else "（未调工具）"
            ok = got in accepted
            label = "/".join(accepted) if len(accepted) > 1 else accepted[0]

        mark = "✓" if ok else "✗"
        passed += ok
        print(f"  {mark} 期望 {label:<28} 实际 {got}")
        if not ok:
            print(f"      输入：{query}")
            if calls:
                print(f"      参数：{calls[0].function.arguments}")
    return passed, len(CASES)


def probe_agent_loop(client):
    """测试 2：多轮 Agent（智能体）循环 —— 取数 → 计算 → 发邮件 能否串起来"""
    print("=" * 60)
    print("测试 2：多轮工具链（query_deals → calc_sum → send_email）")
    msgs = [
        {
            "role": "user",
            "content": "给 zhangsan@example.com 发个邮件，汇报五月成交总额",
        }
    ]
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    r = client.chat.completions.create(
        model=model, messages=msgs, tools=TOOLS, tool_choice="auto"
    )
    msg = r.choices[0].message
    if not msg.tool_calls:
        print("  ✗ 第一轮就没调工具")
        return 0
    print(f"  ✓ 第一轮：{msg.tool_calls[0].function.name}")
    msgs.append(msg.model_dump(exclude_none=True))

    # mock（模拟）回传查询结果
    mock_deals = [
        {"deal_id": "D-001", "city": "上海", "amount": 52000, "stage": "won"},
        {"deal_id": "D-002", "city": "上海", "amount": 38000, "stage": "won"},
    ]
    for tc in msg.tool_calls:
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps({"deals": mock_deals}, ensure_ascii=False),
            }
        )

    r2 = client.chat.completions.create(model=model, messages=msgs, tools=TOOLS)
    calls2 = r2.choices[0].message.tool_calls or []
    if calls2 and calls2[0].function.name == "calc_sum":
        print(f"  ✓ 第二轮：calc_sum({calls2[0].function.arguments})")
        msgs.append(r2.choices[0].message.model_dump(exclude_none=True))
        for tc in calls2:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"sum": 90000}, ensure_ascii=False),
                }
            )
        r3 = client.chat.completions.create(model=model, messages=msgs, tools=TOOLS)
        calls3 = r3.choices[0].message.tool_calls or []
        if calls3 and calls3[0].function.name == "send_email":
            print(f"  ✓ 第三轮：send_email")
            print(f"✓ 多轮链路完整：查数 → 求和 → 发邮件")
            return 3
        print(f"  ✗ 第三轮期望 send_email，实际 {calls3[0].function.name if calls3 else '未调工具'}")
    else:
        got = calls2[0].function.name if calls2 else "未调工具"
        print(f"  ✗ 第二轮期望 calc_sum，实际 {got}")
    return 2


def probe_no_tool_needed(client):
    """测试 3：不该调工具时，模型应直接回答（避免过度触发）"""
    print("=" * 60)
    print("测试 3：无工具需求时不乱调")
    r = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        messages=[{"role": "user", "content": "你好，你能做什么？"}],
        tools=TOOLS,
        tool_choice="auto",
    )
    calls = r.choices[0].message.tool_calls
    if calls:
        print(f"  ✗ 闲聊触发了工具：{calls[0].function.name}")
        return 0
    print("  ✓ 闲聊时直接回答，未乱调工具")
    return 1


def main():
    client = new_client()

    p1, t1 = probe_single_tool_choice(client)
    p2 = probe_agent_loop(client)
    p3 = probe_no_tool_needed(client)

    print("=" * 60)
    # 计数口径：CASES 每个算 1；多轮链路每轮算 1（最多 3 轮）；
    # 「不乱调工具」算 1
    total_ok = p1 + p2 + p3
    total = len(CASES) + 3 + 1
    print(f"结果：{total_ok}/{total} 通过")
    if total_ok == total:
        print("✓ Schema 可用：工具选择准确，多轮链路完整")
    else:
        print("✗ 有用例未通过——多半是 description 需要再调")


if __name__ == "__main__":
    main()