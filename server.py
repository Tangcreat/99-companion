"""HTTP 服务 + 主流程编排（标准库实现，零第三方依赖，需 requests/Pillow）。

启动：python server.py        → http://127.0.0.1:8787
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import random
import re
import socket
import sys
import time
import traceback
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

_env_base = os.environ.get("STARLIT_DATA_ROOT")
if _env_base and os.path.isdir(_env_base):
    # Android / 外部指定：数据目录由调用方给出
    BASE = _env_base
    RES = BASE
elif getattr(sys, "frozen", False):
    # PyInstaller：可写数据放 exe 旁边，只读资源在 _MEIPASS
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

HERE = BASE
sys.path.insert(0, HERE)

from core import cards as cards_mod
from core import clock
from core import drafts as drafts_mod
from core import groups as groups_mod
from core import lore as lore_mod
from core import persona_builder
from core import schedule as sched_mod
from core import sticker as sticker_mod
from core import traits as traits_mod
from core.db import Store
from core.llm import LLM, LLMError
from core.memory import Memory
from core import think as think_mod
from core.think import Thinker

WEB_DIR = os.path.join(RES, "web")
CHAR_DIR = os.path.join(HERE, "characters")
DATA_DIR = os.path.join(HERE, "data")
APP_VERSION = os.environ.get("STARLIT_APP_VERSION") or ""
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")   # 聊天里发的图片
STICKER_DIR = os.path.join(HERE, "stickers")
CONFIG_PATH = os.path.join(HERE, "config.json")

# 重点角色（按 17173 喜爱度投票 + 台词量交叉确认），列表里置顶
FEATURED = [
    "37", "维尔汀", "十四行诗", "露西", "贝丽尔",
    "纸信圈儿", "玛丽安娜", "红弩箭", "斯奈德", "未锈铠",
    "苏菲亚", "鹭鸶剪", "程和光", "梁月", "牙仙",
    "雾行者", "霍夫曼", "星锑", "伊索尔德", "卡卡尼亚",
]
FEATURED_RANK = {n: i for i, n in enumerate(FEATURED)}


def char_sort_key(payload: dict):
    """重点角色按人气序排前面，其余按名字。"""
    rank = FEATURED_RANK.get(payload["name"])
    if rank is not None:
        return (0, rank, payload["name"])
    return (1, 0, payload["name"])


def sticker_payload(entry: dict) -> dict:
    """统一表情列表与上传响应的字段，兼容旧索引的 desc/source。"""
    source = entry.get("source") or "默认"
    file = entry.get("file") or ""
    return {
        "file": file,
        "name": entry.get("name") or entry.get("desc") or os.path.splitext(os.path.basename(file))[0],
        "desc": entry.get("desc", ""),
        "emotions": entry.get("emotions") or [],
        "tags": entry.get("tags") or [],
        "url": APP.stickers.url_of(file),
        "set": source,
        "source": source,
        "size": entry.get("size") or 0,
    }


DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "analysis_model": "",
        "timeout": 90,
        "temperature": 0.9,
        "max_tokens": 800,
    },
    "behavior": {
        "split_long_reply": True,
        "split_threshold": 40,
        "max_bubbles": 4,
        "outbound_delay_ms": [600, 2200],
        "bubble_delay_ms": [400, 1400],
    },
    "memory": {"recall_top_k": 6, "auto_extract": True},
    "sticker": {
        "enabled": True,
        "dir": "",
        "base_probability": 0.35,
        "min_gap_min": 2,
    },
    "autonomy": {"enabled": True, "daily_max": 14, "quiet_hours": ["00:30", "08:30"]},
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if isinstance(v, dict):
                    cfg.setdefault(k, {})
                    for kk, vv in v.items():
                        cfg[k].setdefault(kk, vv)
                else:
                    cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


OPENERS = "（(「【[《“‘"
CLOSERS = "）)」】]》”’"


def _safe_sentences(text: str) -> list[str]:
    """按句末标点切句，但不切括号/引号内部（否则「（还没配置模型。…）」会被拆断）。"""
    parts, buf, depth = [], "", 0
    for ch in text:
        buf += ch
        if ch in OPENERS:
            depth += 1
        elif ch in CLOSERS:
            depth = max(0, depth - 1)
        elif ch in "。！？!?…\n" and depth == 0:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def guess_gender(text: str) -> str:
    """从人设文本粗判性别。

    只用在「调用方没给 gender」的兜底路径上（旧版添加角色界面）。
    判据故意保守：两侧数量接近时不猜，返回 other，避免把女性角色写成「他」。
    """
    if not text:
        return "other"
    he = len(re.findall(r"他(?!们)", text))
    she = text.count("她")
    if he >= 3 and he > she * 2:
        return "male"
    if she >= 3 and she > he * 2:
        return "female"
    return "other"


def split_bubbles(text: str, threshold: int, max_bubbles: int) -> list[str]:
    """一条长回复拆成几个气泡（真人不会一句话发三百字）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= threshold:
        return [text]

    sentences = _safe_sentences(text)
    if len(sentences) <= 1:
        return [text]

    # 合并过短的片段
    out, buf = [], ""
    for p in sentences:
        if len(buf) + len(p) <= threshold:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)

    if len(out) > max_bubbles:
        out = out[:max_bubbles - 1] + ["".join(out[max_bubbles - 1:])]
    return out


