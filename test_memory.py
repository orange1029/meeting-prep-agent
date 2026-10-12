"""
test_memory.py — 长期记忆模块测试

分两部分：
  A 组  纯逻辑测试（分词、打分、增删查、衰减），用临时文件不碰真实记忆库
  B 组  集成测试（真调API，验证记忆真的影响回答行为）

为什么 A 组要单独用临时文件？
  测试不能污染用户真实的记忆库——跑一次测试结果
  「用户叫王磊」进了生产记忆库，以后每次对话都被干扰。

运行：
    python test_memory.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from memory_store import (
    MEMORY_TYPES,
    MemoryStore,
    similarity,
    tokenize,
)
from memory_tools import MEMORY_TOOLS, recall_memory_impl, save_memory_impl
from tools_schema import TOOLS

RESULTS = {"pass": 0, "fail": 0}


def check(label, cond, detail=""):
    ok = bool(cond)
    RESULTS["pass" if ok else "fail"] += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  → {detail}" if not ok and detail else ""))
    return ok


def fresh_store(decay_after_days=30):
    """在临时目录建一个新 store，测试用

    必须强制隔离！不能用 MemoryStore(decay_after_days=...)——
    那个走默认路径会写真实记忆库。跑一次测试就往生产库里塞进
    「很久没用的事实」这类假数据，之后每次对话都被干扰。
    这个坑本项目已经踩过一次。

    注意：不传 path 时 MemoryStore 会读 memory/memories.json，
    所以这里必须显式给path，一行都不能省。
    """
    tmpdir = tempfile.mkdtemp(prefix="memtest_")
    return MemoryStore(os.path.join(tmpdir, "memories.json"),
                       decay_after_days=decay_after_days)


def assert_isolated(store):
    """断言这个 store 用的不是生产记忆文件"""
    prod = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "memory", "memories.json")
    )
    return os.path.abspath(store.path) != prod


# ============ A 组：纯逻辑 ============

def test_tokenize():
    print("=" * 60)
    print("A1 分词")
    t = tokenize("上海教育行业 2025")
    check("中文取单字", "上" in t and "海" in t)
    check("中文取双字组合", "上海" in t and "教育" in t)
    check("英文数字整体保留", "2025" in t)
    check("英文按词切", "abc" in tokenize("hello abc world"))
    check("英文停用词被剔除", "the" not in tokenize("the customer"))
    check("英文实义词保留", "customer" in tokenize("the customer"))
    # 停用双字组合（「这个」「什么」）应被剔除
    check("停用双字被剔除",
          "这个" not in tokenize("这个客户") and "什么" not in tokenize("什么问题"))
    # 虚词单字要剔除，但构成实义词的单字必须保留——
    # 「上海」的「上」若被剔除，中文召回会直接失效
    check("实义单字不被误删", "上" in tokenize("上海"))
    check("虚词单字被剔除", "这" not in tokenize("这个"))
    check("空输入不崩", tokenize("") == [])
    check("None 不崩", tokenize(None) == [])


def test_similarity():
    print("=" * 60)
    print("A2 相似度打分")
    s1 = similarity(tokenize("张伟邮箱"), tokenize("张伟的邮箱是zhangwei@x.com"))
    s2 = similarity(tokenize("北京客户"), tokenize("张伟的邮箱是zhangwei@x.com"))
    check("相同主题得分更高", s1 > s2, f"{s1:.3f} vs {s2:.3f}")
    check("完全不相干得分接近 0", s2 < 0.1)
    check("空 query 返回 0", similarity([], tokenize("任意")) == 0.0)
    check("空记忆返回 0", similarity(tokenize("任意"), []) == 0.0)
    # 长度惩罚测试
    short = similarity(tokenize("上海"), tokenize("上海"))
    long_m = tokenize("上海交大教育科技有限公司位于浦东张江高科技园区")
    l1 = similarity(tokenize("上海"), long_m)
    check("短 query 对长记忆不被过度惩罚", l1 > 0, f"{l1:.3f}")
    check("完全匹配得分 1.0", abs(short - 1.0) < 0.01)


def test_crud():
    print("=" * 60)
    print("A3 增删查")
    st = fresh_store()
    check("[隔离] 测试用 store 不是生产库", assert_isolated(st))
    ok, msg = st.add("用户叫王磊", "fact")
    check("写入成功", ok and "M0001" in msg)
    st.add("用户偏好结论先行", "preference")
    st.add("上海教育行业增长放缓", "observation")
    check("共 3 条", len(st.memories) == 3)

    ok, msg = st.add("", "fact")
    check("空字符串内容被拒", not ok)
    ok, msg = st.add("   ", "fact")
    check("纯空白内容被拒", not ok)
    ok, msg = st.add("类型错", "unknown_type")
    check("非法类型被拒", not ok and "合法值" in msg)

    hits = st.retrieve("王磊", top_k=3)
    check("能检索到", len(hits) == 1 and "王磊" in hits[0]["content"])
    check("命中后 hit_count 递增", st.memories[0]["hit_count"] == 1)
    check("命中后记录 last_hit", st.memories[0]["last_hit_at"] is not None)

    # 持久化验证
    st2 = MemoryStore(st.path)
    check("重开文件仍能读到 3 条", len(st2.memories) == 3)
    check("ID 保持一致", st2.memories[0]["id"] == "M0001")

    n = st.forget(mem_id="M0001")
    check("按 ID 删除", n == 1 and len(st.memories) == 2)
    check("删不存在的返回 0", st.forget(mem_id="M9999") == 0)
    n = st.forget(contains="王磊")
    check("按内容删除（无匹配）", n == 0)

    # ID 唯一性：删除后新增不能撞号（实测出现过两个 M0004）
    st.add("删除后新增的", "fact")
    ids = [m["id"] for m in st.memories]
    check("删除后新增记忆 ID 不重复", len(ids) == len(set(ids)),
          f"IDs={ids}")
    check("新 ID 递增于现存最大值",
          max(int(i[1:]) for i in ids) == len(st.memories) + 1,
          f"IDs={ids}")


def test_decay():
    print("=" * 60)
    print("A4 衰减与遗忘")
    st = fresh_store(decay_after_days=30)
    check("[隔离] 衰减测试未用生产库", assert_isolated(st))

    # 旧的、命中过但很久没再命中
    old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
    st.add("很久没用的事实", "fact")
    st.memories[0]["hit_count"] = 3
    st.memories[0]["last_hit_at"] = old

    # 旧的、从未命中
    st.add("很老的从未命中", "fact")
    st.memories[1]["created_at"] = old
    st.memories[1]["last_hit_at"] = None

    # 新的
    st.add("刚记的", "fact")

    n = st.decay_and_forget()
    check("清理了 2 条老记忆", n == 2, f"实际 {n}")
    check("保留新记忆", len(st.memories) == 1 and "刚记的" in st.memories[0]["content"])

    n2 = st.decay_and_forget()
    check("重复执行幂等", n2 == 0)


def test_stats_and_render():
    print("=" * 60)
    print("A5 统计与渲染")
    st = fresh_store()
    st.add("用户偏好金额用万元", "preference")
    st.add("客户张伟邮箱是 zw@x.com", "fact")
    st.retrieve("万元", top_k=2)
    st.retrieve("万元", top_k=2)

    s = st.stats()
    check("总数正确", s["total"] == 2)
    check("按类型统计", s["by_type"] == {"preference": 1, "fact": 1})
    check("统计未命中数", s["never_hit"] == 1)
    check("统计命中排行", len(s["top_hits"]) >= 1)

    text = st.render_for_prompt(st.retrieve("万元", top_k=2))
    check("渲染包含记忆内容", "万元" in text)
    check("渲染包含类型标注", "preference" in text)
    check("空记忆渲染为空串", st.render_for_prompt([]) == "")

    # strong / weak 分级
    hits = st.retrieve("完全无关的问题xyzabc", top_k=5)
    check("不相关时无 strong", all(h["relevance"] == "weak" for h in hits)
          or len(hits) == 0)


def test_tool_impl():
    print("=" * 60)
    print("A6 记忆工具实现")
    # 用临时 store 隔离，避免污染真实记忆库
    import memory_tools
    tmp = fresh_store()
    orig = memory_tools._STORE
    memory_tools._STORE = tmp
    try:
        r = save_memory_impl(content="测试记忆：客户A 的邮箱是 a@x.com", type="fact")
        check("save_memory 成功", r.get("saved") is True)
        check("返回 memory_id", r["message_id"] if "message_id" in r
              else r["message"].startswith("已记住"))

        r = save_memory_impl(content="", type="fact")
        check("空内容被拒", "error" in r)

        r = save_memory_impl(content="x", type="wrong")
        check("非法类型被拒", "error" in r)

        r = recall_memory_impl(query="客户A 邮箱")
        check("recall 找到刚存的", r["found"] >= 1, f"found={r['found']}")
        check("recall 返回渲染文本", "测试记忆" in r.get("text", ""))

        r = recall_memory_impl(query="")
        check("空 query 被拒", "error" in r)

        r = recall_memory_impl(query="测试", top_k=999)
        check("top_k 被钳制", "error" not in r)
    finally:
        memory_tools._STORE = orig


def test_corrupt_file():
    print("=" * 60)
    print("A7 异常容错")
    tmpdir = tempfile.mkdtemp(prefix="memtest_")
    bad = os.path.join(tmpdir, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{这不是合法 json")
    st = MemoryStore(bad)
    check("坏文件不崩，从空库开始", st.memories == [])

    st.add("x", "fact")
    check("坏文件情况下仍可写入", len(st.memories) == 1)


# ============ B 组：集成（真调 API）============

def test_integration():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("=" * 60)
        print("B 组：跳过（未配置 API Key）")
        return

    print("=" * 60)
    print("B 组：集成测试 —— 记忆是否真的影响回答（真调 API）")

    from openai import OpenAI
    from run_chat_with_memory import build_system_prompt

    store = fresh_store()
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

    store.add("用户偏好：所有金额一律用「万元」表述", "preference")

    # 有记忆 vs 无记忆，同一问题的回答
    q = "今年5月上海成交总额多少？"
    hits = store.retrieve("成交 金额 单位", top_k=3)
    mem_text = store.render_for_prompt(hits, only_strong=True)
    print(f"  召回并注入的记忆：{'有' if mem_text else '无'}")
    if not mem_text:
        print("  （本库中该查询无强相关记忆，跳过对比）")
        return

    prompts = {
        "有记忆": [{"role": "system", "content": build_system_prompt(mem_text)},
                  {"role": "user", "content": q}],
        "无记忆": [{"role": "system", "content": build_system_prompt()},
                  {"role": "user", "content": q}],
    }
    answers = {}
    for tag, msgs in prompts.items():
        # 两个坑（都实测踩过）：
        #  1. 不能传 tools=[] —— DeepSeek 会返回 DSML 标记文本而非正常回答
        #  2. 不能传 tool_choice="none" —— 同样触发 DSML 异常格式
        # 所以用完整工具集 + auto，然后模拟 Agent 循环：
        # 模型调工具 → 我们执行 → 把结果喂回去 → 取最终自然语言回答
        conv = list(msgs)
        for _ in range(4):
            r = client.chat.completions.create(
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                messages=conv,
                tools=TOOLS + MEMORY_TOOLS,
                tool_choice="auto",
            )
            msg = r.choices[0].message
            if not msg.tool_calls:
                answers[tag] = msg.content or ""
                break
            conv.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                from tools_impl import run_tool
                result = run_tool(tc.function.name, tc.function.arguments)
                conv.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
        else:
            answers[tag] = conv[-1].get("content", "") or ""

    has_wan = "万元" in answers["有记忆"] and "万" in answers["有记忆"]
    has_yuan_only = "万元" not in answers["无记忆"]
    check("有记忆时回答用了「万元」", has_wan,
          f"有记忆回答片段: {answers['有记忆'][:80]}")
    check("无记忆时未用「万元」（对照成立）", has_yuan_only,
          f"无记忆回答片段: {answers['无记忆'][:80]}")

    print("  有记忆：" + answers["有记忆"][:150].replace("\n", " "))
    print("  无记忆：" + answers["无记忆"][:150].replace("\n", " "))


def main():
    print("=" * 60)
    print("长期记忆模块测试")
    print(f"记忆类型：{list(MEMORY_TYPES)}\n")

    # 先备份真实记忆库，防止测试污染
    prod = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "memory", "memories.json")
    )
    backup = None
    if os.path.exists(prod):
        backup = prod + ".bak"
        with open(prod, "r", encoding="utf-8") as f:
            backup_data = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(backup_data)

    try:
        test_tokenize()
        test_similarity()
        test_crud()
        test_decay()
        test_stats_and_render()
        test_tool_impl()
        test_corrupt_file()
        test_integration()

        # 确认测试没有污染生产记忆库。
        # 用**测试专属内容**做标记，不能用「王磊」——
        # 用户真实记忆里完全可能就有叫王磊的人，那样会误报。
        test_only_markers = ["很久没用的事实", "很老的从未命中",
                            "删除后新增的", "刚记的"]
        if os.path.exists(prod):
            with open(prod, "r", encoding="utf-8") as f:
                now_data = f.read()
            leaked = [k for k in test_only_markers if k in now_data]
            check("[隔离] 测试未污染生产记忆库", not leaked,
                  f"泄漏内容: {leaked}")
    finally:
        # 恢复真实记忆库
        if backup and os.path.exists(backup):
            with open(prod, "w", encoding="utf-8") as f:
                f.write(backup_data)
            os.remove(backup)

    print("=" * 60)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"结果：{RESULTS['pass']}/{total} 通过")
    if RESULTS["fail"]:
        print("✗ 有用例失败")
        sys.exit(1)
    print("✓ 长期记忆模块全部正常")


if __name__ == "__main__":
    main()