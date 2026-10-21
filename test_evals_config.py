"""
test_evals_config.py — 评估用例配置的自检

为什么需要这个文件？
  用例外置成 YAML 之后，配置本身成了新的失败来源：
    · YAML 语法错 → 整个评估跑不起来
    · id 重复     → --only 跑错用例，还不报错
    · category 写错 → 报告分组错，但不崩，静默出错最难查
    · min_hits > 关键词数 → 这个用例永远不可能通过，
      会被误当成「模型不行」

  这些都属于「配置错误」，必须在跑评估**之前**就报出来，
  而不是变成一个红色的评估结果让人误判模型能力。

运行：
    python test_evals_config.py
"""

import os
import sys
import tempfile

import yaml

# 从 eval_cases 导入，不从 eval_mcp_agent 导入——
# 后者顶部 import fastmcp，而 fastmcp 只装在 .venv-agents 隔离环境里。
# 配置校验不该依赖运行框架：这是「解析」和「使用」分离的一般做法。
from eval_cases import BASELINE_FILE, CASES_FILE, load_cases

# 真实用例集。放在这里而不是从 eval_mcp_agent 导入，
# 是为了让配置校验不依赖 fastmcp（它只装在 .venv-agents 隔离环境里）
CASES = load_cases()

RESULTS = {"pass": 0, "fail": 0}


def check(label, cond, detail=""):
    ok = bool(cond)
    RESULTS["pass" if ok else "fail"] += 1
    print(f"  {'✓' if ok else '✗'} {label}"
          + (f"  → {detail}" if not ok and detail else ""))
    return ok


