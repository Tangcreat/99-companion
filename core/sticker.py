"""表情包：索引、按情绪挑图、发送。

机制照搬原版（它的 index.json 结构我拆出来了）：

    stickers/
    ├── index.json          {file, desc, emotions[], tags[], source}
    ├── blue archive/       一个目录 = 一个表情包集
    │   ├── 不爽_0.0.png    ← 文件名前缀就是情绪标签
    │   └── 低落_B9703B06.gif
    └── mygo/

原版用视觉模型逐张识图生成标签；这里先用**文件名约定**（成本为零），
文件名没有情绪前缀时才退化为按目录归类。想更准可以后续接视觉模型。

挑图逻辑：把当轮情绪归一到标准类 → 找该类下的图 → 按概率决定发不发。
情绪对不上就不发——硬塞一张不搭的图比不发尴尬得多。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
from urllib.parse import quote

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 把模型吐出的情绪词归一到表情包文件名的前缀词
EMOTION_FAMILY = {
    "开心": ["开心", "高兴", "愉快", "快乐", "兴奋", "笑", "得意", "满足", "惊喜"],
    "难过": ["难过", "低落", "伤心", "沮丧", "失落", "委屈", "哭", "悲伤"],
    "生气": ["生气", "愤怒", "不爽", "火大", "恼", "不满", "炸"],
    "困倦": ["困", "累", "疲惫", "倦", "睡", "乏力", "没精神"],
    "害羞": ["害羞", "脸红", "羞", "不好意思", "脸红"],
    "无语": ["无语", "无奈", "尴尬", "汗", "沉默", "不知道该说什么"],
    "亲密": ["亲密", "喜欢", "爱", "抱", "贴", "撒娇", "温柔"],
    "疑惑": ["疑惑", "困惑", "疑问", "不解", "懵", "思考"],
    "惊讶": ["惊讶", "震惊", "吃惊", "意外", "吓"],
    "安慰": ["安慰", "倾听", "陪", "理解", "共情"],
    "平静": ["平静", "安静", "淡然", "冷静", "放松"],
}

_FILENAME_EMO_RE = re.compile(r"^([^\W\d_]{1,8})[_\-\s]")


def _norm_family(label: str) -> str | None:
    """情绪词 → 标准类名。命中不了返回 None。"""
    if not label:
        return None
    for fam, words in EMOTION_FAMILY.items():
        if label == fam:
            return fam
        for w in words:
            if w in label or label in w:
                return fam
    return None


def classify(label: str) -> str | None:
    return _norm_family(label)


class StickerStore:
    def __init__(self, root: str):
        self.root = root
        self.index_path = os.path.join(root, "index.json")

    # ---------------------------------------------------------------- 扫描
    def scan(self, force: bool = False) -> int:
        """遍历目录生成索引。增量：已在索引里的文件跳过（除非 force）。"""
        os.makedirs(self.root, exist_ok=True)
        old = {}
        if not force and os.path.exists(self.index_path):
            for e in self._read_index():
                old[e.get("file")] = e

        entries, seen = [], set()
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in IMAGE_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                seen.add(rel)
                if rel in old and not force:
                    entries.append(old[rel])
                    continue
                entries.append(self._make_entry(rel, fn))

        entries.sort(key=lambda e: e["file"])
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
        return len(entries)

    def _make_entry(self, rel: str, filename: str) -> dict:
        stem = os.path.splitext(filename)[0]
        parts = rel.split("/")
        source = parts[0] if len(parts) > 1 else "默认"

        m = _FILENAME_EMO_RE.match(stem)
        emotion = m.group(1) if m else ""
        if not emotion:
            # 没有情绪前缀：用上一级目录名兜底
            parent = parts[-2] if len(parts) >= 2 else ""
            if parent and _norm_family(parent):
                emotion = parent

        return {
            "file": rel,
            "desc": f"{emotion} {stem}".strip() if emotion else stem,
            "emotions": [emotion] if emotion else [],
            "raw_emotion": emotion,
            "tags": ["stickers"] + [p for p in parts[:-1]],
            "source": source,
            "size": os.path.getsize(os.path.join(self.root, rel)),
        }

    def _read_index(self) -> list:
        if not os.path.exists(self.index_path):
            return []
        try:
            with open(self.index_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def load(self) -> list:
        return self._read_index()

    def sets(self) -> list[dict]:
        """按表情包集分组，给前端做标签页。"""
        idx = self.load()
        groups: dict[str, int] = {}
        for e in idx:
            groups[e.get("source") or "默认"] = groups.get(e.get("source") or "默认", 0) + 1
        out = [{"name": k, "count": v} for k, v in sorted(groups.items(), key=lambda x: -x[1])]
        return out

    def url_of(self, rel: str) -> str:
        parts = (rel or "").replace("\\", "/").split("/")
        return "/stickers/" + "/".join(quote(p) for p in parts)

    # ---------------------------------------------------------------- 挑图
    def pick(self, emotion_label: str, text: str = "",
             rng: random.Random | None = None) -> dict | None:
        """按当轮情绪挑一张。对不上就返回 None。"""
        rng = rng or random
        idx = self.load()
        if not idx:
            return None

        fam = classify(emotion_label)
        pool = []
        if fam:
            words = EMOTION_FAMILY[fam]
            for e in idx:
                ems = e.get("emotions") or []
                if any(any(w in em or em in w for w in words) for em in ems):
                    pool.append(e)
        if not pool and text:
            # 情绪对不上时，用文字里出现的情绪词再试一次
            for w_fam, words in EMOTION_FAMILY.items():
                if any(w in text for w in words):
                    for e in idx:
                        ems = e.get("emotions") or []
                        if any(any(w in em or em in w for w in words) for em in ems):
                            pool.append(e)
                    if pool:
                        break
        if not pool:
            return None
        return rng.choice(pool)

    def resolve(self, rel: str) -> str | None:
        """校验路径合法（防目录穿越）并返回绝对路径。"""
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            return None
        full = os.path.normpath(os.path.join(self.root, rel))
        if not full.startswith(os.path.normpath(self.root)):
            return None
        if not os.path.isfile(full):
            return None
        return full
