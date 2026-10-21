"""
eval_cases.py — 评估用例的加载与校验

为什么要独立成模块？
  用例数据在 evals/mcp_agent_cases.yaml。加载逻辑原本在
  eval_mcp_agent.py 里，但那个文件顶部 import fastmcp——
  而 fastmcp 只装在 .venv-agents 隔离环境里（Agent 框架绝不能装进
  业务项目主环境，理由见 2026-10-12 的记忆）。

  结果：主环境跑 pytest 时，import eval_mcp_agent 就炸，
  连「只想校验 YAML 配置」这件事都做不了。

  拆开之后：
    eval_cases.py      —— 只依赖 yaml，主环境和隔离环境都能跑
    eval_mcp_agent.py  —— 跑评估，需要 fastmcp

  这是「让配置校验不依赖运行框架」的一般性做法：
  配置解析和配置使用分开，才能单独测配置。
"""

import os

EVALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals")
CASES_FILE = os.path.join(EVALS_DIR, "mcp_agent_cases.yaml")
BASELINE_FILE = os.path.join(EVALS_DIR, "baseline.json")

KNOWN_CATEGORIES = {
    "normal", "cross", "empty", "ambiguous",
    "hard_multi", "hard_ambiguous", "hard_wrong_tool", "hard_invalid",
}


def load_cases(path=CASES_FILE):
    """从 YAML 加载评估用例，并做基本校验。

    校验什么：
      · id 唯一 —— 重号会让 --only 跑错用例
      · category 在已知集合里 —— 分类是报告分组的基础，写错了报告就分组错
      · expect_tool 要么是字符串要么是 None —— None 表示「不该调工具」
      · min_hits 不超过关键词数 —— 否则这个用例永远不可能通过
      · 关键词全是字符串 —— YAML 会把裸 0 解析成整数
    """
    import yaml

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"评估用例文件不存在：{path}\n"
            f"它应该在仓库的 evals/ 目录下。"
        )

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cases = (data or {}).get("cases") or []
    if not cases:
        raise ValueError(f"{path} 里没有 cases，或 cases 是空的")

    seen = set()
    for c in cases:
        cid = c.get("id")
        if cid is None:
            raise ValueError(f"{path} 里有个用例没有 id")
        if cid in seen:
            raise ValueError(f"{path} 里 id={cid} 重复了")
        seen.add(cid)

        cat = c.get("category")
        if cat not in KNOWN_CATEGORIES:
            raise ValueError(
                f"用例 {cid} 的 category='{cat}' 不在已知集合里："
                f"{sorted(KNOWN_CATEGORIES)}"
            )

        tool = c.get("expect_tool")
        if tool is not None and not isinstance(tool, str):
            raise ValueError(
                f"用例 {cid} 的 expect_tool 应该是字符串或 null，实际是 "
                f"{type(tool).__name__}"
            )

        # must_contain 的 min_hits 不能大于关键词总数，
        # 否则这个用例永远不可能通过——是配置错误，不是模型能力问题
        keywords = c.get("must_contain") or []
        min_hits = c.get("min_hits", 1)
        if min_hits > len(keywords):
            raise ValueError(
                f"用例 {cid} 的 min_hits={min_hits} 超过了 "
                f"must_contain 的关键词数量 {len(keywords)}，永远不可能通过"
            )

        # 关键词必须全是字符串。
        # 这是用例外置成 YAML 之后踩的坑：YAML 会把裸 0 解析成整数，
        # 写成 must_contain: [没有, 0, 未找到] 时那个 0 是 int，
        # 后面 `k in answer` 直接抛
        # TypeError: 'in <string>' requires string as left operand, not int
        # 而报错发生在跑评估的中途，看起来像是代码 bug，其实是配置 bug。
        # 所以在加载时就拦下来。
        for k in keywords:
            if not isinstance(k, str):
                raise ValueError(
                    f"用例 {cid} 的 must_contain 里有非字符串关键词 {k!r}"
                    f"（类型 {type(k).__name__}）。"
                    f"YAML 会把裸数字解析成整数，数字关键词要加引号：\"{k}\""
                )
        for k in (c.get("must_not") or []):
            if not isinstance(k, str):
                raise ValueError(
                    f"用例 {cid} 的 must_not 里有非字符串关键词 {k!r}"
                    f"（类型 {type(k).__name__}）"
                )

    return cases