def write_yaml(text):
    """在临时目录写一个 YAML 文件，返回路径"""
    d = tempfile.mkdtemp(prefix="evalcfg_")
    p = os.path.join(d, "cases.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def test_real_file():
    print("\n【A】真实的用例文件")
    check("用例文件存在", os.path.exists(CASES_FILE), CASES_FILE)
    check("用例非空", len(CASES) > 0)
    check("用例数 ≥ 14", len(CASES) >= 14, f"实际 {len(CASES)}")

    ids = [c["id"] for c in CASES]
    check("id 唯一", len(ids) == len(set(ids)),
          f"重复: {[i for i in set(ids) if ids.count(i) > 1]}")
    check("id 从 1 开始连续",
          sorted(ids) == list(range(1, len(ids) + 1)),
          f"实际 {sorted(ids)}")

    check("每个用例都有 question",
          all(c.get("question") for c in CASES))
    check("每个用例都有 category",
          all(c.get("category") for c in CASES))
    check("每个用例都指定了 expect_tool 或显式为 null",
          all("expect_tool" in c for c in CASES))
    check("每个用例都有 must_contain",
          all(c.get("must_contain") for c in CASES))

    # 关键：must_contain 默认为同义词 OR 关系，
    # min_hits 不该超过关键词数
    bad = [c["id"] for c in CASES
           if c.get("min_hits", 1) > len(c.get("must_contain") or [])]
    check("min_hits 不超过关键词数", not bad, f"用例 {bad}")

    # 至少要有一个「不编造」的用例，否则评估测不出幻觉
    with_must_not = [c["id"] for c in CASES if c.get("must_not")]
    check("存在检测编造的用例（must_not）", len(with_must_not) >= 2,
          f"实际 {with_must_not}")

    # 至少要有一个「查不到数据」的用例
    empty_cases = [c["id"] for c in CASES if c["category"] == "empty"]
    check("存在零结果场景用例", len(empty_cases) >= 2, f"实际 {empty_cases}")

    # 真实文件里不能有裸数字关键词。
    # YAML 会把裸 0 解析成 int，`k in answer` 时抛
    # TypeError: 'in <string>' requires string as left operand, not int
    non_str = [
        (c["id"], k, type(k).__name__)
        for c in CASES
        for k in (c.get("must_contain") or []) + (c.get("must_not") or [])
        if not isinstance(k, str)
    ]
    check("真实用例里没有非字符串关键词", not non_str, str(non_str))


def test_validation():
    print("\n【B】配置错误的拦截")
    # 文件不存在
    try:
        load_cases("/tmp/绝对不存在的路径_xyz.yaml")
        check("文件不存在时报错", False)
    except FileNotFoundError as e:
        check("文件不存在时报错", "不存在" in str(e))

    # id 重复
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q1\n"
        "    expect_tool: t\n"
        "    must_contain: [a]\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q2\n"
        "    expect_tool: t\n"
        "    must_contain: [b]\n"
    )
    try:
        load_cases(p)
        check("id 重复时报错", False)
    except ValueError as e:
        check("id 重复时报错", "重复" in str(e), str(e))

    # category 写错
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: 不存在的分类\n"
        "    question: q\n"
        "    expect_tool: t\n"
        "    must_contain: [a]\n"
    )
    try:
        load_cases(p)
        check("未知 category 报错", False)
    except ValueError as e:
        check("未知 category 报错", "category" in str(e), str(e))

    # min_hits 超过关键词数
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: t\n"
        "    must_contain: [a]\n"
        "    min_hits: 5\n"
    )
    try:
        load_cases(p)
        check("min_hits 超界报错", False)
    except ValueError as e:
        check("min_hits 超界报错", "min_hits" in str(e), str(e))

    # expect_tool 类型错
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: [a, b]\n"
        "    must_contain: [a]\n"
    )
    try:
        load_cases(p)
        check("expect_tool 类型错报错", False)
    except ValueError as e:
        check("expect_tool 类型错报错", "expect_tool" in str(e), str(e))

    # 空 cases
    p = write_yaml("cases:\n")
    try:
        load_cases(p)
        check("空 cases 报错", False)
    except ValueError as e:
        check("空 cases 报错", "cases" in str(e), str(e))

    # expect_tool 为 null 是合法的（表示「不该调工具」）
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: null\n"
        "    must_contain: [a]\n"
    )
    try:
        cases = load_cases(p)
        check("expect_tool 为 null 合法", len(cases) == 1)
    except ValueError as e:
        check("expect_tool 为 null 合法", False, str(e))

    # 关键词是裸数字——YAML 会解析成 int。
    # 这是用例外置成 YAML 之后真实踩到的坑：
    # must_contain: [没有, 0, 未找到] 里的 0 是 int，
    # 跑到 `k in answer` 那行才炸，看起来像代码 bug 其实是配置 bug。
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: t\n"
        "    must_contain: [没有, 0, 未找到]\n"
    )
    try:
        load_cases(p)
        check("must_contain 里有裸数字时报错", False)
    except ValueError as e:
        check("must_contain 里有裸数字时报错", "非字符串" in str(e), str(e))
        check("报错信息提示加引号", "引号" in str(e), str(e))

    # must_not 同样要求字符串
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: t\n"
        "    must_contain: [a]\n"
        "    must_not: [123]\n"
    )
    try:
        load_cases(p)
        check("must_not 里有裸数字时报错", False)
    except ValueError as e:
        check("must_not 里有裸数字时报错", "非字符串" in str(e), str(e))

    # 加了引号的数字是合法的
    p = write_yaml(
        "cases:\n"
        "  - id: 1\n"
        "    category: normal\n"
        "    question: q\n"
        "    expect_tool: t\n"
        "    must_contain: [\"没有\", \"0\"]\n"
    )
    try:
        cases = load_cases(p)
        check("带引号的数字关键词合法",
              cases and all(isinstance(k, str)
                            for k in cases[0]["must_contain"]))
    except ValueError as e:
        check("带引号的数字关键词合法", False, str(e))


def test_yaml_roundtrip():
    print("\n【C】YAML 与 Python 字典等价")
    # 用例从 YAML 读出来的结构，应该和写进 YAML 时一致
    with open(CASES_FILE, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    check("顶层有 cases 键", "cases" in raw)
    check("cases 是列表", isinstance(raw.get("cases"), list))
    check("每条都有 id/category/question",
          all({"id", "category", "question"} <= set(c)
              for c in raw["cases"]))

    # note 字段不能影响评估逻辑（它是给人看的）
    with_note = [c for c in CASES if c.get("note")]
    check("部分用例带 note（给人看的）", len(with_note) >= 5,
          f"实际 {len(with_note)}")
    check("note 不参与评估字段",
          all("note" not in ("expect_tool", "must_contain")
              for c in CASES))


def main():
    print("=" * 60)
    print("  评估用例配置自检")
    print("=" * 60)

    test_real_file()
    test_validation()
    test_yaml_roundtrip()

    print("\n" + "=" * 60)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"结果：{RESULTS['pass']}/{total} 通过")
    if RESULTS["fail"]:
        print("✗ 有用例失败")
        sys.exit(1)
    print("✓ 用例配置全部正常")


if __name__ == "__main__":
    main()
