"""HTTP 服务 + 主流程编排（标准库实现，零第三方依赖，需 requests/Pillow）。

启动：python server.py        → http://127.0.0.1:8787
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

if getattr(sys, "frozen", False):
    # PyInstaller：可写数据放 exe 旁边，只读资源在 _MEIPASS
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

HERE = BASE
sys.path.insert(0, HERE)

from core import cards as cards_mod
from core import groups as groups_mod
from core import lore as lore_mod
from core import persona_builder
from core import schedule as sched_mod
from core import sticker as sticker_mod
from core import traits as traits_mod
from core.db import Store
from core.llm import LLM, LLMError
from core.memory import Memory
from core.think import Thinker

WEB_DIR = os.path.join(RES, "web")
CHAR_DIR = os.path.join(HERE, "characters")
DATA_DIR = os.path.join(HERE, "data")
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

    # ---------------- 聊天主流程 ----------------
    def chat(self, slug: str, text: str) -> dict:
        card = self.cards.load(slug)
        st = self.char_state(card)
        sch = sched_mod.build(card.name, card.schedule)
        session_id = self.store.session(card.id)

        # 1. 落用户消息
        uid, uts = self.store.add_message(card.id, session_id, "user", text)
        self.store.update_state(card.id, last_user_at=uts, cold_streak=0)

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
        lore_hits = self.lore.retrieve(card.slug, text)
        lore_text = lore_mod.format_for_prompt(lore_hits)

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
            reply = self.thinker.express(
                card, st, sch, hits,
                self.mem.profile_text(), self.mem.confirmed_facts_text(card.id),
                analysis, recent, extra_instruction=sleep_hint,
                lore_text=lore_text,
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
            lore_hits = self.lore.retrieve(slug, text, 700)
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
                    lore_text=lore_mod.format_for_prompt(lore_hits),
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
            return self.static(path)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"服务端错误：{e}", 500)

    def api_get(self, path: str, qs: dict):
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
                out.append(item)
            return self._json({"messages": out})

        if path == "/api/stickers":
            items = []
            for e in APP.stickers.load():
                items.append({
                    "file": e.get("file"),
                    "url": APP.stickers.url_of(e.get("file", "")),
                    "desc": e.get("desc", ""),
                    "emotions": e.get("emotions") or [],
                    "source": e.get("source", ""),
                })
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
            return self._json({"memories": APP.mem.all_active(card.id, 200)})

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

    def static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
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

            if path == "/api/characters/generate":
                try:
                    draft = persona_builder.generate_card(
                        APP.llm,
                        body.get("name") or "",
                        body.get("series") or "",
                        body.get("notes") or "",
                        (APP.cfg.get("search") or {}).get("endpoint") or "",
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
                )
                for e in (d.get("lore") or []):
                    try:
                        APP.lore.upsert(card.slug, e)
                    except Exception:
                        pass
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

            if path == "/api/stickers/scan":
                n = APP.stickers.scan(force=bool(body.get("force")))
                return self._json({"ok": True, "count": n})

            if path == "/api/stickers/send":
                card = APP.cards.load(body.get("slug") or "")
                rel = body.get("file") or ""
                if not APP.stickers.resolve(rel):
                    return self._err("表情包不存在")
                sid = APP.store.session(card.id)
                self.store.add_message(card.id, sid, "user", rel,
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
        except cards_mod.CardError as e:
            return self._err(str(e), 200)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_DELETE(self):
        return self.do_POST()


def main():
    port = int(os.environ.get("STARLIT_PORT") or 8787)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 56)
    print("  星伴克隆 · Starlit Clone")
    print(f"  http://127.0.0.1:{port}")
    print(f"  角色目录: {CHAR_DIR}")
    print(f"  数据文件: {os.path.join(DATA_DIR, 'companion.db')}")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
