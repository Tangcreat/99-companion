"""三层记忆：画像 / 情节 / 情绪。

向量方案沿用原版思路——不调外部 embedding，用本地字符 n-gram 哈希向量：
  * 256 维，L2 归一化，cosine 相似度
  * 中文按字、英文数字按词，另补一层中文 bigram
  * 零依赖、零成本、完全离线
  * 接口固定为 embed(text) -> list[float]，将来换真 embedding 只改这一个函数

版本链是重点：修正记忆时不删旧记录，而是把旧记录置为 superseded，
新记录 version+1 并指向同一 root_id。这样"她记错过什么"是可追溯的。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Iterable

DIM = 256

_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CJK}]|[a-zA-Z0-9]+")
_CJK_RE = re.compile(rf"[{_CJK}]")
_NEGATION_RE = re.compile(r"不|没|非|拒绝|无法|不能|未答应|不承认")

MEMORY_TEXT_MAX = 500


def _tokens(text: str) -> list[str]:
    base = _TOKEN_RE.findall(text.lower())
    out = list(base)
    # 中文 bigram：补一层，缓解单字语义太散
    for i in range(len(base) - 1):
        a, b = base[i], base[i + 1]
        if _CJK_RE.fullmatch(a) and _CJK_RE.fullmatch(b):
            out.append(a + b)
    return out


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for tok in _tokens(text):
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % DIM
        sign = 1.0 if h[4] & 1 else -1.0
        v[idx] += sign
    norm = math.sqrt(sum(x * x for x in v))
    if norm > 0:
        v = [x / norm for x in v]
    return v


def vec_to_str(v: Iterable[float]) -> str:
    return " ".join(f"{x:.6g}" for x in v)


def vec_from_str(s: str) -> list[float]:
    return [float(x) for x in s.split()] if s else []


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def text_hash(text: str) -> str:
    norm = re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


class Memory:
    def __init__(self, store):
        self.s = store

    # ================= 画像层 =================
    def set_profile(self, key: str, value: str):
        """人工编辑 = 明确选择，直接进画像。"""
        self.s.x(
            "INSERT INTO profile(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    def profile_text(self, limit: int = 40) -> str:
        rows = self.s.q("SELECT key,value FROM profile ORDER BY updated_at DESC LIMIT ?", (limit,))
        return "\n".join(f"- {r['key']}：{r['value']}" for r in rows)

    # ================= 事实三态 =================
    def propose_fact(self, char_id: str, key: str, value: str,
                     turn_id: int | None = None, origin: str = "analysis",
                     spoken_text: str = "") -> str:
        """模型自动抽取的事实只能进 candidate。

        只有"实际发出的正文能支持"且"无明显否定语境"时，才允许自动确认。
        """
        if not key or not value:
            return "skipped"
        if _NEGATION_RE.search(spoken_text or ""):
            self.s.x(
                "INSERT OR IGNORE INTO self_facts"
                "(char_id,key,value,value_hash,status,origin,source_turn,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (char_id, key, value, text_hash(value), "conflict", origin, turn_id,
                 time.time(), time.time()),
            )
            return "conflict"

        cur = self.s.x(
            "INSERT OR IGNORE INTO self_facts"
            "(char_id,key,value,value_hash,status,origin,source_turn,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (char_id, key, value, text_hash(value), "candidate", origin, turn_id,
             time.time(), time.time()),
        )
        return "candidate" if cur.rowcount else "exists"

    def confirm_fact(self, fact_id: int):
        now = time.time()
        self.s.x(
            "UPDATE self_facts SET status='confirmed', confirmed_at=?, updated_at=? WHERE id=?",
            (now, now, fact_id),
        )

    def confirmed_facts_text(self, char_id: str, limit: int = 30) -> str:
        """只把 confirmed 注入长期身份；候选和冲突一律排除。"""
        rows = self.s.q(
            "SELECT key,value FROM self_facts WHERE char_id=? AND status='confirmed' "
            "ORDER BY updated_at DESC LIMIT ?", (char_id, limit))
        return "\n".join(f"- {r['key']}：{r['value']}" for r in rows)

    # ================= 情节层 =================
    def add(self, char_id: str, text: str, kind: str = "episodic",
            emotion: str | None = None, weight: float = 1.0,
            source_turn: int | None = None) -> int | None:
        text = (text or "").strip()[:MEMORY_TEXT_MAX]
        if not text:
            return None
        h = text_hash(text)
        # 去重：同角色同内容且仍 active 的不重复写
        dup = self.s.q1(
            "SELECT id FROM memories WHERE char_id=? AND text_hash=? AND status='active'",
            (char_id, h))
        if dup:
            self.s.x("UPDATE memories SET weight=weight+0.2 WHERE id=?", (dup["id"],))
            return dup["id"]

        now = time.time()
        cur = self.s.x(
            "INSERT INTO memories(char_id,ts,kind,text,vec,emotion,weight,text_hash,"
            "source_turn,version,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,'active',?)",
            (char_id, now, kind, text, vec_to_str(embed(text)), emotion, weight, h,
             source_turn, now),
        )
        mid = cur.lastrowid
        self.s.x("UPDATE memories SET root_id=? WHERE id=?", (mid, mid))
        return mid

    def revise(self, memory_id: int, new_text: str, reason: str = ""):
        """修正记忆：旧版标 superseded，新版 version+1 挂在同一 root 下。"""
        old = self.s.q1("SELECT * FROM memories WHERE id=?", (memory_id,))
        if not old:
            return None
        now = time.time()
        self.s.x(
            "UPDATE memories SET status='superseded', superseded_at=?, updated_at=? WHERE id=?",
            (now, now, memory_id),
        )
        root = old["root_id"] or old["id"]
        cur = self.s.x(
            "INSERT INTO memories(char_id,ts,kind,text,vec,emotion,weight,text_hash,"
            "source_turn,root_id,previous_id,version,status,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (old["char_id"], now, old["kind"], new_text[:MEMORY_TEXT_MAX],
             vec_to_str(embed(new_text)), old["emotion"], old["weight"],
             text_hash(new_text), old["source_turn"], root, memory_id,
             old["version"] + 1, now),
        )
        new_id = cur.lastrowid
        self.s.x(
            "INSERT OR REPLACE INTO memory_meta(memory_id,pinned,category,corrected_at) "
            "VALUES(?,?,?,?)",
            (new_id, 0, "corrected", now),
        )
        return new_id

    def recall(self, char_id: str, query: str, top_k: int = 6) -> list[dict]:
        """向量召回 + 时间衰减 + 权重。"""
        qv = embed(query)
        rows = self.s.q(
            "SELECT id,ts,kind,text,vec,emotion,weight,version,root_id FROM memories "
            "WHERE char_id=? AND status='active'", (char_id,))
        now = time.time()
        scored = []
        for r in rows:
            sim = cosine(qv, vec_from_str(r["vec"]))
            age_days = max((now - r["ts"]) / 86400.0, 0.0)
            # 半衰期 30 天，但最近 3 天内几乎不衰减
            decay = 1.0 if age_days < 3 else math.exp(-age_days / 30.0)
            score = sim * (0.6 + 0.4 * min(r["weight"], 3.0) / 3.0) * (0.5 + 0.5 * decay)
            if sim > 0.02:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [dict(r, score=round(s, 4)) for s, r in scored[:top_k]]

    def all_active(self, char_id: str, limit: int = 200):
        return self.s.q(
            "SELECT id,ts,kind,text,emotion,version,root_id FROM memories "
            "WHERE char_id=? AND status='active' ORDER BY ts DESC LIMIT ?",
            (char_id, limit))

    def history_chain(self, root_id: int):
        return self.s.q(
            "SELECT id,version,status,text,superseded_at FROM memories "
            "WHERE root_id=? ORDER BY version", (root_id,))
