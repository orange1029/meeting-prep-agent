"""
test_prep_tools.py — 会前简报链路的测试

为什么需要这个文件？
  这个仓库里有两套工具：
    tools_impl.py     4 个通用业务工具（查客户/查成交/算合计/发邮件）
    prep_tools_impl.py 2 个简报专用工具（查行业动态/查历史沟通）
  以及一条完整的简报生成链路：
    meeting_prep_agent.py  collect_data → build_blocks → generate_suggestion

  之前 test_errors.py / test_stream_chat.py / test_memory.py 全部只覆盖
  通用工具那套，**简报链路一个用例都没有**。
  但简报才是这个项目的产品本体，而且它已经修过 4 个真 bug
  （tag 重复 / 空参数 / 别名映射 / 无历史时编造）——却没有一个回归测试。
  这是最大的覆盖缺口。

三类测试：
  A 组 工具层——search_industry_news / get_meeting_history / run_prep_tool
  B 组 渲染层——build_blocks 的四段渲染与「数据缺失必须标 ⚠」
  C 组 匹配层——find_customer 的分级匹配 + 记忆 ID 清理

设计约束：
  · **零 API 调用**——全部是纯逻辑，秒级跑完，不花钱、不受模型随机性影响
  · **隔离记忆库**——get_meeting_history 读的是 memory/memories.json，
    测试必须在临时目录里造一份，绝不碰生产库
    （这个坑本项目已经踩过一次：test_decay 往生产库灌了假数据）

运行：
    python test_prep_tools.py
"""

import os
import sys
import tempfile
from unittest import mock

from dotenv import load_dotenv

load_dotenv()

from meeting_prep_agent import (
    HISTORY_GROUPS,
    _clean_memory_ids,
    _history_facts,
    build_blocks,
    collect_data,
)
from prep_tools_impl import (
    CLIENT_ALIASES,
    find_customer,
    get_meeting_history,
    run_prep_tool,
    search_industry_news,
)
from tools_impl import MOCK_CUSTOMERS

RESULTS = {"pass": 0, "fail": 0}


def check(label, cond, detail=""):
    ok = bool(cond)
    RESULTS["pass" if ok else "fail"] += 1
    print(f"  {'✓' if ok else '✗'} {label}"
          + (f"  → {detail}" if not ok and detail else ""))
    return ok


# ============ 隔离的记忆库 ============

def make_store(entries):
    """在临时目录造一个记忆库，返回 (store, tmpdir)

    entries: [{"content": ..., "tags": [...], "type": ...}, ...]

    必须显式给 path——MemoryStore() 默认走 memory/memories.json，
    也就是生产库。这个坑本项目踩过一次。
    """
    from memory_store import MemoryStore

    tmpdir = tempfile.mkdtemp(prefix="preptest_")
    path = os.path.join(tmpdir, "memories.json")
    store = MemoryStore(path)
    for e in entries:
        store.add(e["content"], mtype=e.get("type", "fact"),
                  tags=e.get("tags", []))
    return store, tmpdir


def patch_store(store):
    """把 get_meeting_history 里的 MemoryStore() 换成测试用的 store

    用 mock.patch 打 prepare_tools_impl 模块里 MemoryStore 的导入点。
    因为 get_meeting_history 是**函数内延迟导入** memory_store，
    所以 patch "memory_store.MemoryStore" 即可全局生效。
    """
    return mock.patch("memory_store.MemoryStore", return_value=store)


# ============ A 组：工具层 ============

def test_search_industry_news():
    print("\n【A1】search_industry_news")
    r = search_industry_news(industry="金融", days=30)
    check("标准行业有结果", r.get("total", 0) > 0, str(r)[:80])
    check("返回结构含 news/total/industry/days",
          all(k in r for k in ("news", "total", "industry", "days")))

    # 别名映射
    r2 = search_industry_news(industry="银行", days=30)
    check("别名「银行」→ 金融", r2.get("industry") == "金融",
          f"得到 {r2.get('industry')}")
    r3 = search_industry_news(industry="超市", days=30)
    check("别名「超市」→ 零售", r3.get("industry") == "零售",
          f"得到 {r3.get('industry')}")

    # 未知行业必须明确报错并列出可查行业，不能返回空列表
    r4 = search_industry_news(industry="航空航天", days=30)
    check("未知行业返回 error", "error" in r4)
    check("错误信息列出可查行业",
          "available_industries" in r4
          and "金融" in str(r4.get("available_industries", [])),
          str(r4)[:100])
    check("未知行业不返回 news 键（避免模型自己编）",
          "news" not in r4)

    # 参数校验
    check("空行业报错", "error" in search_industry_news(industry=""))
    check("空 days 用默认 30",
          search_industry_news(industry="金融", days="").get("days") == 30)
    r5 = search_industry_news(industry="金融", days=999)
    check("days 越界报错", "error" in r5)
    r6 = search_industry_news(industry="金融", days="abc")
    check("days 非整数报错", "error" in r6)

    # 时间窗口过滤：7 天窗口应少于 30 天窗口
    r7 = search_industry_news(industry="金融", days=7)
    check("7 天窗口比 30 天窗口结果少",
          r7.get("total", 0) < r.get("total", 0),
          f"7天={r7.get('total')} 30天={r.get('total')}")


