"""角色卡：导入 / 校验 / 存储。

关键设计（这是"不用预设角色"的核心）：

    characters/<slug>/
        card.json     ← 声明：名字、人格、兴趣、主题色（Tavern V2 兼容）
        persona.md    ← 人格正文（Markdown，人和模型都读得懂）
        avatar.png    ← 256×256 本地图片
        state.json    ← 运行时（情绪/好感/作息），与卡片解耦，删卡不丢状态

支持三种输入：
  1. Tavern Card V2  JSON  {spec:"chara_card_v2", spec_version:"2.0", data:{...}}
  2. Tavern Card V1  JSON  {name, description, personality, ...}
  3. PNG 内嵌角色卡   （tEXt chunk，keyword="chara"，base64 JSON）

内部字段一律先归一化成 InternalCard，再落盘——外部格式变化不影响内部。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field, asdict

PERSONA_MIN_CHARS = 200
AVATAR_MAX_BYTES = 8 * 1024 * 1024
AVATAR_SIZE = 256


class CardError(ValueError):
    pass


@dataclass
class InternalCard:
    id: str
    name: str
    slug: str
    persona_md: str
    summary: str = ""
    gender: str = ""             # male / female / other，决定第三人称用「他」还是「她」
    signature: str = ""          # 个性签名：她想让别人看到的一句话
    address: str = ""            # 她对聊天人的固定称呼（如「司辰」「阁下」）
    series: str = ""
    aliases: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    accent: str = "#9ecbff"
    first_mes: str = ""
    mes_example: str = ""
    # 运行时偏好（可覆盖默认作息）
    schedule: dict = field(default_factory=dict)
    traits: dict = field(default_factory=dict)
    lore: list[dict] = field(default_factory=list)
    source_spec: str = ""
    imported_at: float = 0.0


# ---------------------------------------------------------------- PNG 嵌入卡
def _png_chara_text(raw: bytes) -> str | None:
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if ctype in (b"tEXt", b"iTXt"):
            try:
                if ctype == b"tEXt":
                    key, _, val = data.partition(b"\x00")
                    if key.decode("latin-1").lower() == "chara":
                        return base64.b64decode(val).decode("utf-8")
                else:
                    # iTXt: keyword \0 compressionFlag \0 lang \0 translated \0 text
                    parts = data.split(b"\x00", 5)
                    if parts[0].decode("latin-1").lower() == "chara" and len(parts) == 6:
                        payload = parts[5]
                        if parts[1] == b"\x01":
                            import zlib
                            payload = zlib.decompress(payload)
                        return payload.decode("utf-8")
            except Exception:
                pass
        if ctype == b"IEND":
            break
        pos += 12 + length
    return None


# ---------------------------------------------------------------- 解析
def _strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.S)
    return m.group(1) if m else s


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,，、\n]", str(v)) if x.strip()]


def _compose_persona_from_v2(d: dict) -> str:
    """V2 没有统一的 persona.md，按语义拼出来。"""
    parts = []
    if d.get("description"):
        parts.append(f"## 她是谁\n{d['description'].strip()}")
    if d.get("personality"):
        parts.append(f"## 性格\n{d['personality'].strip()}")
    if d.get("scenario"):
        parts.append(f"## 处境\n{d['scenario'].strip()}")
    ex = d.get("mes_example")
    if ex:
        parts.append(f"## 说话样本\n{ex.strip()}")
    return "\n\n".join(parts)


def parse_card_dict(obj: dict, raw_name_hint: str = "",
                    require_persona: bool = True) -> InternalCard:
    if not isinstance(obj, dict):
        raise CardError("角色卡必须是 JSON 对象")

    spec = (obj.get("spec") or "").lower()
    data = obj.get("data") if isinstance(obj.get("data"), dict) else None

    if spec and "v2" in spec or (obj.get("spec_version") in ("2.0", 2.0) and data):
        d = data or {}
        name = d.get("name") or raw_name_hint
        persona = _compose_persona_from_v2(d)
        if not persona and d.get("persona_md"):
            persona = d["persona_md"]
        first = d.get("first_mes", "")
        mes_ex = d.get("mes_example", "")
        series = d.get("series", "") or (d.get("extensions", {}) or {}).get("series", "")
        interests = _as_list(d.get("interests") or (d.get("extensions", {}) or {}).get("interests"))
        tags = _as_list(d.get("tags"))
        accent = (d.get("extensions", {}) or {}).get("accent", "#9ecbff")
        source_spec = "tavern_v2"
    elif obj.get("persona") or obj.get("persona_md") or obj.get("spec_version") in ("1.0", 1.0):
        # 本项目的原生格式
        d = data or obj
        name = d.get("name") or raw_name_hint
        persona = d.get("persona_md") or d.get("persona") or ""
        first = d.get("first_mes", "")
        mes_ex = d.get("mes_example", "")
        series = d.get("series", "")
        interests = _as_list(d.get("interests"))
        tags = _as_list(d.get("tags"))
        accent = d.get("accent", "#9ecbff")
        source_spec = "starlit_clone_v1"
    else:
        # 当作 V1 裸卡
        d = obj
        name = d.get("name") or raw_name_hint
        persona = _compose_persona_from_v2(d)
        first = d.get("first_mes", "")
        mes_ex = d.get("mes_example", "")
        series = d.get("series", "")
        interests = _as_list(d.get("interests"))
        tags = _as_list(d.get("keywords") or d.get("tags"))
        accent = d.get("accent", "#9ecbff")
        source_spec = "tavern_v1"

    name = (name or "").strip()
    if not name:
        raise CardError("角色卡缺少 name")

    persona = (persona or "").strip()
    if require_persona and len(persona) < PERSONA_MIN_CHARS:
        raise CardError(
            f"人设太短（当前 {len(persona)} 字，至少 {PERSONA_MIN_CHARS} 字），"
            "请补充说话方式、性格和边界"
        )

    slug = _make_slug(name)

    # Tavern 的 character_book（lorebook）→ 设定集条目
    lore_entries: list[dict] = []
    block = data if isinstance(data, dict) else obj
    book = block.get("character_book") if isinstance(block, dict) else None
    for e in ((book or {}).get("entries") or []):
        if not isinstance(e, dict):
            continue
        content = (e.get("content") or "").strip()
        if not content:
            continue
        keys = e.get("keys") or e.get("key") or []
        if isinstance(keys, str):
            keys = [keys]
        title = (e.get("comment") or e.get("name") or content[:12]).strip()
        lore_entries.append({
            "title": title[:20],
            "kind": "world",
            "keywords": [str(k).strip() for k in keys if str(k).strip()][:6],
            "content": content,
            "always": bool(e.get("constant") or e.get("always")),
        })

    return InternalCard(
        id=_stable_id(name),
        name=name,
        slug=slug,
        persona_md=persona,
        summary=(d.get("summary") or "").strip(),
        gender=(d.get("gender") or "").strip(),
        signature=(d.get("signature") or "").strip(),
        address=(d.get("address") or "").strip(),
        series=(series or "").strip(),
        aliases=_as_list(d.get("aliases")),
        interests=interests,
        tags=tags,
        accent=accent if re.match(r"^#[0-9a-fA-F]{6}$", str(accent)) else "#9ecbff",
        first_mes=first,
        mes_example=mes_ex,
        schedule=d.get("schedule") or {},
        traits=d.get("traits") or {},
        lore=lore_entries,
        source_spec=source_spec,
        imported_at=time.time(),
    )


def _stable_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


_SAFE = re.compile(r"[^\w\u4e00-\u9fff-]+")


def _make_slug(name: str) -> str:
    s = _SAFE.sub("-", name.strip()).strip("-").lower()
    return s or "character"


# ---------------------------------------------------------------- 头像
def normalize_avatar(raw: bytes) -> bytes:
    """本地图片 → 256×256 PNG。明确拒绝外部 URL。"""
    if not raw:
        raise CardError("头像为空")
    if len(raw) > AVATAR_MAX_BYTES:
        raise CardError("头像太大（上限 8MB）")
    try:
        from PIL import Image
    except ImportError:
        raise CardError("缺少 Pillow，无法处理头像（pip install pillow）")
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise CardError("头像数据损坏，请重新选择图片")
    im = im.convert("RGBA")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    im = im.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- 存储
class CardStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def dir_of(self, slug: str) -> str:
        return os.path.join(self.root, slug)

    def exists(self, slug: str) -> bool:
        return os.path.exists(os.path.join(self.dir_of(slug), "card.json"))

    def save(self, card: InternalCard, avatar_bytes: bytes | None = None) -> str:
        d = self.dir_of(card.slug)
        os.makedirs(d, exist_ok=True)
        payload = {
            "spec": "starlit_clone_card",
            "spec_version": "1.0",
            "data": {
                "id": card.id, "name": card.name, "slug": card.slug,
                "summary": card.summary, "gender": card.gender,
                "signature": card.signature,
                "address": card.address,
                "series": card.series,
                "aliases": card.aliases, "interests": card.interests,
                "tags": card.tags, "accent": card.accent,
                "first_mes": card.first_mes, "mes_example": card.mes_example,
                "persona_md": card.persona_md,
                "schedule": card.schedule, "traits": card.traits,
                "source_spec": card.source_spec, "imported_at": card.imported_at,
            },
        }
        with open(os.path.join(d, "card.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(d, "persona.md"), "w", encoding="utf-8") as f:
            f.write(card.persona_md)
        if avatar_bytes:
            with open(os.path.join(d, "avatar.png"), "wb") as f:
                f.write(normalize_avatar(avatar_bytes))
        if not os.path.exists(os.path.join(d, "state.json")):
            with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
                json.dump({"created_at": time.time(), "notes": "运行时状态，删除本文件即重置"},
                          f, ensure_ascii=False, indent=2)
        return d

    def load(self, slug: str) -> InternalCard:
        d = self.dir_of(slug)
        cp = os.path.join(d, "card.json")
        if not os.path.exists(cp):
            raise CardError(f"角色不存在：{slug}")
        with open(cp, encoding="utf-8") as f:
            obj = json.load(f)
        # persona.md 是权威副本（人可以手改），所以先不校验长度
        card = parse_card_dict(obj, raw_name_hint=slug, require_persona=False)
        pp = os.path.join(d, "persona.md")
        if os.path.exists(pp):
            with open(pp, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                card.persona_md = txt
        if len(card.persona_md.strip()) < 80:
            raise CardError(f"「{card.name}」的人设太短（{len(card.persona_md)} 字）")
        return card

    def list(self) -> list[InternalCard]:
        out = []
        if not os.path.isdir(self.root):
            return out
        for name in sorted(os.listdir(self.root)):
            if os.path.exists(os.path.join(self.root, name, "card.json")):
                try:
                    out.append(self.load(name))
                except CardError:
                    continue
        return out

    def delete(self, slug: str):
        import shutil
        d = self.dir_of(slug)
        if os.path.isdir(d):
            shutil.rmtree(d)

    # ---- 直接创建（AI 蒸馏出来的卡）----
    def create(self, name: str, persona_md: str, series: str = "", summary: str = "",
               interests: list[str] | None = None, tags: list[str] | None = None,
               accent: str = "#9ecbff", avatar_bytes: bytes | None = None,
               traits: dict | None = None, schedule: dict | None = None,
               min_chars: int | None = None, gender: str = "") -> InternalCard:
        name = (name or "").strip()
        if not name:
            raise CardError("角色名不能为空")
        persona_md = (persona_md or "").strip()
        floor = PERSONA_MIN_CHARS if min_chars is None else int(min_chars)
        if len(persona_md) < floor:
            raise CardError(
                f"人设太短（{len(persona_md)} 字，至少 {floor} 字）")
        base = _make_slug(name)
        slug = base
        n = 2
        while self.exists(slug):
            slug = f"{base}-{n}"
            n += 1
        card = InternalCard(
            id=_stable_id(name), name=name, slug=slug, persona_md=persona_md,
            summary=summary, series=series,
            interests=[str(x) for x in (interests or []) if str(x).strip()],
            tags=[str(x) for x in (tags or []) if str(x).strip()],
            accent=accent if re.match(r"^#[0-9a-fA-F]{6}$", str(accent)) else "#9ecbff",
            traits=traits or {}, schedule=schedule or {},
            gender=(gender if gender in ("male", "female", "other") else ""),
            source_spec="distilled", imported_at=time.time(),
        )
        self.save(card, avatar_bytes)
        return card

    # ---- 导入入口 ----
    def import_bytes(self, raw: bytes, filename: str = "") -> InternalCard:
        """支持 .json / .png（PNG 内嵌卡）。"""
        obj = None
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            txt = _png_chara_text(raw)
            if not txt:
                raise CardError("这张 PNG 里没有内嵌角色卡数据")
            obj = json.loads(_strip_fences(txt))
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            obj = json.loads(_strip_fences(text))

        card = parse_card_dict(obj, raw_name_hint=os.path.splitext(os.path.basename(filename))[0])
        avatar = raw if raw[:8] == b"\x89PNG\r\n\x1a\n" else None

        # 同名冲突：加后缀，不覆盖
        base = card.slug
        n = 2
        while self.exists(card.slug):
            card.slug = f"{base}-{n}"
            n += 1
        self.save(card, avatar)
        return card
