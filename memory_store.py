"""
memory_store.py — 长期记忆存储层

解决什么问题？
  run_chat.py 的对话历史（messages）在进程退出就没了。
  重开程序问「上次那个上海客户后来怎么样了」，它完全不记得。
  长期记忆（long-term memory）让 Agent 跨会话保留事实与偏好。

为什么不用向量数据库？
  本项目阶段用**关键词打分检索**就够：
    · 零依赖，不用装 embedding 模型、不用调 embedding API
    · 可解释——能看清「为什么这条记忆被召回」
    · 记忆条数在几十~几百条量级时，关键词检索的召回效果不输向量检索
  什么时候该换向量检索？记忆上千条、或用户表达与记忆用词完全不同
  （如用户说「我那个餐饮行业的客户」，记忆里写「餐饮」）。届时替换
  retrieve() 内部实现即可，上层无需改动。

数据落在哪？
  memory/memories.json —— 纯文本，可直接打开看、可手改、可进 git

设计上的三个关键决策：
  1. **记忆有类型**：fact（事实）/ preference（偏好）/ observation（观察）
     不同类型的信任度不同，observation（模型自己的推测）不该当事实用
  2. **记忆会被强化**：被反复召回的条目 hit_count 上升，
     长期不命中的会衰减并最终遗忘——避免记忆库无限膨胀成垃圾堆
  3. **去重靠模型**：写入前先检索相似条目，由调用方（模型）决定
     是否真的重复。本层只负责存储与检索，不做语义判断
"""

import json
import os
import re
import time
from datetime import datetime

# 记忆类型及其说明（会拼进 prompt 告诉模型）
MEMORY_TYPES = {
    "fact": "客观事实，如「客户张伟的邮箱是 zhangwei@x.com」",
    "preference": "用户偏好，如「回答要简洁」「金额用万元」",
    "observation": "基于数据的推测，如「上海区教育行业增长放缓」",
}

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "memories.json")

# 停用词：中文高频虚词 + 英文常见词。
# 注意：这些单字只在「独立成词」时才算停用词，不能无条件从分词里剔除——
# 「上」是虚词，但「上海」「上门」里它是实义单字。
# 所以在 tokenize() 里对停用词做单字级豁免。
STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "也", "你", "他",
    "她", "它", "们", "这", "那", "个", "上", "下", "中", "为", "以", "与",
    "the", "a", "an", "of", "to", "is", "are", "in", "on", "and", "or",
}

# 高频双字虚词组合：从分词里剔除，避免污染相似度计算
STOP_BIGRAMS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "可以", "已经",
    "现在", "一下", "一些", "所有", "然后", "但是", "所以", "因为",
}

# 同义/近义扩展：关键词检索的最大短板是「用户换个说法就召不回」。
# 解决办法是查表把常见代词映射到标准词。这不是真正的语义理解，
# 但覆盖了对话里最高频的几类指代，性价比很高。
# 想彻底解决要换向量检索，见模块开头说明。
SYNONYMS = {
    # 身份类指代
    "我是谁": ["姓名", "名字", "叫", "身份", "职位"],
    "你记得我": ["姓名", "身份", "叫"],
    "我叫什么": ["姓名", "名字", "叫"],
    "老王": ["王磊"],
    # 偏好类指代
    "偏好": ["喜欢", "习惯", "风格", "口径"],
    "风格": ["偏好", "习惯"],
    "单位": ["口径", "万元", "元"],
    "口径": ["单位", "偏好"],
    # 数据类指代
    "业绩": ["成交", "成交额", "金额"],
    "销售额": ["成交额", "成交"],
    "客户名单": ["客户", "客户列表"],
    "行业": ["客户", "industry"],
    "城市": ["city", "地区"],
}


def expand_tokens(tokens, original=""):
    """
    按同义表扩展分词。原词全部保留，扩展词是加分项而非替代。

    双向匹配：查询侧扩展出的词，若记忆内容含该词的**同义变体**，
    也算命中。否则「我叫什么」扩展出「姓名」，但记忆写的是
    「用户叫王磊」——「姓名」并不字面出现在记忆里，永远匹配不上。
    """
    extra = []
    for key, vals in SYNONYMS.items():
        if key in (original or ""):
            extra.extend(vals)

    # 反向映射：扩展词 -> 同组其他词，记忆侧也做同样处理
    expanded_set = set(tokens) | set(extra)
    for key, vals in SYNONYMS.items():
        if expanded_set & set(vals):
            expanded_set |= set(vals) | {key}

    return sorted(expanded_set)


# ============ 分词与打分 ============