def test_get_meeting_history():
    print("\n【A2】get_meeting_history")
    entries = [
        {"content": "与招商银行的会议：讨论数据中台建设", "tags": ["招商银行", "会议"]},
        {"content": "上次承诺：两周内提供方案与报价", "tags": ["招商银行", "承诺"]},
        {"content": "遗留问题：一期建设范围未明确", "tags": ["招商银行", "问题"]},
        {"content": "王志华关注点：预算审批周期长", "tags": ["招商银行", "关注点"]},
        {"content": "与华山的会议：聊 AI 辅助诊断", "tags": ["华山医院", "会议"]},
    ]
    store, _ = make_store(entries)

    with patch_store(store):
        r = get_meeting_history(client_name="招商银行")
        check("命中历史", r.get("has_history") is True)
        check("count 正确（4 条）", r.get("count") == 4, f"得到 {r.get('count')}")
        check("分类到 meetings", len(r.get("meetings", [])) == 1)
        check("分类到 commitments", len(r.get("commitments", [])) == 1)
        check("分类到 open_issues", len(r.get("open_issues", [])) == 1)
        check("分类到 focus", len(r.get("focus", [])) == 1)

        # 回归：一条记忆打了两个 tag（会议+沟通）时不能出现两次
        dup_store, _ = make_store([
            {"content": "与某银行的会议记录", "tags": ["某银行", "会议", "沟通"]},
        ])
        with patch_store(dup_store):
            r_dup = get_meeting_history(client_name="某银行")
            total = sum(len(r_dup.get(k, []))
                        for k in ("meetings", "commitments",
                                  "open_issues", "focus", "others"))
            check("多 tag 记忆不重复出现", total == 1, f"出现 {total} 次")

        # 无 tag 的记忆要进 others，不能丢
        other_store, _ = make_store([
            {"content": "关于某客户的随手记", "tags": ["某客户"]},
        ])
        with patch_store(other_store):
            r_other = get_meeting_history(client_name="某客户")
            check("无分类 tag 的进 others",
                  len(r_other.get("others", [])) == 1,
                  str(r_other.get("others"))[:80])

        # 别名：招行 → 招商银行
        r_alias = get_meeting_history(client_name="招行")
        check("别名「招行」命中", r_alias.get("has_history") is True,
              str(r_alias)[:80])
        check("别名返回的 client 是标准名",
              r_alias.get("client") == "招商银行",
              f"得到 {r_alias.get('client')}")

        # 没命中必须诚实返回 has_history=False，且给可执行提示
        r_none = get_meeting_history(client_name="完全不存在的客户XYZ")
        check("未命中 has_history=False", r_none.get("has_history") is False)
        check("未命中返回 message 提示", bool(r_none.get("message")))
        check("未命中不返回 all_memories（避免模型自己找）",
              "all_memories" not in r_none)

        # 空参数
        check("空 client_name 报错",
              "error" in get_meeting_history(client_name=""))
        check("None client_name 报错",
              "error" in get_meeting_history(client_name=None))


def test_run_prep_tool():
    print("\n【A3】run_prep_tool 分发器")
    r = run_prep_tool("search_industry_news", {"industry": "金融"})
    check("分发到简报工具", r.get("total", 0) > 0)
    r2 = run_prep_tool("query_customers", {"industry": "教育"})
    check("分发到通用业务工具", r2.get("total", 0) > 0)
    r3 = run_prep_tool("nope", {})
    check("未知工具报错", "error" in r3)
    r4 = run_prep_tool("search_industry_news",
                       {"industry": "金融", "bad_param": 1})
    check("未知参数被拦下", "error" in r4)
    check("错误信息列出合法参数", "industry" in str(r4.get("error", "")))
    r5 = run_prep_tool("search_industry_news", "{不是json")
    check("非法 JSON 字符串参数报错", "error" in r5)
    r6 = run_prep_tool("search_industry_news", ["不是", "dict"])
    check("非 dict 参数报错", "error" in r6)


# ============ B 组：渲染层 ============

