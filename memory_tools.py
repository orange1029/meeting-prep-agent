"""
memory_tools.py — 记忆相关的 2 个 Tool（工具）

在原有 4 个业务工具之外，加：
  save_memory   记住一条事实/偏好/观察
  recall_memory 主动回忆与当前问题相关的记忆

为什么需要 recall_memory？记忆已经自动注入 prompt 了，
为什么还要一个显式工具？
  因为自动注入靠关键词打分，是「猜」的——用户问「他上次说的那个方案」
  这种指代，关键词打分可能召不回。有了显式工具，模型可以在
  关键词召回失败时主动换词再试一次。这是自动注入的兜底，不是替代。
"""

import json
import os

from memory_store import MEMORY_TYPES, MemoryStore

# 进程内单例。所有工具共用同一个 store，否则会出现
# 「save_memory 写进 A 实例，recall_memory 从 B 实例读」的诡异问题
_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE


# ============ Tool: save_memory ============

save_memory = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "记住一条需要跨对话长期保留的信息。当用户说「记住」「以后都要」"
            "「我的习惯是」「X 的邮箱是」，或你从对话中确认了某个对未来对话有用的"
            "客观事实（联系人、邮箱、金额约定、口径）时使用。"
            "只记对未来回答确实有帮助的内容，不要记闲聊和一次性数据。"
            "重复或矛盾的信息不必重复记——重复内容会浪费检索额度。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "要记住的内容，写成完整、可独立理解的陈述句。"
                        "反例（信息不足）：'张伟的邮箱'；"
                        "正例：'客户张伟的邮箱是 zhangwei@shjtu-edu.com'"
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["fact", "preference", "observation"],
                    "description": (
                        "fact=客观事实（联系人、邮箱、金额）；"
                        "preference=用户偏好或约定（回答风格、单位口径）；"
                        "observation=你基于数据的推测（趋势判断），"
                        "这类要标明是不确定的信息。"
                    ),
                },
            },
            "required": ["content", "type"],
        },
    },
}


def save_memory_impl(content="", type="fact", **kwargs):
    """工具实现：写入一条记忆"""
    if not (content or "").strip():
        return {"error": "记忆内容为空，请说明要记住什么"}
    if type not in MEMORY_TYPES:
        return {"error": f"type 取值无效：'{type}'。"
                        f"合法值：{', '.join(MEMORY_TYPES)}"}
    store = get_store()
    ok, msg = store.add(content.strip(), type)
    return {
        "saved": ok,
        "message": msg,
        "content": content.strip(),
        "type": type,
        "total_memories": len(store.memories),
    }


# ============ Tool: recall_memory ============

recall_memory = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": (
            "主动检索长期记忆。当用户提到「上次」「之前」「以前」"
            "「我跟你说过的」「你还记得」，或当前问题明显依赖历史信息而你"
            "在系统提示中没看到相关内容时，调用它换关键词再搜一次。"
            "注意：相关记忆通常已自动注入提示，你不必每轮都调用；"
            "仅在自动召回失败、或用户明确要求回忆时才用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索用的关键词。用具体名词而非整句问法——"
                        "检索靠关键词匹配，「张伟 邮箱」比「他之前说的邮箱是多少」更有效。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最大 10。",
                },
            },
            "required": ["query"],
        },
    },
}


def recall_memory_impl(query="", top_k=5, **kwargs):
    """工具实现：检索记忆"""
    query = (query or "").strip()
    if not query:
        return {"error": "query 为空，请给出检索关键词"}
    store = get_store()
    hits = store.retrieve(query, top_k=max(1, min(int(top_k or 5), 10)))
    return {
        "query": query,
        "found": len(hits),
        "memories": hits,
        "text": store.render_for_prompt(hits),
    }


# ============ 组装 ============

MEMORY_TOOLS = [save_memory, recall_memory]

MEMORY_REGISTRY = {
    "save_memory": save_memory_impl,
    "recall_memory": recall_memory_impl,
}


if __name__ == "__main__":
    # 打印 Schema 并做基本自检
    print(f"共 {len(MEMORY_TOOLS)} 个记忆工具\n")
    for t in MEMORY_TOOLS:
        fn = t["function"]
        print(f"  {fn['name']}  参数：{list(fn['parameters']['properties'])}"
              f"  必填：{fn['parameters']['required']}")
    errs = []
    for t in MEMORY_TOOLS:
        fn = t["function"]
        if t["type"] != "function":
            errs.append(f"{fn['name']}: type 错误")
        if not fn.get("description"):
            errs.append(f"{fn['name']}: 缺 description")
        p = fn["parameters"]
        if p.get("type") != "object":
            errs.append(f"{fn['name']}: parameters.type 错误")
        for k, v in p["properties"].items():
            if "type" not in v or "description" not in v:
                errs.append(f"{fn['name']}.{k}: 缺 type 或 description")
        for r in p["required"]:
            if r not in p["properties"]:
                errs.append(f"{fn['name']}: required 里的 {r} 未定义")
    print("\n" + ("✗ " + "; ".join(errs) if errs else "✓ 格式校验通过"))
    print(f"记忆文件路径：{get_store().path}")