class App:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CHAR_DIR, exist_ok=True)
        self.cfg = load_config()
        if not os.path.exists(CONFIG_PATH):
            save_config(self.cfg)
        self.store = Store(os.path.join(DATA_DIR, "companion.db"))
        self.mem = Memory(self.store)
        self.cards = cards_mod.CardStore(CHAR_DIR)
        self.lore = lore_mod.LoreStore(CHAR_DIR)
        self.drafts = drafts_mod.DraftStore(os.path.join(DATA_DIR, "character_drafts.json"))
        self.groups = groups_mod.GroupStore(DATA_DIR)
        self.cooccur = {}
        _co = os.path.join(DATA_DIR, "cooccur.json")
        if os.path.exists(_co):
            try:
                with open(_co, encoding="utf-8") as f:
                    self.cooccur = json.load(f)
            except Exception:
                pass
        self.llm = LLM(self.cfg["llm"])
        self.thinker = Thinker(self.llm)
        self.rng = random.Random()
        st_cfg = self.cfg.get("sticker") or {}
        st_dir = (st_cfg.get("dir") or "").strip() or STICKER_DIR
        self.stickers = sticker_mod.StickerStore(st_dir)
        try:
            os.makedirs(st_dir, exist_ok=True)
            if not os.path.exists(self.stickers.index_path):
                n = self.stickers.scan()
                if n:
                    print(f"[stickers] 已索引 {n} 张表情包 -> {st_dir}")
        except Exception as e:
            print(f"[stickers] 初始化失败：{e}")
        try:
            self.migrate_traits()
        except Exception as e:
            print(f"[migrate] 跳过：{e}")

    def migrate_traits(self):
        """把角色卡里调好的初始参数同步进状态表。

        char_state 只在首次创建时读 card.traits，之后就不再看卡了。
        所以老版本升级上来的人，参数永远停在旧默认值——除非显式迁移一次。
        """
        marker = os.path.join(DATA_DIR, ".traits_version")
        try:
            cur = int(open(marker, encoding="utf-8").read().strip())
        except Exception:
            cur = 0
        if cur >= 2:
            return
        n = 0
        for card in self.cards.list():
            t = card.traits or {}
            if not t:
                continue
            try:
                ent = max(0, min(100, int(t.get("enthusiasm", 60))))
                aff = max(0, min(100, int(t.get("affection", 50))))
                ini = float(t.get("initiative", 1.0) or 1.0)
            except (TypeError, ValueError):
                continue
            row = self.store.q1("SELECT char_id FROM char_state WHERE char_id=?", (card.id,))
            if row:
                self.store.x(
                    "UPDATE char_state SET enthusiasm=?,affection=?,initiative=? WHERE char_id=?",
                    (ent, aff, ini, card.id))
            else:
                self.store.x(
                    "INSERT INTO char_state(char_id,enthusiasm,affection,initiative) VALUES(?,?,?,?)",
                    (card.id, ent, aff, ini))
            n += 1
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write("2")
        except Exception:
            pass
        print(f"[migrate] 已按角色卡重置 {n} 个角色的初始参数")

    def reload_llm(self):
        self.cfg = load_config()
        self.llm = LLM(self.cfg["llm"])
        self.thinker = Thinker(self.llm)

    # ---------------- 角色 ----------------
    def char_state(self, card) -> dict:
        row = self.store.q1("SELECT * FROM char_state WHERE char_id=?", (card.id,))
        if not row:
            # 首次加载：用角色卡里的 traits 初始化，而不是无脑默认值
            t = card.traits or {}
            def _i(k, d):
                try:
                    return max(0, min(100, int(t.get(k, d))))
                except (TypeError, ValueError):
                    return d
            self.store.x(
                "INSERT INTO char_state(char_id,enthusiasm,affection,initiative) VALUES(?,?,?,?)",
                (card.id, _i("enthusiasm", 60), _i("affection", 50),
                 float(t.get("initiative", 1.0) or 1.0)),
            )
        st = self.store.state(card.id)
        now = time.time()
        # 精力随时间恢复
        gap_h = (now - (st.get("last_user_at") or now)) / 3600.0
        st["energy"] = traits_mod.energy_drift(st.get("energy", 80), gap_h)
        return st

    def card_payload(self, card) -> dict:
        st = self.char_state(card)
        sch = sched_mod.build(card.name, card.schedule)
        return {
            "id": card.id,
            "slug": card.slug,
            "name": card.name,
            "series": card.series,
            "summary": card.summary,
            "gender": card.gender,
            "signature": card.signature,
            "address": card.address,
            "tags": card.tags,
            "interests": card.interests,
            "accent": card.accent,
            "avatar": f"/api/avatar/{card.slug}",
            "state": {
                "mood": st.get("mood", "平静"),
                "energy": st["energy"],
                "enthusiasm": st.get("enthusiasm", 60),
                "affection": st.get("affection", 50),
            },
            "schedule": sch.to_dict(),
            "asleep": sch.is_asleep(),
            "phase": sched_mod.describe_phase(sch),
            "sessions_per_day": round(traits_mod.sessions_per_day(
                st.get("enthusiasm", 60), st.get("affection", 50), st.get("initiative", 1.0)), 2),
            "featured": card.name in FEATURED_RANK,
            "bond": traits_mod.bond_stage(st.get("affection", 50))["name"],
            "bond_gist": traits_mod.bond_stage(st.get("affection", 50))["gist"],
            "reply_seconds": round(traits_mod.reply_seconds(
                st.get("enthusiasm", 60), st.get("affection", 50), self.rng), 1),
        }

    def my_name(self) -> str:
        """对方希望被怎么称呼。没设过就是「老师」。"""
        mp = os.path.join(DATA_DIR, "me.json")
        if os.path.exists(mp):
            try:
                nm = (json.load(open(mp, encoding="utf-8")).get("name") or "").strip()
                if nm:
                    return nm
            except Exception:
                pass
        return "老师"

    # ---------------- 添加角色草稿 ----------------
    @staticmethod
    def _draft_avatar_bytes(avatar: str) -> bytes | None:
        """只接受本地图片 dataURL 或项目目录内的已存图片，不读取外部 URL。"""
        avatar = str(avatar or "").strip()
        if not avatar:
            return None
        if avatar.startswith("data:"):
            m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", avatar, re.S)
            if not m:
                raise cards_mod.CardError("头像必须是有效的本地图片 dataURL")
            try:
                raw = base64.b64decode(m.group(1), validate=True)
            except Exception as e:
                raise cards_mod.CardError("头像数据损坏，请重新选择图片") from e
        else:
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", avatar):
                raise cards_mod.CardError("头像不能使用外部 URL")
            base = os.path.abspath(BASE)
            path = avatar if os.path.isabs(avatar) else os.path.join(base, avatar)
            path = os.path.abspath(path)
            try:
                if os.path.commonpath([base, path]) != base:
                    raise cards_mod.CardError("头像路径必须位于项目目录内")
                with open(path, "rb") as f:
                    raw = f.read(cards_mod.AVATAR_MAX_BYTES + 1)
            except FileNotFoundError as e:
                raise cards_mod.CardError("头像文件不存在，请重新选择") from e
        if not raw or len(raw) > cards_mod.AVATAR_MAX_BYTES:
            raise cards_mod.CardError("头像为空或超过 8MB")
        return raw

    def generate_draft(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        avatar = str(body.get("avatar") or "").strip()
        self._draft_avatar_bytes(avatar)
        gender_hint = str(body.get("gender") or "").strip().lower()
        generated = persona_builder.generate_card(
            self.llm, name, "", "",
            (self.cfg.get("search") or {}).get("endpoint") or "",
            gender_hint=gender_hint,
        )
        generated["avatar"] = avatar
        return self.drafts.create(generated)

    def import_draft(self, body: dict) -> dict:
        """把 V1/V2、原生卡或预设归一化成可编辑草稿，不直接建卡。"""
        card_obj = body.get("card") if "card" in body else body
        if isinstance(card_obj, str):
            try:
                card_obj = json.loads(card_obj)
            except json.JSONDecodeError as e:
                raise cards_mod.CardError("角色卡 JSON 格式错误") from e
        if not isinstance(card_obj, dict):
            raise cards_mod.CardError("角色卡必须是 JSON 对象")
        try:
            parsed = cards_mod.parse_card_dict(card_obj, require_persona=False)
        except cards_mod.CardError:
            # 星伴原生导出把 persona_md 放在 data 中，且使用自己的 spec 名。
            nested = card_obj.get("data")
            if not isinstance(nested, dict):
                raise
            parsed = cards_mod.parse_card_dict(nested, require_persona=False)
        source = card_obj.get("data") if isinstance(card_obj.get("data"), dict) else card_obj
        avatar = str(body.get("avatar") or source.get("avatar") or "").strip()
        self._draft_avatar_bytes(avatar)
        return self.drafts.create({
            "name": parsed.name,
            "series": parsed.series,
            "aliases": parsed.aliases,
            "summary": parsed.summary,
            "interests": parsed.interests,
            "persona_md": parsed.persona_md,
            "avatar": avatar,
        })

    def trial_draft(self, body: dict) -> dict:
        """独立试聊：只把草稿上下文传给模型，绝不访问聊天、记忆或状态表。"""
        draft = self.drafts.get(body.get("draft_id") or "")
        self.drafts._require_revision(draft, body.get("revision"))
        text = str(body.get("text") or "").strip()[:2000]
        if not text:
            raise drafts_mod.DraftError("试聊消息不能为空")
        if not draft["persona_md"]:
            raise drafts_mod.DraftError("请先填写完整人设再试聊")
        if not self.llm.configured:
            raise LLMError("模型还没配置好，先到「设置」填 Base URL、API Key 和模型名")
        card = cards_mod.InternalCard(
            id="draft", name=draft["name"], slug="draft",
            persona_md=draft["persona_md"], summary=draft["summary"],
            series=draft["series"], aliases=draft["aliases"],
            interests=draft["interests"],
        )
        state = {"mood": "平静", "energy": 80, "affection": 50,
                 "enthusiasm": 60, "last_spoke_at": None}
        recent = list(draft["trial"]) + [{"role": "user", "text": text}]
        reply = self.thinker.express(
            card, state, sched_mod.build(card.name, {}), [], "", "", {}, recent,
            extra_instruction=(
                "这是正式添加前的独立试聊。只回应当前对话，不声称记住了其他聊天，"
                "不要输出分析过程，也不要把试聊当作已经发生的正式关系经历。"
            ),
            user_name=self.my_name(),
        )
        reply = (reply or "……").strip()
        self.drafts.append_trial(draft["id"], draft["revision"], "user", text)
        self.drafts.append_trial(draft["id"], draft["revision"], "char", reply)
        return {"ok": True, "reply": reply, "revision": draft["revision"],
                "trial": self.drafts.get(draft["id"])["trial"]}

    def commit_draft(self, body: dict) -> dict:
        draft = self.drafts.get(body.get("draft_id") or "")
        self.drafts._require_revision(draft, body.get("revision"))
        avatar = self._draft_avatar_bytes(draft["avatar"])
        if not avatar:
            raise cards_mod.CardError("正式添加前请选择头像")
        card = self.cards.create(
            name=draft["name"], persona_md=draft["persona_md"],
            series=draft["series"], summary=draft["summary"],
            interests=draft["interests"], avatar_bytes=avatar,
            gender=draft.get("gender") or "",
        )
        card.aliases = draft["aliases"]
        if not card.signature and card.summary:
            card.signature = re.sub(r"\s+", " ", card.summary.split("，")[0])[:24]
        self.cards.save(card)
        self.drafts.commit(draft["id"], draft["revision"])
        return {"ok": True, "slug": card.slug, "character": self.card_payload(card)}

    # ---------------- 聊天主流程 ----------------
    def chat(self, slug: str, text: str, extra_instruction: str = "",
             store_user: bool = True) -> dict:
        card = self.cards.load(slug)
        st = self.char_state(card)
        sch = sched_mod.build(card.name, card.schedule)
        session_id = self.store.session(card.id)

        # 1. 落用户消息（发图片时已经落过一条，不再重复）
        if store_user:
            uid, uts = self.store.add_message(card.id, session_id, "user", text)
            self.store.update_state(card.id, last_user_at=uts, cold_streak=0)
        else:
            # 发图片时用户消息已经落过了，这里只是接着生成回复。
            # 但要拿到那条消息的 id——下面的记忆抽取要用它当来源，否则会 UnboundLocalError。
            self.store.update_state(card.id, last_user_at=time.time(), cold_streak=0)
            row = self.store.q1(
                "SELECT id, ts FROM messages WHERE char_id=? AND role='user' "
                "ORDER BY id DESC LIMIT 1", (card.id,))
            uid = row["id"] if row else 0
            uts = row["ts"] if row else time.time()

        recent = self.store.recent_messages(card.id, 12)
        analysis = {}
        if self.llm.configured:
            analysis = self.thinker.analyze(recent, card.name)

        # 2. 画像更新（只写 profile，人工可覆盖）
        for kv in (analysis.get("profile_updates") or [])[:5]:
            if isinstance(kv, dict) and kv.get("key") and kv.get("value"):
                self.mem.set_profile(str(kv["key"]), str(kv["value"]))

        # 3. 召回记忆
        top_k = int(self.cfg["memory"]["recall_top_k"])
        hits = self.mem.recall(card.id, text, top_k)

        # 3b. 检索设定集（往年的故事经历，只注入命中的）
        past_intent = lore_mod.is_past_intent(text)
        if past_intent:
            # 问「讲讲你的故事」时走时间线：她的经历是完整一条线，
            # 不该等用户碰巧说出「阿派朗岛」才被想起来。
            timeline = self.lore.retrieve_timeline(card.slug)
            lore_text = lore_mod.format_timeline(timeline)
            lore_hits = timeline
        else:
            lore_hits = self.lore.retrieve(card.slug, text)
            lore_text = lore_mod.format_for_prompt(lore_hits)

        # 3c. 共用世界观：只在真的提到相关词时才补一条
        world_hits = lore_mod.format_world(self.lore.retrieve_world(text))
        if world_hits:
            lore_text = (world_hits + ("\n" + lore_text if lore_text else "")).strip()

        # 4. 生成回复
        if not self.llm.configured:
            reply = "（还没配置模型。打开右上角「设置」填 Base URL、API Key 和模型名，然后再跟我说话。）"
        else:
            sleep_hint = ""
            if sch.is_asleep():
                sleep_hint = (
                    f"你现在正在睡觉（{sch.sleep} 睡、{sch.wake} 起），被这条消息吵醒了。"
                    f"可以用很短的、带困意的语气回一句，理由可以说「{sch.label}」，"
                    "然后表示要接着睡。不要长篇大论。"
                )
            hints = [h for h in (sleep_hint, extra_instruction) if h]
            reply = self.thinker.express(
                card, st, sch, hits,
                self.mem.profile_text(), self.mem.confirmed_facts_text(card.id),
                analysis, recent, extra_instruction="\n".join(hints),
                lore_text=lore_text, past_intent=past_intent,
                user_name=self.my_name(),
            )
        if not reply:
            reply = "……"

        # 5. 落回复 + 更新状态
        bubbles = split_bubbles(reply, int(self.cfg["behavior"]["split_threshold"]),
                                int(self.cfg["behavior"]["max_bubbles"])) \
            if self.cfg["behavior"]["split_long_reply"] else [reply]
        for i, b in enumerate(bubbles):
            self.store.add_message(card.id, session_id, "char", b,
                                   json.dumps({"analysis": analysis if i == 0 else {}}, ensure_ascii=False))

        mood = analysis.get("emotion_label") or st.get("mood") or "平静"
        q = float(analysis.get("quality") or 0.5)
        # 好感随互动质量缓慢上涨；一次最多 +2，避免刷两句就跳档
        gain = 2 if q > 0.75 else (1 if q > 0.5 else 0)
        aff = min(100, int(st.get("affection", 50)) + gain)
        energy = max(5, int(st["energy"]) - self.rng.randint(1, 3))
        self.store.update_state(card.id, last_spoke_at=time.time(), mood=mood,
                                affection=aff, energy=energy)

        # 6. 记忆抽取
        if self.cfg["memory"]["auto_extract"] and self.llm.configured:
            if analysis.get("memory_worthy") and analysis.get("memory_summary"):
                self.mem.add(card.id, analysis["memory_summary"], kind="episodic",
                             emotion=analysis.get("emotion_label"), source_turn=uid)
            for kv in (analysis.get("self_facts") or [])[:3]:
                if isinstance(kv, dict) and kv.get("key") and kv.get("value"):
                    self.mem.propose_fact(card.id, str(kv["key"]), str(kv["value"]),
                                          turn_id=uid, origin="analysis", spoken_text=reply)

        # 7. 安全信号留痕
        if analysis.get("risk") in ("自伤倾向", "极端情绪"):
            self.store.x(
                "INSERT INTO events(char_id,ts,kind,text,emotion,importance,flagged) "
                "VALUES(?,?,?,?,?,?,1)",
                (card.id, time.time(), "risk", text[:300], analysis.get("emotion_label"), 0.95))

        # 8. 表情包：按当轮情绪挑一张（对不上就不发，硬塞反而尴尬）
        sticker = None
        st_cfg = self.cfg.get("sticker") or {}
        if st_cfg.get("enabled") and self.stickers.load():
            prob = float(st_cfg.get("base_probability") or 0.35)
            if self.rng.random() < prob:
                picked = self.stickers.pick(mood, text, self.rng)
                if picked:
                    sticker = {"file": picked["file"],
                               "url": self.stickers.url_of(picked["file"]),
                               "emotion": picked.get("raw_emotion") or ""}
                    self.store.add_message(
                        card.id, session_id, "char", picked["file"],
                        json.dumps({"type": "sticker"}, ensure_ascii=False))

        delay_ms = int(traits_mod.reply_seconds(
            st.get("enthusiasm", 60), st.get("affection", 50), self.rng) * 1000)
        delay_ms = max(600, min(delay_ms, 6000))     # 演示用上限，真跑可放开

        return {
            "bubbles": bubbles,
            "sticker": sticker,
            "delay_ms": delay_ms,
            "analysis": analysis,
            "recalled": [{"text": h["text"], "score": h["score"]} for h in hits],
            "lore": [{"title": e["title"], "kind": e["kind"]} for e in lore_hits],
            "mood": mood,
            "risk": analysis.get("risk", "none"),
        }


    # ---------------- 备份 / 诊断 ----------------
    def make_backup(self) -> bytes:
        """打包角色与数据。不含 config.json（里面有 API Key）。"""
        try:
            self.store.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        buf = io.BytesIO()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        skip = {"companion.db-wal", "companion.db-shm", "browser-profile",
                "sync-temp", "sticker_thumbs"}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("README-备份说明.txt",
                       f"99陪伴 数据备份\n导出于 {stamp}\n\n"
                       "包含：characters/（角色卡与设定集）、data/（聊天记录、记忆、我的资料）\n"
                       "不含：config.json（含 API Key）\n\n"
                       "恢复：把这两个目录覆盖回程序目录即可。\n")
            for root, dirs, files in os.walk(CHAR_DIR):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.join("characters", os.path.relpath(full, CHAR_DIR))
                    try:
                        z.write(full, arc)
                    except Exception:
                        pass
            if os.path.isdir(DATA_DIR):
                for fn in os.listdir(DATA_DIR):
                    if fn in skip:
                        continue
                    full = os.path.join(DATA_DIR, fn)
                    if os.path.isfile(full):
                        try:
                            z.write(full, os.path.join("data", fn))
                        except Exception:
                            pass
        return buf.getvalue()

    def diagnostics(self) -> dict:
        db_size = 0
        integrity = "unknown"
        counts = {}
        dbp = os.path.join(DATA_DIR, "companion.db")
        if os.path.exists(dbp):
            db_size = os.path.getsize(dbp)
            try:
                row = self.store.q1("PRAGMA integrity_check")
                integrity = (row[0] if row else "unknown")
                for t in ("messages", "memories", "sessions", "char_state", "self_facts"):
                    r = self.store.q1(f"SELECT COUNT(*) FROM {t}")
                    counts[t] = r[0] if r else 0
            except Exception as e:
                integrity = f"error: {e}"

        cards = self.cards.list()
        with_lore = 0
        for c in cards:
            try:
                if self.lore.load(c.slug):
                    with_lore += 1
            except Exception:
                pass

        now = clock.now()
        return {
            "clock": {
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "spoken": think_mod.format_time_context(None, now).rstrip("。"),
                "tz": clock.tz_name(),
                "tz_offset_min": clock.offset_minutes(),
                "source": clock.source(),
                "epoch": int(now.timestamp()),
                "app_version": APP_VERSION,
            },
            "model": {
                "configured": self.llm.configured,
                "base_url": self.llm.base,
                "model": self.llm.model,
                "analysis_model": self.llm.analysis_model,
            },
            "db": {
                "path": dbp, "size": db_size, "size_h": f"{db_size/1048576:.1f} MB",
                "integrity": integrity, "counts": counts,
            },
            "characters": {
                "total": len(cards),
                "featured": sum(1 for c in cards if c.name in FEATURED_RANK),
                "with_lore": with_lore,
            },
            "stickers": {"count": len(self.stickers.load()), "dir": self.stickers.root},
            "server": {
                "port": int(os.environ.get("STARLIT_PORT") or 8787),
                "host": os.environ.get("STARLIT_HOST") or "127.0.0.1",
                "lan": lan_ip(),
                "version": "1.0",
            },
        }

    # ---------------- 聊天外观 ----------------
    def load_appearance(self) -> dict:
        p = os.path.join(DATA_DIR, "appearance.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
        return {}

    def save_appearance(self, slug: str, bubble: str, bg: str):
        a = self.load_appearance()
        a[slug] = {"bubble": bubble, "bg": bg}
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "appearance.json"), "w", encoding="utf-8") as f:
            json.dump(a, f, ensure_ascii=False, indent=1)

    # ---------------- 群聊 ----------------
    def group_chat(self, gid: str, text: str) -> dict:
        g = self.groups.get(gid)
        if not g:
            raise ValueError("群不存在")

        bot = "group:" + gid
        sid = self.store.session(bot)
        self.store.add_message(bot, sid, "user", text)
        recent = self.store.recent_messages(bot, 14)

        last = ""
        for m in reversed(recent):
            try:
                meta = json.loads(m["meta"] or "{}")
            except Exception:
                meta = {}
            if meta.get("slug"):
                last = meta["slug"]
                break

        speakers = self.groups.pick_speakers(g, self.cooccur, last, self.rng, 2)
        out = []
        for i, slug in enumerate(speakers):
            try:
                card = self.cards.load(slug)
            except Exception:
                continue
            st = self.char_state(card)
            sch = sched_mod.build(card.name, card.schedule)
            hits = self.mem.recall(card.id, text, 4)
            past_intent = lore_mod.is_past_intent(text)
            if past_intent:
                lore_hits = self.lore.retrieve_timeline(slug, budget=1200, limit=3)
                lore_text = lore_mod.format_timeline(lore_hits)
            else:
                lore_hits = self.lore.retrieve(slug, text, 700)
                lore_text = lore_mod.format_for_prompt(lore_hits)
            analysis = {}
            if i == 0 and self.llm.configured:
                analysis = self.thinker.analyze(recent, card.name)

            extra = (
                "你们在一个群里聊天，不只你一个人在场。"
                "只说 1~3 条短句，像在群里随口插一句。"
                "别问对方的私事，别提只有你俩知道的事，不要 @ 任何人，不要提「群」这个字。"
            )
            if not self.llm.configured:
                reply = "（模型还没配置，先在设置里填 API Key）"
            else:
                reply = self.thinker.express(
                    card, st, sch, hits,
                    self.mem.profile_text(), self.mem.confirmed_facts_text(card.id),
                    analysis, recent, extra_instruction=extra,
                    lore_text=lore_text, past_intent=past_intent,
                    user_name=self.my_name(),
                )
            if not reply:
                continue
            self.store.add_message(
                bot, sid, "char", reply,
                json.dumps({"speaker": card.name, "slug": slug}, ensure_ascii=False))
            out.append({
                "name": card.name,
                "slug": slug,
                "bubbles": split_bubbles(
                    reply, int(self.cfg["behavior"]["split_threshold"]), 3),
            })
            self.store.update_state(card.id, last_spoke_at=time.time())
        return {"speakers": out}