def test_build_blocks():
    print("\n【B】build_blocks 渲染与诚实性")

    # --- 客户档案缺失 ---
    data = {
        "customer": None, "peers": [], "profile_error": None,
        "news": [], "news_error": None,
        "history": {"has_history": False, "message": "没有记录"},
    }
    profile, news_block, hist = build_blocks(data, "某客户", "金融", "议题")
    check("档案缺失标 ⚠", "⚠" in profile)
    check("档案缺失写明未找到", "未找到" in profile)
    check("行业动态缺失标 ⚠", "⚠" in news_block)
    check("历史无记录标 ⚠", "⚠" in hist)
    check("历史无记录说明是首次接触", "首次接触" in hist)

    # --- 客户档案存在 ---
    cust = {"id": "C-013", "name": "招商银行", "industry": "金融",
            "city": "上海", "contact": "王志华", "email": "a@b.com"}
    data2 = dict(data, customer=cust, peers=[
        {"id": "C-014", "name": "上海汇丰银行", "industry": "金融"}])
    profile2, _, _ = build_blocks(data2, "招商银行", "金融", "议题")
    check("档案含客户名", "招商银行" in profile2)
    check("档案含联系人", "王志华" in profile2)
    check("档案含同行业参考", "同行业参考客户" in profile2)

    # --- 行业填错要提示 ---
    data3 = dict(data, customer=cust, peers=[])
    profile3, _, _ = build_blocks(data3, "招商银行", "医疗", "议题")
    check("行业填错时提示 ⚠", "⚠" in profile3)
    check("提示里指出档案的真实行业", "金融" in profile3)

    # --- 历史有记录时分类渲染 ---
    data4 = dict(data, customer=cust, peers=[],
                 history={
                     "has_history": True, "count": 2,
                     "meetings": [{"id": "M0002", "content": "讨论了数据中台"}],
                     "commitments": [{"id": "M0004", "content": "两周内给报价"}],
                     "open_issues": [{"id": "M0005", "content": "一期范围未明确"}],
                     "focus": [], "others": [],
                 })
    _, _, hist4 = build_blocks(data4, "招商银行", "金融", "议题")
    check("历史渲染含过往会议", "过往会议" in hist4)
    check("历史渲染含已承诺事项", "已承诺事项" in hist4)
    check("历史渲染含遗留问题", "遗留问题" in hist4)
    check("历史渲染不含内部 ID", "M000" not in hist4,
          f"泄漏: {[w for w in hist4.split() if 'M000' in w]}")


def test_collect_data():
    print("\n【B2】collect_data 数据采集")
    # 用 mock 掉 LLM（本测试不需要真调 API）
    with mock.patch("meeting_prep_agent.run_prep_tool") as rt:
        def fake(name, args):
            if name == "query_customers":
                return {"customers": list(MOCK_CUSTOMERS), "total": len(MOCK_CUSTOMERS)}
            if name == "search_industry_news":
                return {"news": [], "total": 0}
            if name == "get_meeting_history":
                return {"has_history": False, "count": 0, "message": "无"}
            return {"error": "unexpected tool: " + name}
        rt.side_effect = fake

        d = collect_data("招商银行", "金融", "数据中台", client_llm=None,
                         trace_progress=False)
        check("目标客户找到", d["customer"]["name"] == "招商银行")
        check("同行业参考按真实行业取",
              all(p["industry"] == "金融" for p in d["peers"]),
              str([p["name"] for p in d["peers"]]))
        check("同行业参考不含目标自己",
              all(p["name"] != "招商银行" for p in d["peers"]))

        # 回归：行业写错时档案不能丢
        d2 = collect_data("招商银行", "医疗", "数据中台", client_llm=None,
                          trace_progress=False)
        check("行业写错时档案仍能找到",
              d2["customer"] and d2["customer"]["name"] == "招商银行",
              str(d2["customer"]))
        # 参考客户按**目标客户的真实行业**取，不按用户填的。
        # 档案才是事实来源——用户填错行业时，档案段会用 ⚠ 提示，
        # 但参考客户列表不该跟着错。
        check("行业写错时参考客户按目标客户真实行业取",
              all(p["industry"] == "金融" for p in d2["peers"]),
              str([p["name"] for p in d2["peers"]]))

        # 别名也要能定位
        d3 = collect_data("招行", "金融", "议题", client_llm=None,
                          trace_progress=False)
        check("别名「招行」定位到招商银行",
              d3["customer"] and d3["customer"]["name"] == "招商银行",
              str(d3["customer"]))


# ============ C 组：匹配层 ============

