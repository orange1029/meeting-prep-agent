"""
test_deepseek.py — DeepSeek API（接口）连通性与调用测试

两种运行方式：
  A) 直接运行：  python test_deepseek.py        → 走main()，打印完整报告
  B) pytest：    pytest test_deepseek.py        → 走 test_ 函数 + conftest.py 的 fixture

代码结构：
  run_basic / run_stream / run_json_mode   实际干活的函数（不叫 test_，pytest 不会收集）
  test_basic / test_stream / test_json_mode  pytest 包装层，只负责传入 fixture 并断言

依赖：
  pip install openai python-dotenv pytest

配置（两种任选其一）：
  A) 环境变量：$env:DEEPSEEK_API_KEY="sk-xxx"        （Windows PowerShell）
  B) 同目录 .env 文件：DEEPSEEK_API_KEY=sk-xxx

模型说明（截至 2026-10）：
  deepseek-v4-flash  便宜、快，默认推荐
  deepseek-v4-pro更强，贵约 3.1 倍
  deepseek-chat / deepseek-reasoner 已于 2026-07-24 下线，不要再用
"""

import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # 读取同目录 .env（若存在）

API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# 价格（美元 / 每百万 token），用于估算成本；官方价目表可能调整，以官网为准
PRICE = {
    "deepseek-v4-flash": {"in": 0.14, "out": 0.28},
    "deepseek-v4-pro": {"in": 0.435, "out": 0.87},
}

PROMPT = "用一句话解释什么是 FDE（Forward Deployed Engineer，前向部署工程师）。"


def cost_of(usage, model):
    """按 token 用量估算本次调用花费（美元）"""
    p = PRICE.get(model)
    if not p or not usage:
        return None
    return (
        usage.prompt_tokens * p["in"] + usage.completion_tokens * p["out"]
    ) / 1_000_000


# ============ 实际干活的函数（命名不用 test_ 前缀，避免被 pytest 收集） ============


def run_basic(client, model=MODEL):
    """测试 1：非流式调用，一次性返回完整结果。返回 (回复内容, token 用量)"""
    print("=" * 60)
    print(f"[1/3] 非流式调用  model={model}  （不流式，一次性返回完整结果）")
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是数据分析工程搭子，回答简洁。"},
            {"role": "user", "content": PROMPT},
        ],
        temperature=0.3,
        max_tokens=500,
    )
    dt = time.perf_counter() - t0

    content = resp.choices[0].message.content
    print(f"  回复：{content}")
    u = resp.usage
    print(
        f"  tokens（令牌）：输入 {u.prompt_tokens} / 输出 {u.completion_tokens}"
        f" / 合计 {u.total_tokens}    耗时 {dt:.2f}s（秒）"
    )
    c = cost_of(u, model)
    if c is not None:
        print(f"  估算花费：${c:.6f}（美元）")
    return content, u


def run_stream(client, model=MODEL):
    """测试 2：流式调用，逐块返回。返回 (完整回复, 分块数, 首字延迟秒)"""
    print("=" * 60)
    print("[2/3] 流式调用（streaming，边生成边返回）")
    t0 = time.perf_counter()
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "数一数：从 1 到 50 的偶数之和。"}],
        stream=True,
        max_tokens=500,
    )
    chunks, first_at, parts = 0, None, []
    for ck in stream:
        delta = ck.choices[0].delta.content
        if delta:
            if first_at is None:
                first_at = time.perf_counter() - t0
                print(f"  首字延迟（首 token 耗时）：{first_at:.2f}s（秒）")
            print(delta, end="", flush=True)
            parts.append(delta)
            chunks += 1
    full = "".join(parts)
    print(f"\n  完整回复：{full}")
    print(f"  分块数：{chunks}    总耗时 {time.perf_counter() - t0:.2f}s（秒）")
    return full, chunks, first_at


