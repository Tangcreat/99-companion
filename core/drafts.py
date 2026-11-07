"""添加角色草稿：可持久化编辑，试聊上下文只保留在当前进程内。"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid


class DraftError(ValueError):
    """草稿不存在、版本冲突或字段不合法。"""


class DraftStore:
    """以一个小 JSON 文件保存草稿，并用 revision 阻止覆盖并发修改。"""

    FIELDS = (
        "id", "revision", "name", "series", "aliases", "summary",
        "interests", "persona_md", "avatar", "trial", "created_at", "updated_at",
    )
    EDITABLE = {"name", "series", "gender", "aliases", "summary",
                "interests", "persona_md", "avatar"}

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.drafts: dict[str, dict] = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load()

    @staticmethod
    def _text(value, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _list(value, limit: int = 8) -> list[str]:
        if isinstance(value, str):
            value = re.split(r"[,，、\n]", value)
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            text = str(item).strip()[:40]
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _normalize(self, value: dict, existing: dict | None = None) -> dict:
        """统一草稿字段；已有草稿的时间与试聊不会被输入覆盖。"""
        value = value if isinstance(value, dict) else {}
        old = existing or {}
        now = time.time()
        return {
            "id": self._text(old.get("id") or value.get("id"), 64),
            "revision": max(1, int(old.get("revision") or value.get("revision") or 1)),
            "name": self._text(value.get("name"), 24),
            "series": self._text(value.get("series"), 80),
            "gender": (lambda g: g if g in ("male", "female", "other") else "other")(
                self._text(value.get("gender"), 8).lower()),
            "aliases": self._list(value.get("aliases")),
            "summary": self._text(value.get("summary"), 240),
            "interests": self._list(value.get("interests")),
            "persona_md": self._text(value.get("persona_md"), 12000),
            "avatar": self._text(value.get("avatar"), 12 * 1024 * 1024),
            "trial": copy.deepcopy(old.get("trial") or []),
            "created_at": float(old.get("created_at") or value.get("created_at") or now),
            "updated_at": float(old.get("updated_at") or value.get("updated_at") or now),
        }

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("drafts", []) if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                # 试聊明确不跨重启恢复，即使旧文件里曾误存也不读出来。
                item = dict(item, trial=[])
                draft = self._normalize(item)
                if draft["id"]:
                    self.drafts[draft["id"]] = draft
        except (OSError, ValueError, TypeError) as e:
            raise DraftError(f"草稿文件无法读取：{e}") from e

    def _save(self) -> None:
        """原子写入；磁盘只保存草稿，不保存临时试聊。"""
        items = []
        for draft in self.drafts.values():
            saved = copy.deepcopy(draft)
            saved["trial"] = []
            items.append(saved)
        items.sort(key=lambda d: d["created_at"])
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"drafts": items}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _require_revision(draft: dict, revision) -> None:
        """变更前强制校验 JSON 整数版本，拒绝缺省、字符串、浮点和布尔值。"""
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise DraftError("缺少有效的 revision（需要整数）")
        if revision != draft["revision"]:
            raise DraftError(
                f"草稿已被修改（当前 revision {draft['revision']}），请重新载入后再保存")

    @staticmethod
    def _check_revision(draft: dict, revision) -> None:
        """供可选校验读取使用，类型规则与强制变更校验完全一致。"""
        DraftStore._require_revision(draft, revision)

    def create(self, value: dict) -> dict:
        """创建 revision 1 的新草稿，不触碰正式角色目录。"""
        with self.lock:
            draft = self._normalize(value)
            if not draft["name"]:
                raise DraftError("角色名不能为空")
            draft["id"] = uuid.uuid4().hex
            draft["created_at"] = draft["updated_at"] = time.time()
            self.drafts[draft["id"]] = draft
            self._save()
            return copy.deepcopy(draft)

    def get(self, draft_id: str, revision=None) -> dict:
        """读取单个草稿；提供 revision 时同时做乐观锁校验。"""
        with self.lock:
            draft = self.drafts.get(str(draft_id or ""))
            if not draft:
                raise DraftError("草稿不存在或已经正式添加")
            if revision is not None:
                self._check_revision(draft, revision)
            return copy.deepcopy(draft)

    def list(self) -> list[dict]:
        """按最近修改顺序列出全部未完成草稿。"""
        with self.lock:
            items = [copy.deepcopy(d) for d in self.drafts.values()]
            return sorted(items, key=lambda d: d["updated_at"], reverse=True)

    def patch(self, draft_id: str, revision, changes: dict) -> dict:
        """保存可编辑字段并递增 revision；人设变化后清空旧试聊。"""
        if not isinstance(changes, dict):
            raise DraftError("修改内容必须是对象")
        with self.lock:
            old = self.get(draft_id)
            self._require_revision(old, revision)
            merged = dict(old)
            for key in self.EDITABLE:
                if key in changes:
                    merged[key] = changes[key]
            draft = self._normalize(merged, old)
            if not draft["name"]:
                raise DraftError("角色名不能为空")
            draft["revision"] = old["revision"] + 1
            draft["updated_at"] = time.time()
            draft["trial"] = []
            self.drafts[draft["id"]] = draft
            self._save()
            return copy.deepcopy(draft)

    def append_trial(self, draft_id: str, revision, role: str, text: str) -> list[dict]:
        """追加一条临时消息，最多保留最近 24 条，不写数据库或磁盘。"""
        role = str(role or "")
        if role not in ("user", "char"):
            raise DraftError("试聊角色必须是 user 或 char")
        text = str(text or "").strip()[:2000]
        if not text:
            raise DraftError("试聊消息不能为空")
        with self.lock:
            draft = self.get(draft_id)
            self._require_revision(draft, revision)
            stored = self.drafts[draft["id"]]
            stored["trial"].append({"role": role, "text": text})
            stored["trial"] = stored["trial"][-24:]
            stored["updated_at"] = time.time()
            return copy.deepcopy(stored["trial"])

    def delete(self, draft_id: str) -> None:
        """删除未完成草稿。"""
        with self.lock:
            if str(draft_id or "") not in self.drafts:
                raise DraftError("草稿不存在或已经正式添加")
            del self.drafts[str(draft_id)]
            self._save()

    def commit(self, draft_id: str, revision) -> dict:
        """在正式建卡成功后删除草稿，并返回最终提交内容。"""
        with self.lock:
            draft = self.get(draft_id)
            self._require_revision(draft, revision)
            self.delete(draft_id)
            return draft