APP = App()


class Handler(BaseHTTPRequestHandler):
    server_version = "StarlitClone/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = unquote(u.path), parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self.api_get(path, qs)
            if path.startswith("/stickers/"):
                return self.sticker_file(path)
            if path.startswith("/uploads/"):
                return self.upload_file(path)
            return self.static(path)
        except cards_mod.CardError as e:
            # 角色被删掉、或者 URL 里的名字不对：这是客户端问题，不是服务端崩了
            return self._err(str(e), 404)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"服务端错误：{e}", 500)

    def api_get(self, path: str, qs: dict):
        if path == "/api/backup":
            data = APP.make_backup()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="99peiban-backup-{stamp}.zip"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/appearance":
            return self._json({"appearance": APP.load_appearance()})

        if path == "/api/diagnostics":
            return self._json(APP.diagnostics())

        if path == "/api/groups":
            out = []
            for g in APP.groups.load():
                names = []
                for slug in (g.get("members") or []):
                    try:
                        names.append(APP.cards.load(slug).name)
                    except Exception:
                        names.append(slug)
                out.append({
                    "id": g.get("id"), "name": g.get("name"),
                    "members": g.get("members") or [],
                    "member_names": names,
                })
            return self._json({"groups": out})

        if path == "/api/group/messages":
            gid = (qs.get("id") or [""])[0]
            rows = APP.store.recent_messages("group:" + gid, 80)
            out = []
            for r in rows:
                try:
                    meta = json.loads(r["meta"] or "{}")
                except Exception:
                    meta = {}
                out.append({
                    "role": r["role"], "text": r["text"], "ts": r["ts"],
                    "speaker": meta.get("speaker") or "",
                    "slug": meta.get("slug") or "",
                })
            return self._json({"messages": out})

        if path == "/api/character-drafts":
            return self._json({"drafts": APP.drafts.list()})

        if path == "/api/characters":
            items = [APP.card_payload(c) for c in APP.cards.list()]
            items.sort(key=char_sort_key)
            return self._json({"characters": items})

        if path == "/api/settings":
            cfg = json.loads(json.dumps(APP.cfg))
            key = cfg["llm"].get("api_key") or ""
            cfg["llm"]["api_key_hint"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
            cfg["llm"]["api_key"] = ""
            cfg["configured"] = APP.llm.configured
            return self._json(cfg)

        if path == "/api/messages":
            slug = (qs.get("char") or [""])[0]
            card = APP.cards.load(slug)
            limit = int((qs.get("limit") or ["60"])[0])
            rows = APP.store.recent_messages(card.id, limit)
            out = []
            for r in rows:
                item = {"role": r["role"], "text": r["text"], "ts": r["ts"], "type": "text"}
                try:
                    meta = json.loads(r["meta"] or "{}")
                except Exception:
                    meta = {}
                if meta.get("type") == "sticker":
                    item["type"] = "sticker"
                    item["url"] = APP.stickers.url_of(r["text"])
                elif meta.get("type") == "image":
                    item["type"] = "image"
                    item["url"] = meta.get("url") or ""
                out.append(item)
            return self._json({"messages": out})

        if path == "/api/story":
            # 列表只给元信息；带 code 才回正文（一章 1500 字，一次全下没必要）
            sp = os.path.join(DATA_DIR, "story.json")
            if not os.path.exists(sp):
                return self._json({"chapters": []})
            try:
                data = json.load(open(sp, encoding="utf-8"))
            except Exception:
                return self._json({"chapters": []})
            chapters = data.get("chapters") or []
            code = (qs.get("code") or [""])[0]
            if code:
                one = next((c for c in chapters if c.get("code") == code), None)
                return self._json({"chapter": one or {}})
            brief = [{"code": c.get("code"), "title": c.get("title"),
                      "year": c.get("year"), "chars": c.get("chars")}
                     for c in chapters]
            return self._json({"chapters": brief})

        if path == "/api/messages/search":
            slug = (qs.get("char") or [""])[0]
            q = (qs.get("q") or [""])[0].strip()
            card = APP.cards.load(slug)
            if not q:
                return self._json({"hits": []})
            rows = APP.store.q(
                "SELECT role,text,ts FROM messages WHERE char_id=? AND text LIKE ? "
                "ORDER BY id DESC LIMIT 60",
                (card.id, f"%{q}%"),
            )
            hits = [{"role": r["role"], "text": r["text"], "ts": r["ts"]} for r in rows]
            return self._json({"hits": hits, "count": len(hits)})

        if path == "/api/memory/center":
            slug = (qs.get("char") or [""])[0]
            card = APP.cards.load(slug)
            mems = [dict(x) for x in APP.mem.all_active(card.id, 200)]
            groups: dict[str, list] = {}
            for m in mems:
                k = m.get("kind") or "其他"
                groups.setdefault(k, []).append({
                    "id": m.get("id"), "text": m.get("text"),
                    "ts": m.get("ts"), "version": m.get("version", 1),
                })
            facts = APP.store.q(
                "SELECT id,key,value,status FROM self_facts WHERE char_id=? "
                "ORDER BY updated_at DESC LIMIT 60", (card.id,))
            lore = APP.lore.load(card.slug)
            return self._json({
                "memories": groups,
                "mem_total": len(mems),
                "facts": [dict(r) for r in facts],
                "lore_count": len(lore),
                "history_count": len([e for e in lore if e.get("kind") == "history"]),
            })

        if path == "/api/stats":
            slug = (qs.get("char") or [""])[0]
            card = APP.cards.load(slug)
            row = APP.store.q(
                "SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last FROM messages WHERE char_id=?",
                (card.id,),
            )
            n = row[0]["n"] if row else 0
            first = row[0]["first"] if row else None
            last = row[0]["last"] if row else None
            mine = APP.store.q(
                "SELECT COUNT(*) n FROM messages WHERE char_id=? AND role='user'", (card.id,)
            )
            st = APP.char_state(card)
            days = 0
            if first:
                days = max(1, int((time.time() - first) / 86400) + 1)
            return self._json({
                "days": days,
                "total": n,
                "mine": mine[0]["n"] if mine else 0,
                "first_at": first,
                "last_at": last,
                "affection": st.get("affection", 50),
                "enthusiasm": st.get("enthusiasm", 60),
                "bond": traits_mod.bond_stage(st.get("affection", 50))["name"],
            })

        if path == "/api/sticker-sets":
            groups = APP.stickers.sets()
            return self._json({"sets": {s["key"]: s for s in groups}})

        if path == "/api/stickers":
            selected = (qs.get("set") or [""])[0].strip()
            query = (qs.get("q") or [""])[0].strip().lower()
            entries = APP.stickers.load()
            if selected and selected != "ALL":
                entries = [e for e in entries if (e.get("source") or "默认") == selected]
            items = [sticker_payload(e) for e in entries]
            if query:
                def matches(item: dict) -> bool:
                    haystack = [item["file"], item["name"], item["desc"],
                                *item["emotions"], *item["tags"]]
                    return any(query in str(text).lower() for text in haystack)
                items = [item for item in items if matches(item)]
            return self._json({
                "enabled": bool((APP.cfg.get("sticker") or {}).get("enabled")),
                "dir": APP.stickers.root,
                "sets": APP.stickers.sets(),
                "items": items,
            })

        if path == "/api/lore":
            slug = (qs.get("char") or [""])[0]
            return self._json({"entries": APP.lore.load(slug),
                               "kinds": lore_mod.KINDS})

        if path == "/api/memories":
            slug = (qs.get("char") or [""])[0]
            card = APP.cards.load(slug)
            rows = [dict(x) for x in APP.mem.all_active(card.id, 200)]
            return self._json({"memories": rows})

        if path == "/api/facts":
            slug = (qs.get("char") or [""])[0]
            card = APP.cards.load(slug)
            rows = APP.store.q(
                "SELECT id,key,value,status,origin,created_at FROM self_facts "
                "WHERE char_id=? ORDER BY updated_at DESC LIMIT 100", (card.id,))
            return self._json({"facts": [dict(r) for r in rows]})

        if path == "/api/profile":
            slug = (qs.get("char") or [""])[0]
            return self._json({"profile": APP.mem.profile_text(100)})

        if path == "/api/me":
            mp = os.path.join(DATA_DIR, "me.json")
            nm = "老师"
            if os.path.exists(mp):
                try:
                    nm = json.load(open(mp, encoding="utf-8")).get("name") or nm
                except Exception:
                    pass
            return self._json({
                "name": nm,
                "avatar": "/api/me/avatar" if os.path.exists(os.path.join(DATA_DIR, "me.png")) else "",
            })

        if path == "/api/me/avatar":
            p = os.path.join(DATA_DIR, "me.png")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, b"", "image/png")

        m = re.match(r"^/api/avatar/([\w\u4e00-\u9fff-]+)$", path)
        if m:
            p = os.path.join(APP.cards.dir_of(m.group(1)), "avatar.png")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, b"", "image/png")

        return self._err("未知接口", 404)

    def sticker_file(self, path: str):
        """表情包图片：必须经 resolve() 校验，防目录穿越。"""
        rel = unquote(path)[len("/stickers/"):]
        full = APP.stickers.resolve(rel)
        if not full:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"", ctype)

    def upload_file(self, path: str):
        """聊天图片。只认 basename，杜绝 ../../ 穿越。"""
        name = os.path.basename(unquote(path)[len("/uploads/"):])
        if not name or name.startswith("."):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        full = os.path.join(UPLOAD_DIR, name)
        if not os.path.exists(full):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"", ctype)

    def static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        elif path in ("/mobile", "/mobile/"):
            path = "/mobile.html"
        rel = path.lstrip("/").replace("..", "")
        full = os.path.join(WEB_DIR, rel)
        if not os.path.exists(full) or os.path.isdir(full):
            return self._send(404, b"404 not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            body = self._body()

            if path == "/api/settings":
                llm = body.get("llm") or {}
                cfg = load_config()
                for k in ("base_url", "model", "analysis_model"):
                    if k in llm:
                        cfg["llm"][k] = str(llm[k]).strip()
                if llm.get("api_key"):
                    cfg["llm"]["api_key"] = str(llm["api_key"]).strip()
                if "temperature" in llm:
                    cfg["llm"]["temperature"] = float(llm["temperature"])
                save_config(cfg)
                APP.reload_llm()
                return self._json({"ok": True, "configured": APP.llm.configured})

            if path == "/api/groups/auto":
                g = APP.groups.auto_build(APP.cooccur, APP.cards.list(), 4)
                if not g:
                    return self._err("角色不够，至少要有两个角色")
                return self._json({"ok": True, "group": g})

            if path == "/api/groups/create":
                g = APP.groups.create(body.get("name") or "", body.get("members") or [])
                return self._json({"ok": True, "group": g})

            if path == "/api/groups/delete":
                APP.groups.delete(body.get("id") or "")
                return self._json({"ok": True})

            if path == "/api/group/chat":
                txt = (body.get("text") or "").strip()
                if not txt:
                    return self._err("消息为空")
                try:
                    return self._json(APP.group_chat(body.get("id") or "", txt))
                except ValueError as e:
                    return self._err(str(e))

            if path == "/api/character-drafts/generate":
                try:
                    return self._json({"ok": True, "draft": APP.generate_draft(body)})
                except (LLMError, cards_mod.CardError, drafts_mod.DraftError) as e:
                    return self._json({"ok": False, "error": str(e)}, 200)

            if path == "/api/character-drafts/import":
                return self._json({"ok": True, "draft": APP.import_draft(body)})

            if path == "/api/character-drafts/chat":
                try:
                    return self._json(APP.trial_draft(body))
                except (LLMError, cards_mod.CardError, drafts_mod.DraftError) as e:
                    return self._json({"ok": False, "error": str(e)}, 200)

            if path == "/api/character-drafts/commit":
                return self._json(APP.commit_draft(body))

            if path == "/api/characters/generate":
                try:
                    gender_hint = str(body.get("gender") or "").strip().lower()
                    draft = persona_builder.generate_card(
                        APP.llm,
                        body.get("name") or "",
                        body.get("series") or "",
                        body.get("notes") or "",
                        (APP.cfg.get("search") or {}).get("endpoint") or "",
                        gender_hint=gender_hint,
                    )
                except LLMError as e:
                    return self._json({"ok": False, "error": str(e)}, 200)
                except Exception as e:
                    return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 200)
                return self._json({"ok": True, "draft": draft})

            if path == "/api/characters/create":
                d = body.get("draft") or body
                avatar = None
                if d.get("avatar_base64"):
                    avatar = base64.b64decode(d["avatar_base64"])
                card = APP.cards.create(
                    name=d.get("name") or "",
                    persona_md=d.get("persona_md") or "",
                    series=d.get("series") or "",
                    summary=d.get("summary") or "",
                    interests=d.get("interests") or [],
                    avatar_bytes=avatar,
                    traits=d.get("traits") or {},
                    schedule=d.get("schedule") or {},
                    gender=(d.get("gender")
                            or guess_gender(d.get("persona_md") or "")),
                )
                for e in (d.get("lore") or []):
                    try:
                        APP.lore.upsert(card.slug, e)
                    except Exception:
                        pass
                # 没给签名就先用简介的首句顶上，之后可以随时改
                if not card.signature:
                    sig = str(d.get("signature") or "").strip()
                    if not sig:
                        sig = (card.summary or "").split("，")[0].strip()
                    if sig:
                        card.signature = re.sub(r"\s+", " ", sig)[:24]
                        APP.cards.save(card)
                return self._json({"ok": True, "slug": card.slug,
                                   "character": APP.card_payload(card)})

            if path == "/api/characters/import":
                name = body.get("filename") or "card.json"
                raw = base64.b64decode(body.get("data_base64") or "")
                card = APP.cards.import_bytes(raw, name)
                for e in card.lore:
                    try:
                        APP.lore.upsert(card.slug, e)
                    except Exception:
                        pass
                return self._json({"ok": True, "slug": card.slug,
                                   "character": APP.card_payload(card)})

            if path == "/api/chat/image":
                # 对方发来一张图片：存盘、落一条图片消息，再让她"看见"并回一句
                slug = body.get("slug") or ""
                card = APP.cards.load(slug)
                raw = base64.b64decode(body.get("data_base64") or "")
                if not raw:
                    return self._err("图片为空")
                png = cards_mod.normalize_avatar(raw)      # 复用：缩放 + 转 PNG
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                name = f"img-{int(time.time() * 1000)}-{random.randint(100, 999)}.png"
                with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
                    f.write(png)
                url = f"/uploads/{name}"
                sid = APP.store.session(card.id)
                _mid, ts = APP.store.add_message(
                    card.id, sid, "user", "[图片]",
                    json.dumps({"type": "image", "url": url}, ensure_ascii=False))
                # 让她知道收到一张图。重点：她看不到画面内容，必须明说，
                # 否则模型会凭空调色（实测会说"一只很普通的猫"）。
                image_hint = (
                    "对方刚给你发了一张图片。你只能知道「收到了一张图」这件事，"
                    "**看不到图片里画的是什么**。\n"
                    "所以不要描述画面内容、不要说「我看到……」、不要猜这是什么。\n"
                    "可以自然地反应一下（凑近看、问拍的什么、让对方讲讲），"
                    "一到两句就够，别解释你为什么看不见。"
                )
                reply = (APP.chat(slug, "[图片]", extra_instruction=image_hint,
                                  store_user=False)
                         if APP.llm.configured else None)
                return self._json({"ok": True, "url": url, "ts": ts,
                                   "reply": reply})

            if path == "/api/messages/clear":
                card = APP.cards.load(body.get("slug") or "")
                APP.store.x("DELETE FROM messages WHERE char_id=?", (card.id,))
                return self._json({"ok": True})

            if path == "/api/chat":
                slug = body.get("slug") or ""
                text = (body.get("text") or "").strip()
                if not text:
                    return self._err("消息为空")
                return self._json(APP.chat(slug, text))

            if path == "/api/lore/save":
                item = APP.lore.upsert(body.get("slug") or "", body.get("entry") or {})
                return self._json({"ok": True, "entry": item})

            if path == "/api/lore/delete":
                APP.lore.delete(body.get("slug") or "", body.get("id") or "")
                return self._json({"ok": True})

            if path == "/api/memories/revise":
                mid = int(body.get("id") or 0)
                new_text = (body.get("text") or "").strip()
                if not mid or not new_text:
                    return self._err("缺少 id 或 text")
                nid = APP.mem.revise(mid, new_text)
                return self._json({"ok": True, "new_id": nid})

            if path == "/api/facts/confirm":
                fid = int(body.get("id") or 0)
                APP.mem.confirm_fact(fid)
                return self._json({"ok": True})

            if path == "/api/diagnostics/test":
                try:
                    return self._json(APP.llm.ping())
                except LLMError as e:
                    return self._json({"ok": False, "error": str(e)}, 200)

            if path == "/api/stickers/upload":
                entry = APP.stickers.upload(
                    body.get("name") or "表情", body.get("data") or "",
                    body.get("set") or "默认")
                return self._json({"ok": True, "item": sticker_payload(entry)})

            if path == "/api/stickers/delete":
                APP.stickers.delete(body.get("file") or "")
                return self._json({"ok": True})

            if path == "/api/sticker-category/create":
                created = APP.stickers.create_category(body.get("name") or "")
                return self._json({"ok": True, "set": created})

            if path == "/api/sticker-category/delete":
                APP.stickers.delete_category(body.get("name") or "")
                return self._json({"ok": True})

            if path == "/api/stickers/scan":
                n = APP.stickers.scan(force=bool(body.get("force")))
                return self._json({"ok": True, "count": n})

            if path == "/api/stickers/send":
                card = APP.cards.load(body.get("slug") or "")
                rel = body.get("file") or ""
                if not APP.stickers.resolve(rel):
                    return self._err("表情包不存在")
                sid = APP.store.session(card.id)
                APP.store.add_message(card.id, sid, "user", rel,
                                       json.dumps({"type": "sticker"}, ensure_ascii=False))
                return self._json({"ok": True, "url": APP.stickers.url_of(rel)})

            if path == "/api/open-data":
                try:
                    if os.name == "nt":
                        os.startfile(DATA_DIR)
                    else:
                        import subprocess
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", DATA_DIR])
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 200)

            if path == "/api/avatar/upload":
                slug = body.get("slug") or ""
                raw = base64.b64decode(body.get("data_base64") or "")
                card = APP.cards.load(slug)
                png = cards_mod.normalize_avatar(raw)
                with open(os.path.join(APP.cards.dir_of(card.slug), "avatar.png"), "wb") as f:
                    f.write(png)
                return self._json({"ok": True, "url": f"/api/avatar/{card.slug}?t={int(time.time())}"})

            if path == "/api/appearance/save":
                slug = body.get("slug") or ""
                bubble = str(body.get("bubble") or "")[:9]
                bg = str(body.get("bg") or "")[:9]
                ok = re.match(r"^#[0-9a-fA-F]{6}$", bubble) and re.match(r"^#[0-9a-fA-F]{6}$", bg)
                if not slug or not ok:
                    return self._err("参数不合法")
                APP.save_appearance(slug, bubble, bg)
                return self._json({"ok": True})

            if path == "/api/me":
                nm = (body.get("name") or "").strip()[:20]
                if not nm:
                    return self._err("昵称不能为空")
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(os.path.join(DATA_DIR, "me.json"), "w", encoding="utf-8") as f:
                    json.dump({"name": nm}, f, ensure_ascii=False)
                return self._json({"ok": True, "name": nm})

            if path == "/api/me/avatar":
                raw = body.get("data_base64")
                if raw:
                    png = cards_mod.normalize_avatar(base64.b64decode(raw))
                    with open(os.path.join(DATA_DIR, "me.png"), "wb") as f:
                        f.write(png)
                    return self._json({"ok": True, "url": f"/api/me/avatar?t={int(time.time())}"})
                if body.get("reset"):
                    p = os.path.join(DATA_DIR, "me.png")
                    if os.path.exists(p):
                        os.remove(p)
                    return self._json({"ok": True, "url": ""})
                return self._err("缺少图片数据")

            if path == "/api/characters/signature":
                card = APP.cards.load(body.get("slug") or "")
                sig = str(body.get("signature") or "").strip()
                sig = re.sub(r"\s+", " ", sig)[:40]
                card.signature = sig
                APP.cards.save(card)
                return self._json({"ok": True, "signature": card.signature})

            if path == "/api/characters/state":
                card = APP.cards.load(body.get("slug") or "")
                APP.char_state(card)          # 确保状态行存在
                upd = {}
                for k in ("enthusiasm", "affection"):
                    if k in body:
                        try:
                            upd[k] = max(0, min(100, int(body[k])))
                        except (TypeError, ValueError):
                            pass
                if "initiative" in body:
                    try:
                        upd["initiative"] = max(0.2, min(3.0, float(body["initiative"])))
                    except (TypeError, ValueError):
                        pass
                if "mood" in body:
                    upd["mood"] = str(body["mood"])[:16]
                if upd:
                    APP.store.update_state(card.id, **upd)
                return self._json({"ok": True, "character": APP.card_payload(card)})

            if path == "/api/characters/delete":
                APP.cards.delete(body.get("slug") or "")
                return self._json({"ok": True})

            return self._err("未知接口", 404)
        except (cards_mod.CardError, drafts_mod.DraftError, sticker_mod.StickerError) as e:
            return self._err(str(e), 200)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_PATCH(self):
        """保存草稿修改；revision 不一致时拒绝覆盖。"""
        try:
            body = self._body()
            if urlparse(self.path).path != "/api/character-drafts":
                return self._err("未知接口", 404)
            draft_id = body.get("draft_id") or ""
            changes = {k: v for k, v in body.items() if k not in ("draft_id", "revision")}
            if "avatar" in changes:
                APP._draft_avatar_bytes(str(changes.get("avatar") or ""))
            draft = APP.drafts.patch(draft_id, body.get("revision"), changes)
            return self._json({"ok": True, "draft": draft})
        except (cards_mod.CardError, drafts_mod.DraftError, sticker_mod.StickerError) as e:
            return self._err(str(e), 200)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_DELETE(self):
        return self.do_POST()


def lan_ip() -> str:
    """取本机在局域网里的 IPv4，供手机访问。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    port = int(os.environ.get("STARLIT_PORT") or 8787)
    host = os.environ.get("STARLIT_HOST") or "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    ip = lan_ip()
    print("=" * 56)
    print("  99陪伴")
    print(f"  电脑： http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  手机： http://{ip}:{port}/mobile")
        print("        （同一 Wi-Fi 下用手机浏览器打开，可添加到主屏幕）")
    else:
        print(f"  手机： 需以 STARLIT_HOST=0.0.0.0 启动才能从手机访问")
    print(f"  角色目录: {CHAR_DIR}")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()


def start_server(port: int = 8787, host: str = "127.0.0.1"):
    """给 Android（Chaquopy）等外部环境调用的入口，不打印横幅、不阻塞主线程。"""
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.serve_forever()