def run_json_mode(client, model=MODEL):
    """测试 3：JSON 模式（结构化输出），返回解析后的 dict

    注意：DeepSeek 要求 prompt 中必须出现 "json" 字样，否则返回 400。
    """
    print("=" * 60)
    print("[3/3] JSON 模式（结构化输出）")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "只输出一个合法 json 对象，不要任何解释文字。",
            },
            {
                "role": "user",
                # 关键词 json 必须出现，否则 DeepSeek 报 400
                "content": (
                    "请把下面信息整理成 json，字段为 order_amount（数字）、"
                    "region（字符串）、channel（字符串）："
                    "订单金额 128.5 元，地区 上海，渠道 App。"
                ),
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=300,
    )
    raw = resp.choices[0].message.content
    print(f"  原始字符串：{raw}")
    data = json.loads(raw)  # 解析失败会直接抛错，让测试失败
    print(f"  解析结果：{data}")
    c = cost_of(resp.usage, model)
    if c is not None:
        print(f"  估算花费：${c:.6f}（美元）")
    return data


# ============ pytest 包装层（test_ 开头，会被 pytest 收集；client 由 conftest.py 提供） ============


def test_api_key_configured():
    """测试 0：确认密钥已配置，避免其他测试莫名失败"""
    assert API_KEY, (
        "未找到 DEEPSEEK_API_KEY（DeepSeek API 密钥）。"
        "请在同目录 .env 文件中填入，或设置环境变量。"
    )
    assert not API_KEY.startswith("sk-在这里"), ".env 里还是占位符，没换成真密钥"


def test_basic(client, model):
    """测试 1：非流式调用应返回非空内容，且 token 用量合理"""
    content, usage = run_basic(client, model)
    assert content.strip(), "回复内容为空"
    assert usage.total_tokens > 0, "token 计数为 0"
    assert usage.completion_tokens > 0, "输出 token 为 0"
    assert usage.prompt_tokens > 0, "输入 token 为 0"


def test_stream(client, model):
    """测试 2：流式调用应拿到多个分块，且内容拼接完整"""
    full, chunks, first_at = run_stream(client, model)
    assert chunks > 1, f"分块数异常：{chunks}（流式没生效？）"
    assert full.strip(), "流式内容为空"
    assert first_at is not None, "未记录到首字延迟"
    assert "650" in full, "回复内容不含预期答案 650，可能丢块"


def test_json_mode(client, model):
    """测试 3：JSON 模式应返回可解析的 dict，且字段正确"""
    data = run_json_mode(client, model)
    assert isinstance(data, dict), f"不是 dict（字典）：{type(data)}"
    assert data.get("region") == "上海", f"region 字段错误：{data.get('region')}"
    assert data.get("channel") == "App", f"channel 字段错误：{data.get('channel')}"
    assert data.get("order_amount") == 128.5, f"金额错误：{data.get('order_amount')}"


# ============ 直接运行入口 ============


def main():
    if not API_KEY:
        print("✗ 未找到 DEEPSEEK_API_KEY（DeepSeek API 密钥）")
        print("  设置方式：")
        print('    PowerShell：$env:DEEPSEEK_API_KEY="sk-你的密钥"')
        print("    或在脚本同目录建 .env 文件，内容：DEEPSEEK_API_KEY=sk-你的密钥")
        print("  密钥获取：https://platform.deepseek.com/api_keys")
        sys.exit(1)

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    print(f"endpoint（接口地址）：{BASE_URL}")
    print(f"model（模型）：{MODEL}\n")

    try:
        run_basic(client)
        run_stream(client)
        run_json_mode(client)
    except Exception as e:
        print(f"\n✗ 调用失败：{type(e).__name__}")
        print(f"  {e}")
        print("\n排查方向：")
        print("  400 → prompt 不合规范（如JSON 模式缺 'json' 关键词）")
        print("  401 → 密钥错或余额不足；404 → 模型名已下线；429 → 限流")
        sys.exit(1)

    print("=" * 60)
    print("✓ 全部测试通过")


if __name__ == "__main__":
    main()