def tokenize(text):
    """
    分词：中英混合的简易分词。
    中文没有空格，用「单字 + 双字组合」的方式近似切分。
    这不是真正的分词器，但对关键词召回够用——
    真正要语义检索时才需要 jieba 或向量模型。

    停用词策略：**只剔除单独成词的单字停用词与停用双字组合**。
    「上海」的「上」不能剔（实义），「我的」的「的」要剔（虚词）——
    判断方法：若该字两侧构成的组合都不在实义词表里，就当虚词。
    这个判断天然有误差，但比无条件剔除安全得多：
    宁可多留几个虚词（轻微增加噪音），也不能删掉实义词（丢失召回）。
    """
    text = (text or "").lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", text)
              if t not in STOPWORDS]

    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        n = len(run)
        bigrams = [run[i:i + 2] for i in range(n - 1)]
        bigram_set = set(bigrams)

        # 判定哪些位置的停用单字是「虚词」：
        # 该字若构成任意一个非停用双字组合，则为实义，保留
        drop = set()
        for i, ch in enumerate(run):
            if ch not in STOPWORDS:
                continue
            combos = []
            if i > 0:
                combos.append(run[i - 1:i + 1])
            if i < n - 1:
                combos.append(run[i:i + 2])
            # 所有组合都是停用组合 → 纯虚词，剔除
            if combos and all(c in STOP_BIGRAMS or c in STOPWORDS
                              for c in combos):
                drop.add(i)

        tokens.extend(ch for i, ch in enumerate(run) if i not in drop)
        tokens.extend(bg for bg in bigrams if bg not in STOP_BIGRAMS)

    return [t for t in tokens if t]


def similarity(query_tokens, memory_tokens):
    """
    相似度 = 交集 / 平方根乘积（Jaccard 的改良）。
    用改良版而非纯 Jaccard：记忆长短差异大时，纯 Jaccard 会
    严重惩罚长记忆（短查询 vs 长记忆交集小），改良版能缓解。

    另外对**双字词**加权：中文字符是单字切分，像「上海」「行业」
    这种双字词比「上」「海」有意义得多，加权后精确匹配排得更前。
    """
    if not query_tokens or not memory_tokens:
        return 0.0
    q, m = set(query_tokens), set(memory_tokens)
    inter = len(q & m)

    # 双字词（长度2 的中文 token）额外加权
    q_bi = {t for t in query_tokens if len(t) == 2 and _is_cjk(t)}
    m_bi = {t for t in memory_tokens if len(t) == 2 and _is_cjk(t)}
    bi_hit = len(q_bi & m_bi)
    # 加权后必须重新归一化，否则相似度会 >1（自己比自己能得 1.5）
    return min(1.0, (inter + bi_hit) / ((len(q) * len(m)) ** 0.5))


