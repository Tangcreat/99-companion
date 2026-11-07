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

import base64
import hashlib
import json
import os
import random
import re
import threading
from urllib.parse import quote

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


class StickerError(ValueError):
    """表情图片、分类或路径不合法。"""

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
        self.root = os.path.abspath(root)
        self.index_path = os.path.join(root, "index.json")
        self.lock = threading.RLock()

    def _write_index(self, entries: list[dict]) -> None:
        """原子更新索引，避免上传/删除与读取并发时看到半个 JSON。"""
        os.makedirs(self.root, exist_ok=True)
        entries = sorted(entries, key=lambda e: e.get("file", ""))
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.index_path)

    def _category_path(self, name: str, create: bool = False) -> tuple[str, str]:
        """分类只允许根目录下的一层普通目录，不接受路径分隔符。"""
        name = str(name or "").strip()
        if not name or name in (".", "..") or len(name) > 60:
            raise StickerError("分类名不能为空，且不能超过 60 字")
        if name.startswith(".") or re.search(r"[/\\\x00]", name):
            raise StickerError("分类名不能包含路径分隔符")
        if name.lower() in ("index.json", "thumbs", "thumbnails"):
            raise StickerError("这个分类名被系统保留")
        path = os.path.abspath(os.path.join(self.root, name))
        if os.path.commonpath([self.root, path]) != self.root or os.path.islink(path):
            raise StickerError("分类路径不合法")
        if create:
            os.makedirs(self.root, exist_ok=True)
            if os.path.exists(path) and not os.path.isdir(path):
                raise StickerError("分类名已被文件占用")
            os.makedirs(path, exist_ok=True)
        return name, path

    @staticmethod
    def _decode_upload(data_url: str) -> tuple[bytes, str]:
        match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
                         str(data_url or ""), re.S)
        if not match:
            raise StickerError("图片必须是有效的 dataURL")
        mime = match.group(1).lower()
        ext = MIME_EXT.get(mime)
        if not ext:
            raise StickerError("只支持 PNG、JPG、GIF、WebP、BMP 图片")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as e:
            raise StickerError("图片数据损坏，请重新选择") from e
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise StickerError("单张图片不能超过 8MB")
        signatures = {
            ".png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": raw.startswith(b"\xff\xd8\xff"),
            ".gif": raw.startswith((b"GIF87a", b"GIF89a")),
            ".webp": len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
            ".bmp": raw.startswith(b"BM"),
        }
        if not signatures.get(ext, False):
            raise StickerError("图片内容与格式不符")
        return raw, ext

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
            _dirs[:] = [d for d in _dirs if not d.startswith(".") and
                        d.lower() not in ("thumbs", "thumbnails")]
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

        with self.lock:
            self._write_index(entries)
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
            "name": stem,
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
        """按表情包集分组；空分类也显示，便于继续上传。"""
        groups: dict[str, int] = {}
        for e in self.load():
            source = e.get("source") or "默认"
            groups[source] = groups.get(source, 0) + 1
        if os.path.isdir(self.root):
            for name in os.listdir(self.root):
                if name.startswith(".") or name.lower() in ("thumbs", "thumbnails"):
                    continue
                if os.path.isdir(os.path.join(self.root, name)):
                    groups.setdefault(name, 0)
        return [{"key": k, "name": k, "count": v}
                for k, v in sorted(groups.items(), key=lambda x: (-x[1], x[0]))]

    # ---------------------------------------------------------------- 管理
    def create_category(self, name: str) -> dict:
        """创建空分类，重名时明确报错。"""
        with self.lock:
            name, path = self._category_path(name)
            if os.path.exists(path):
                raise StickerError("这个分类已经存在")
            os.makedirs(self.root, exist_ok=True)
            os.mkdir(path)
            return {"key": name, "name": name, "count": 0}

    def delete_category(self, name: str) -> None:
        """仅删除真正空的分类目录，防止误删整套图片。"""
        with self.lock:
            name, path = self._category_path(name)
            if not os.path.isdir(path):
                raise StickerError("分类不存在")
            if os.listdir(path):
                raise StickerError("分类里还有图片，请先删除图片")
            os.rmdir(path)
            remaining = [e for e in self.load() if (e.get("source") or "默认") != name]
            self._write_index(remaining)

    def upload(self, name: str, data_url: str, set_name: str) -> dict:
        """上传一张本地图片到分类目录，并立即写入索引。"""
        raw, ext = self._decode_upload(data_url)
        original = os.path.basename(str(name or "表情").replace("\\", "/"))
        stem = os.path.splitext(original)[0]
        stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem).strip(" ._")[:80] or "表情"
        with self.lock:
            set_name, directory = self._category_path(set_name or "默认", create=True)
            if not os.path.exists(self.index_path):
                self.scan()
            candidate = stem + ext
            if os.path.exists(os.path.join(directory, candidate)):
                digest = hashlib.sha256(raw).hexdigest()[:8]
                candidate = f"{stem}_{digest}{ext}"
                n = 2
                while os.path.exists(os.path.join(directory, candidate)):
                    candidate = f"{stem}_{digest}_{n}{ext}"
                    n += 1
            full = os.path.join(directory, candidate)
            with open(full, "xb") as f:
                f.write(raw)
            rel = f"{set_name}/{candidate}"
            entry = self._make_entry(rel, candidate)
            entries = [e for e in self.load() if e.get("file") != rel]
            entries.append(entry)
            try:
                self._write_index(entries)
            except Exception:
                os.remove(full)
                raise
            return entry

    def delete(self, file: str) -> None:
        """删除图片、索引项以及可能存在的派生缩略图。"""
        with self.lock:
            full = self.resolve(file)
            if not full:
                raise StickerError("表情包不存在")
            rel = str(file).replace("\\", "/").lstrip("/")
            os.remove(full)
            self._write_index([e for e in self.load() if e.get("file") != rel])
            for base in (os.path.join(self.root, ".thumbs"),
                         os.path.join(self.root, "thumbs"),
                         os.path.join(self.root, "thumbnails")):
                thumb = os.path.abspath(os.path.join(base, rel))
                if os.path.commonpath([self.root, thumb]) == self.root and os.path.isfile(thumb):
                    os.remove(thumb)

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
        full = os.path.abspath(os.path.join(self.root, rel))
        try:
            if os.path.commonpath([self.root, full]) != self.root:
                return None
        except ValueError:
            return None
        if not os.path.isfile(full):
            return None
        return full
