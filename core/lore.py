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
MAX_ENTRY_CHARS = 400      # 单条截断
VECTOR_MIN_SCORE = 0.11    # 向量兜底的最低相似度（实测：相关 0.13~0.32，无关 <0.06）
MAX_VECTOR_HITS = 3        # 向量命中最多条数，防止噪声稀释上下文


def _eid(title: str, n: int) -> str:
    h = hashlib.sha256(f"{title}:{n}".encode("utf-8")).hexdigest()[:8]
    return f"lore-{h}"


class LoreStore:
    def __init__(self, cards_root: str):
        self.root = cards_root

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
                hits.append((score + float(e.get("priority") or 3) * 0.1, e))

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


def format_for_prompt(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = []
    for e in entries:
        kind = KINDS.get(e.get("kind"), "设定")
        lines.append(f"【{kind}·{e.get('title')}】{e.get('_text') or e.get('content')}")
    return "\n".join(lines)