def test_find_customer():
    print("\n【C1】find_customer 分级匹配")
    check("精确名命中", find_customer("招商银行", MOCK_CUSTOMERS)["id"] == "C-013")
    check("客户号命中", find_customer("C-013", MOCK_CUSTOMERS)["id"] == "C-013")
    check("别名「招行」命中",
          find_customer("招行", MOCK_CUSTOMERS)["id"] == "C-013")
    check("别名「平安」命中平安银行",
          find_customer("平安", MOCK_CUSTOMERS)["name"] == "深圳平安银行")
    check("长名包含短名命中",
          find_customer("招商银行上海分行", MOCK_CUSTOMERS)["id"] == "C-013")

    # 回归：过短的模糊查询不能误命中
    # 旧逻辑 `client in name or name in client` 会让「银行」命中
    # 「深圳平安银行」、「上海」命中「上海交大教育科技」——
    # 顾问问的其实是两回事，误命中会给出错的档案
    check("「银行」不误命中", find_customer("银行", MOCK_CUSTOMERS) is None)
    check("「上海」不误命中", find_customer("上海", MOCK_CUSTOMERS) is None)
    check("「医院」不误命中", find_customer("医院", MOCK_CUSTOMERS) is None)
    check("空串不命中", find_customer("", MOCK_CUSTOMERS) is None)
    check("None 不命中", find_customer(None, MOCK_CUSTOMERS) is None)
    check("空客户列表不命中", find_customer("招商银行", []) is None)
    check("不存在的客户不命中",
          find_customer("不存在的客户", MOCK_CUSTOMERS) is None)

    # 别名表自身的一致性：每个别名都该能命中某个客户
    for alias in CLIENT_ALIASES:
        hit = find_customer(alias, MOCK_CUSTOMERS)
        check(f"别名「{alias}」可命中", hit is not None,
              CLIENT_ALIASES[alias])

    # 别名值必须指向客户库里真实存在的名字片段。
    # 踩过的坑：别名写成「中信」→「中信银行」，但客户库里的全名是
    # 「广州中信证券」，别名指向一个不存在的名字，等于失效。
    for alias, target in CLIENT_ALIASES.items():
        hit = find_customer(alias, MOCK_CUSTOMERS)
        check(f"别名「{alias}」命中的是目标客户",
              hit and target in hit["name"],
              f"期望含「{target}」，实际 {hit['name'] if hit else None}")


def test_clean_memory_ids():
    print("\n【C2】内部记忆 ID 清理")
    check("删除 ID 本身", _clean_memory_ids("M0003") == "")
    check("删除 ID 保留正文",
          _clean_memory_ids("M0005 遗留的一期建设范围") == "遗留的一期建设范围")
    check("括号内 ID 连带空括号清掉",
          _clean_memory_ids("承诺（M0010）") == "承诺")
    check("多个 ID 全清",
          "M000" not in _clean_memory_ids("见M0003和M0004"))
    # 不能误伤正常文本
    check("M30 机型不被误伤",
          _clean_memory_ids("M30 机型不错") == "M30 机型不错")
    check("普通文本不变",
          _clean_memory_ids("正常文本没有编号") == "正常文本没有编号")
    check("None 安全", _clean_memory_ids(None) == "")


def test_history_facts():
    print("\n【C3】_history_facts 给 LLM 的事实")
    h = {
        "has_history": True, "client": "招商银行", "count": 2,
        "meetings": [{"id": "M0002", "content": "讨论了数据中台"}],
        "commitments": [{"id": "M0004", "content": "两周内给报价"}],
        "open_issues": [], "focus": [], "others": [],
    }
    facts = _history_facts(h)
    check("条数正确", len(facts) == 2, str(facts))
    check("不含 id 字段", all("id" not in f for f in facts))
    check("含可读标签",
          facts[0]["标签"] == "招商银行·过往会议", str(facts[0]))
    check("内容非空", all(f["内容"] for f in facts))
    check("无历史时返回空列表",
          _history_facts({"has_history": False}) == [])
    check("None 安全", _history_facts(None) == [])

    # 标签里的客户名不能来自 id
    check("标签不含内部编号",
          not any("M000" in f["标签"] for f in facts))

    # 分类顺序与渲染层一致
    check("HISTORY_GROUPS 覆盖全部 5 类", len(HISTORY_GROUPS) == 5)
    check("HISTORY_GROUPS 键名对应工具返回字段",
          [k for _, k in HISTORY_GROUPS] ==
          ["meetings", "commitments", "open_issues", "focus", "others"])


# ============ runner ============

def main():
    print("=" * 60)
    print("  会前简报链路测试（纯逻辑，零 API 调用）")
    print("=" * 60)

    test_search_industry_news()
    test_get_meeting_history()
    test_run_prep_tool()
    test_build_blocks()
    test_collect_data()
    test_find_customer()
    test_clean_memory_ids()
    test_history_facts()

    print("\n" + "=" * 60)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"结果：{RESULTS['pass']}/{total} 通过")
    if RESULTS["fail"]:
        print("✗ 有用例失败")
        sys.exit(1)
    print("✓ 简报链路全部正常")


if __name__ == "__main__":
    main()