def _is_cjk(s):
    """判断是否含中日韩统一表意文字"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


# ============ 存储主体 ============

class MemoryStore:
    def __init__(self, path=MEMORY_FILE, decay_after_days=30):
        self.path = path
        self.decay_after_days = decay_after_days
        self.memories = []
        self._load()

    # ---- 读写 ----

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.memories = data.get("memories", [])
            except (json.JSONDecodeError, OSError) as e:
                print(f"[memory] 读取失败（{e}），从空库开始")
                self.memories = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "memories": self.memories,
        }
        # 先写临时文件再替换，避免写一半崩溃导致文件损坏
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 写入 ----

    def _next_id(self):
        """
        生成下一个记忆 ID。
        不能用 len(self.memories)+1 —— 删除记忆后编号会回退，
        导致新记忆与旧记忆撞 ID（实测出现过两个 M0004），
        进而让 forget(mem_id=...) 删错条目。必须扫描现存最大编号 +1。
        """
        mx = 0
        for m in self.memories:
            digits = re.sub(r"\D", "", str(m.get("id", "")))
            if digits.isdigit():
                mx = max(mx, int(digits))
        return f"M{mx + 1:04d}"

    def add(self, content, mtype="fact", tags=None, source="manual"):
        """
        写入一条记忆。返回 (是否成功, 信息字符串)
        去重判断交给调用方——本层只管存。
        """
        content = (content or "").strip()
        if not content:
            return False, "记忆内容为空"
        if mtype not in MEMORY_TYPES:
            return False, f"未知的记忆类型 '{mtype}'，合法值：{list(MEMORY_TYPES)}"

        self.memories.append({
            "id": self._next_id(),
            "content": content,
            "type": mtype,
            "tags": tags or [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_hit_at": None,
            "hit_count": 0,
            "source": source,
        })
        self._save()
        return True, f"已记住（{self.memories[-1]['id']}）"

    def add_many(self, items, source="auto"):
        """批量写入，供每轮自动提取用。返回成功条数"""
        n = 0
        for it in items:
            ok, _ = self.add(
                it.get("content", ""),
                it.get("type", "fact"),
                it.get("tags"),
                source=source,
            )
            n += ok
        return n

    # ---- 检索 ----

    def retrieve(self, query, top_k=5, min_score=0.15, types=None,
                 strong_score=0.30):
        """
        按关键词相似度召回记忆。
          top_k        最多返回几条
          min_score    低于此分直接丢弃
          strong_score 达到此分才算「强相关」，
                       弱相关记忆仍然返回，但渲染时标为参考
          types        只召回指定类型，None 表示全部

        两道阈值的意义：
          单独一个阈值要么漏（太严）要么噪（太松）。
          分级后可做到「宁缺毋滥但不漏」——强相关一定给，
          弱相关只作参考，模型自己判断能不能用。

        实测校准依据（记忆库：用户偏好万元 + 用户叫王磊）：
          「我叫什么」→ 0.08   「金额单位怎么表述」→ 0.30
          「北京客户名单」→ 0.05（噪音，应滤掉）
        """
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        q_tokens = expand_tokens(q_tokens, query)

        scored = []
        for m in self.memories:
            if types and m["type"] not in types:
                continue
            # 标签也参与匹配，方便「教育行业相关记忆」这类按标签召回
            haystack = m["content"] + " " + " ".join(m.get("tags", []))
            # 记忆侧同样做同义扩展，保证双向匹配
            m_tokens = expand_tokens(tokenize(haystack), haystack)
            score = similarity(q_tokens, m_tokens)
            if score >= min_score:
                scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits = scored[:top_k]

        # 命中即强化
        now = datetime.now().isoformat(timespec="seconds")
        for _, m in hits:
            m["hit_count"] = m.get("hit_count", 0) + 1
            m["last_hit_at"] = now
        if hits:
            self._save()

        return [{"score": round(s, 3),
                 "relevance": "strong" if s >= strong_score else "weak",
                 **m} for s, m in hits]

    # ---- 维护 ----

    def decay_and_forget(self):
        """
        衰减与遗忘：
          超过 decay_after_days 未被召回的记忆，删除。
        理由：长期不命中的记忆要么已过时，要么压根没用，
              留在库里只会挤占上下文、干扰召回精度。
        返回删除条数。
        """
        cutoff = time.time() - self.decay_after_days * 86400
        keep = []
        removed = 0
        for m in self.memories:
            hit = m.get("last_hit_at")
            if hit and m.get("hit_count", 0) > 0:
                try:
                    if datetime.fromisoformat(hit).timestamp() < cutoff:
                        removed += 1
                        continue
                except ValueError:
                    pass
            elif not hit:
                # 从未命中过的新记忆，给它一个宽限期
                try:
                    if datetime.fromisoformat(m["created_at"]).timestamp() < cutoff:
                        removed += 1
                        continue
                except ValueError:
                    pass
            keep.append(m)
        if removed:
            self.memories = keep
            self._save()
        return removed

    def forget(self, mem_id=None, contains=None):
        """
        遗忘。指定 id 删单条，或按内容关键词删多条。
        返回删除条数。
        """
        if not self.memories:
            return 0
        if mem_id:
            before = len(self.memories)
            self.memories = [m for m in self.memories if m["id"] != mem_id]
            n = before - len(self.memories)
        elif contains:
            before = len(self.memories)
            self.memories = [
                m for m in self.memories
                if not all(k in m["content"] for k in [contains])
            ]
            n = before - len(self.memories)
        else:
            return 0
        if n:
            self._save()
        return n

    def stats(self):
        """统计信息，供 /memory 命令展示"""
        by_type = {}
        for m in self.memories:
            by_type[m["type"]] = by_type.get(m["type"], 0) + 1
        never_hit = sum(1 for m in self.memories if m.get("hit_count", 0) == 0)
        most = sorted(self.memories,
                      key=lambda m: m.get("hit_count", 0),
                      reverse=True)[:3]
        return {
            "total": len(self.memories),
            "by_type": by_type,
            "never_hit": never_hit,
            "top_hits": [(m["id"], m["hit_count"], m["content"][:30])
                         for m in most if m.get("hit_count", 0) > 0],
        }

    def render_for_prompt(self, memories, only_strong=False):
        """
        把召回的记忆渲染成可注入 system prompt（系统提示词）的文本。
        only_strong=True 时只渲染强相关记忆——自动召回建议用这个模式，
        避免把弱相关内容塞进上下文干扰模型。
        """
        if not memories:
            return ""
        if only_strong:
            strong = [m for m in memories if m.get("relevance") == "strong"]
            # 全部都是弱相关时，宁可不注入也别塞噪音
            if not strong:
                return ""
            memories = strong

        lines = ["【你此前记住的信息】"]
        for m in memories:
            tag = "" if m.get("relevance") == "strong" else "（可能相关，供参考）"
            lines.append(f"- （{m['type']}）{m['content']}{tag}")
        lines.append("若这些信息与用户当前说法冲突，以用户当前说法为准，并考虑更新记忆。")
        return "\n".join(lines)


# ============ 供 Agent 调用的高层接口 ============

def recall(query, store=None, top_k=5):
    """召回相关记忆，返回可直接注入 prompt 的文本"""
    store = store or MemoryStore()
    return store.render_for_prompt(store.retrieve(query, top_k=top_k))


def remember(items, store=None):
    """
    批量记住（供模型自动提取后调用）。
    items: [{"content": "...", "type": "fact|observation|preference"}]
    """
    store = store or MemoryStore()
    return store.add_many(items, source="auto")