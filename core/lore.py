"""设定集（Lore）：角色的世界、往事、关系、习惯。

它和「记忆」是两回事，别混：

    设定集 = 你说给她听的（作者写的，静态，永不衰减）
    记忆   = 她和你聊出来的（模型抽的，动态，会衰减也会被修正）

混在一起的后果很具体：把三年前的剧情当"记忆"参与向量召回，会挤掉
真正刚发生的对话；把聊天记忆写回人设，角色会被你自己的话慢慢改掉。

检索策略（两级）：
  1. 关键词命中——精确，可控，作者写了什么就触发什么
  2. 向量兜底——用户换个说法（"那次事故" → "龟岛那回"）也能命中
永远注入的条目（always=true）不受预算限制，但要自己控制数量。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from .memory import cosine, embed

KINDS = {
    "world": "世界观",
    "history": "往事",
    "place": "地点",
    "person": "人物",
    "habit": "习惯",
    "rule": "规则",
}

DEFAULT_BUDGET = 1400      # 注入的总字数上限
TIMELINE_BUDGET = 2600     # 「讲讲你的过去」时放宽，够放 5~6 段经历
TIMELINE_MAX = 6           # 时间线最多几段
MAX_ENTRY_CHARS = 400      # 单条截断
VECTOR_MIN_SCORE = 0.11    # 向量兜底的最低相似度（实测：相关 0.13~0.32，无关 <0.06）
MAX_VECTOR_HITS = 3        # 向量命中最多条数，防止噪声稀释上下文
KIND_BONUS = {"history": 2.5, "person": 0.3}   # 往事是"她自己"，比世界观更该被提起

# 「想听你过去」的强信号。命中就走时间线，不再靠关键词逐条碰运气。
PAST_HINTS = (
    "故事", "过去", "往事", "经历", "回忆", "以前", "从前", "当年", "小时候",
    "你的人生", "你的来历", "怎么来的", "发生过什么", "经历过什么",
    "讲讲你", "说说你", "介绍一下你", "你的身世", "那时候", "后来呢",
)
# 弱信号：单独出现不足以判定，要配合疑问语气
WEAK_PAST_HINTS = ("记得", "曾经", "那段", "那件事", "那天", "当时")
QUESTION_MARKS = ("吗", "么", "什么", "哪", "还", "怎么", "为什么", "是不是")

CHAPTER_RE = re.compile(r"【\s*(\d+)\s*(?:ST|ND|RD|TH)", re.I)


def is_past_intent(query: str) -> bool:
    """判断对方是不是在问「你过去的事」。

    这条判断很关键：只靠关键词命中，「给我讲讲你的故事」这种泛问
    永远碰不到「阿派朗岛」「平凡结」这种具体触发词，于是模型只能回答
    「我没有故事可讲」——这是实测出现过的真实故障。
    """
    q = (query or "").strip()
    if not q:
        return False
    if any(h in q for h in PAST_HINTS):
        return True
    weak = sum(1 for h in WEAK_PAST_HINTS if h in q)
    if weak >= 2:
        return True
    if weak == 1 and any(m in q for m in QUESTION_MARKS):
        return True
    return False


def _chapter_order(title: str) -> int:
    """从「【7TH · 孤独之歌】」里取章节号用来排序；非章节标题排在后面。"""
    m = CHAPTER_RE.search(title or "")
    return int(m.group(1)) if m else 999


def _eid(title: str, n: int) -> str:
    h = hashlib.sha256(f"{title}:{n}".encode("utf-8")).hexdigest()[:8]
    return f"lore-{h}"


class LoreStore:
    def __init__(self, cards_root: str, world_path: str = ""):
        self.root = cards_root
        # 共用世界观：所有角色同处一个故事体系，这部分不该重复写 162 遍
        self.world_path = world_path or os.path.join(
            os.path.dirname(os.path.abspath(cards_root)), "data", "world.json")

    def _path(self, slug: str) -> str:
        return os.path.join(self.root, slug, "lore.json")

    def load(self, slug: str) -> list[dict]:
        p = self._path(slug)
        if not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") if isinstance(data, dict) else data
            return [e for e in (entries or []) if isinstance(e, dict)]
        except Exception:
            return []

    def save(self, slug: str, entries: list[dict]):
        d = os.path.join(self.root, slug)
        os.makedirs(d, exist_ok=True)
        with open(self._path(slug), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries}, f, ensure_ascii=False, indent=2)

    def upsert(self, slug: str, entry: dict) -> dict:
        entries = self.load(slug)
        now = time.time()
        title = (entry.get("title") or "").strip()
        content = (entry.get("content") or "").strip()
        if not title or not content:
            raise ValueError("设定条目需要标题和内容")

        kw = entry.get("keywords")
        if isinstance(kw, str):
            kw = [x.strip() for x in re.split(r"[,，、\s]+", kw) if x.strip()]
        item = {
            "id": entry.get("id") or _eid(title, len(entries)),
            "title": title,
            "kind": entry.get("kind") if entry.get("kind") in KINDS else "history",
            "keywords": [str(x) for x in (kw or []) if str(x).strip()],
            "content": content,
            "always": bool(entry.get("always")),
            "priority": int(entry.get("priority") or 3),
            "updated_at": now,
        }
        for i, e in enumerate(entries):
            if e.get("id") == item["id"]:
                entries[i] = item
                break
        else:
            entries.append(item)
        self.save(slug, entries)
        return item

    def delete(self, slug: str, entry_id: str):
        entries = [e for e in self.load(slug) if e.get("id") != entry_id]
        self.save(slug, entries)

    # ------------------------------------------------------------------
    # 共用世界观
    # ------------------------------------------------------------------
    def load_world(self) -> list[dict]:
        if not os.path.exists(self.world_path):
            return []
        try:
            with open(self.world_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") if isinstance(data, dict) else data
            return [e for e in (entries or []) if isinstance(e, dict)]
        except Exception:
            return []

    def retrieve_world(self, query: str, limit: int = 2) -> list[dict]:
        """只在这个世界确实被提到时才注入。

        这里有意的保守：世界观词条一旦每轮都塞，会挤掉角色自己的语气。
        所以只认关键词精确命中，不做向量兜底。
        """
        entries = self.load_world()
        if not entries:
            return []
        q = (query or "").lower()
        if not q:
            return []
        hits = []
        for e in entries:
            keys = [e.get("title", "")] + list(e.get("keywords") or [])
            matched = [k for k in keys if k and str(k).lower() in q]
            if matched:
                score = sum(min(len(str(k)), 8) for k in matched) / 4.0
                hits.append((score, e))
        hits.sort(key=lambda x: -x[0])
        out, used = [], 0
        for _s, e in hits[:limit]:
            text = (e.get("content") or "").strip()
            if len(text) > 260:
                text = text[:260] + "…"
            cost = len(text) + 10
            if out and used + cost > 480:
                break
            out.append(dict(e, _text=text))
            used += cost
        return out

    # ------------------------------------------------------------------
    def retrieve(self, slug: str, query: str,
                 budget: int = DEFAULT_BUDGET) -> list[dict]:
        """返回这一轮该注入的条目，按优先级排序，总额受限。"""
        entries = self.load(slug)
        if not entries:
            return []

        q = (query or "").lower()
        qv = embed(query or "") if query else None
        hits: list[tuple[float, dict]] = []
        vector_used = 0

        for e in entries:
            score = 0.0
            if e.get("always"):
                score = 100.0
            else:
                kws = [k for k in (e.get("keywords") or []) if k]
                matched = [k for k in kws if k.lower() in q]
                if matched:
                    # 命中的关键词越长越具体，权重越高
                    score = 10.0 + sum(min(len(k), 8) for k in matched) / 4.0
                elif qv is not None and vector_used < MAX_VECTOR_HITS:
                    sim = cosine(qv, embed(f"{e.get('title', '')} {e.get('content', '')}"))
                    if sim >= VECTOR_MIN_SCORE:
                        score = sim * 10.0
                        vector_used += 1
            if score > 0:
                hits.append(
                    (score + float(e.get("priority") or 3) * 0.1
                     + KIND_BONUS.get(e.get("kind"), 0.0), e)
                )

        hits.sort(key=lambda x: -x[0])

        out, used = [], 0
        for _score, e in hits:
            text = (e.get("content") or "").strip()
            if len(text) > MAX_ENTRY_CHARS:
                text = text[:MAX_ENTRY_CHARS] + "…"
            cost = len(text) + len(e.get("title") or "") + 8
            if out and used + cost > budget:
                continue
            out.append(dict(e, _text=text))
            used += cost
            if used >= budget:
                break
        return out


    def retrieve_timeline(self, slug: str, budget: int = TIMELINE_BUDGET,
                          limit: int = TIMELINE_MAX) -> list[dict]:
        """按时间顺序取她的经历，用于「讲讲你的故事」这类泛问。

        和 retrieve() 的区别：这里不看关键词，而是把她的过去当成一条线取出来。
        一个人被问「讲讲你的故事」时，不会等你提到某个具体地名才开口。
        """
        entries = [e for e in self.load(slug) if e.get("kind") == "history"]
        if not entries:
            return []
        # 有章节号的按章节排，没章节号的（"她的转变"这类总结）垫后
        entries.sort(key=lambda e: (_chapter_order(e.get("title", "")),
                                    -float(e.get("priority") or 3)))
        out, used = [], 0
        for e in entries:
            text = (e.get("content") or "").strip()
            if not text:
                continue
            if len(text) > MAX_ENTRY_CHARS:
                text = text[:MAX_ENTRY_CHARS] + "…"
            cost = len(text) + len(e.get("title") or "") + 8
            if out and used + cost > budget:
                continue
            out.append(dict(e, _text=text))
            used += cost
            if len(out) >= limit or used >= budget:
                break
        return out


def format_for_prompt(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = []
    for e in entries:
        kind = KINDS.get(e.get("kind"), "设定")
        lines.append(f"【{kind}·{e.get('title')}】{e.get('_text') or e.get('content')}")
    return "\n".join(lines)


def format_world(entries: list[dict]) -> str:
    """共用世界观的排版。这些是「所有人都知道」的常识，语气要和「往事」区分开。"""
    if not entries:
        return ""
    return "\n".join(
        f"【{e.get('title')}】{e.get('_text') or e.get('content')}"
        for e in entries
    )


def format_timeline(entries: list[dict]) -> str:
    """把经历排成「一条人生线」，而不是一堆散条目。

    模型看到「1. … 2. … 3. …」会自然理解成先后发生的，
    看到一堆【往事·xxx】则容易当成互不相关的资料卡。
    """
    if not entries:
        return ""
    lines = []
    for i, e in enumerate(entries, 1):
        title = (e.get("title") or "").strip()
        body = (e.get("_text") or e.get("content") or "").strip()
        lines.append(f"{i}. {title}\n   {body}")
    return "\n".join(lines)
