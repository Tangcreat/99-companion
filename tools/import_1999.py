#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量导入「重返未来1999」角色（173 位）。

以 characters/*.md 里的人物形象卡为主源——它覆盖全部 173 个角色，
字段比 personas/ 的 JSON 更全（说话方式的习惯/禁忌都拆开了）。
personas/*.json 只用来补充 knowledge（md 里没有这一节）。

md 的「人物形象卡」长这样：

    ## 人物形象卡
    **身份**：...
    **阵营**：...
    **性格**
    - 条目
    **核心动机**：...
    **缺陷／矛盾**：...
    **说话方式**
    - 语气：...
    - 句式：...
    - 禁忌：...
    **关系**
    | 名字 | 关系 | 态度 |
    **人物弧线**：...
    **扮演提示**
    - ...

映射：上面 → persona.md；关系表 → lore(person)；扮演提示 → persona.md 末节；
「同场角色」表 → 群聊分组依据（存进 data/cooccur.json）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.cards import CardStore          # noqa: E402
from core.lore import LoreStore           # noqa: E402

LIB = r"C:\Users\17807\Desktop\重返未来1999_故事库"
CHAR_DIR = os.path.join(ROOT, "characters")
DATA_DIR = os.path.join(ROOT, "data")

SERIES = "重返未来：1999"
PALETTE = [
    "#3a5a8c", "#c4814e", "#b04a4a", "#4f8f8f", "#8a7434",
    "#7a5a9c", "#3f7a5c", "#a05a7a", "#5a6f8c", "#8c6a3a",
]
STOP = set("的了是在有不这那和与或等就都而及对为以于自其之而且但如果因为所以可以个你我他她们它们上下来去说要还会能很非常把被将从向给让")


# ---------------------------------------------------------------- 解析
def _field(sec: str, name: str) -> str:
    m = re.search(rf"\*\*{name}\*\*[：:]\s*(.+)", sec)
    return m.group(1).strip() if m else ""


def _bullets(sec: str, name: str, end_names: list[str]) -> list[str]:
    end = "|".join(re.escape(n) for n in end_names)
    m = re.search(rf"\*\*{name}\*\*\s*\n(.*?)(?=\n\*\*(?:{end})\*\*|\Z)", sec, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[2:].strip())
    return out


def _table(sec: str, after: str) -> list[list[str]]:
    m = re.search(rf"\*\*{re.escape(after)}\*\*\s*\n((?:\|.*\n)+)", sec)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if cells[0] in ("名字", "角色", "名称"):
            continue
        rows.append(cells)
    return rows


def parse_persona_section(md_path: str) -> dict | None:
    """只读到 `## 档案` 为止，避免把几千行台词全部读进来。"""
    if not os.path.exists(md_path):
        return None
    buf = []
    with open(md_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("## ") and "人物形象卡" not in line and buf:
                break
            buf.append(line)
            if len(buf) > 400:
                break
    text = "".join(buf)
    m = re.search(r"## 人物形象卡\s*\n(.*)", text, re.S)
    if not m:
        return None
    sec = m.group(1)

    speech_block = ""
    ms = re.search(r"\*\*说话方式\*\*\s*\n(.*?)(?=\n\*\*(?:关系|人物弧线|扮演提示|示例对话|禁忌)\*\*|\Z)", sec, re.S)
    if ms:
        speech_block = ms.group(1)

    def speech(kind: str) -> str:
        hits = re.findall(rf"-\s*{kind}[：:]\s*(.+)", speech_block)
        return "；".join(h.strip() for h in hits)

    tone = speech("语气")
    sentence = speech("句式")
    address = speech("称呼")
    habits = [h.strip() for h in re.findall(r"-\s*习惯[：:]\s*(.+)", speech_block)]
    forbids = [h.strip() for h in re.findall(r"-\s*禁忌[：:]\s*(.+)", speech_block)]

    return {
        "identity": _field(sec, "身份"),
        "faction": _field(sec, "阵营"),
        "era": _field(sec, "时代与地点") or _field(sec, "时代"),
        "traits": _bullets(sec, "性格", ["核心动机", "缺陷／矛盾", "缺陷", "说话方式"]),
        "core": _field(sec, "核心动机"),
        "flaw": _field(sec, "缺陷／矛盾") or _field(sec, "缺陷"),
        "tone": tone, "sentence": sentence, "address": address,
        "habits": habits, "forbidden": forbids,
        "relationships": _table(sec, "关系"),
        "arc": _field(sec, "人物弧线"),
        "hints": _bullets(sec, "扮演提示", ["示例对话"]),
    }


def parse_cooccur(md_path: str) -> list[tuple[str, int]]:
    """「同场角色（按共同出场关卡数）」——群聊分组用。"""
    if not os.path.exists(md_path):
        return []
    out, started = [], False
    with open(md_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("## 同场角色"):
                started = True
                continue
            if started:
                if line.startswith("## "):
                    break
                m = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", line)
                if m:
                    name = m.group(1).strip()
                    if name and name not in ("角色", "名字"):
                        out.append((name, int(m.group(2))))
    return out


def front_matter(md_path: str) -> dict:
    d = {}
    with open(md_path, encoding="utf-8", errors="replace") as f:
        head = f.read(1200)
    m = re.match(r"^---\n(.*?)\n---", head, re.S)
    if not m:
        return d
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = v.strip()
    return d


# ---------------------------------------------------------------- 组装
def build_persona_md(p: dict) -> str:
    L = ["## 她是谁", p["identity"] or "（资料缺失）"]
    if p["faction"]:
        L += ["", "## 立场", p["faction"]]
    if p["era"]:
        L += ["", "## 所处的时代", p["era"]]

    L += ["", "## 性格"]
    if p["core"]:
        L.append(f"**核心动机**：{p['core']}")
    if p["flaw"]:
        L.append(f"**她的缺陷**：{p['flaw']}")
    if p["traits"]:
        L.append("")
        L += [f"- {t}" for t in p["traits"]]

    if p["tone"] or p["sentence"] or p["habits"] or p["forbidden"]:
        L += ["", "## 说话方式"]
        if p["tone"]:
            L.append(f"**语气**：{p['tone']}")
        if p["sentence"]:
            L.append(f"**句式**：{p['sentence']}")
        if p["habits"]:
            L += ["", "**习惯**"] + [f"- {h}" for h in p["habits"]]
        if p["address"]:
            L += ["", f"**怎么称呼人**：{p['address']}"]
        if p["forbidden"]:
            L += ["", "**不要这样**"] + [f"- {h}" for h in p["forbidden"]]

    if p["arc"]:
        L += ["", "## 她的转变", p["arc"]]
    if p["hints"]:
        L += ["", "## 聊天时的注意"] + [f"- {h}" for h in p["hints"]]
    return "\n".join(L).strip()


def build_lore(p: dict, name: str) -> list[dict]:
    out = []
    for row in p["relationships"][:10]:
        who = row[0]
        if not who or who == name:
            continue
        parts = [x for x in row[1:] if x]
        out.append({
            "title": who[:20], "kind": "person",
            "keywords": [who][:3],
            "content": "；".join(parts) or who,
            "always": False, "priority": 5,
        })
    if p["arc"]:
        out.append({
            "title": "她的转变", "kind": "history",
            "keywords": [], "content": p["arc"][:400],
            "always": False, "priority": 3,
        })
    return out


def parse_fallback(md_path: str) -> dict | None:
    """没有形象卡的 143 个角色：档案 + 语言特征 + 真实台词抽样。

    台词是这人亲口说的（文件本身就是他的专属档案），
    所以抽出来的样本比任何"说话方式"总结都准。
    """
    text = open(md_path, encoding="utf-8", errors="replace").read()

    archive = ""
    m = re.search(r"## 档案\s*\n(.*?)(?=\n## )", text, re.S)
    if m:
        archive = re.sub(r"\s+", " ", m.group(1)).strip().strip("-").strip()

    arc_lines = []
    m = re.search(r"## 主线轨迹\s*\n(.*?)(?=\n## )", text, re.S)
    if m:
        arc_lines = [l[2:].strip() for l in m.group(1).splitlines() if l.strip().startswith("- ")]

    stats: dict[str, str] = {}
    m = re.search(r"## 语言特征[^\n]*\n(.*?)(?=\n## )", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != 2 or cells[0] in ("指标", ""):
                continue
            if set("".join(cells)) <= set("-: "):
                continue
            stats[cells[0]] = cells[1]

    samples: list[str] = []
    idx = text.find("## 全部台词")
    if idx > 0:
        for line in text[idx:].splitlines():
            mm = re.match(r"-\s*【[^】]*】(.+)", line)
            if not mm:
                continue
            s = mm.group(1).strip()
            if not (10 <= len(s) <= 46):
                continue
            if re.fullmatch(r"[.…。、，！？—\s]+", s):
                continue
            if s in samples:
                continue
            samples.append(s)
            if len(samples) >= 10:
                break

    if not archive and not stats and not samples:
        return None

    L: list[str] = []
    if archive:
        L += ["## 她是谁", archive[:320]]
    if arc_lines:
        L += ["", "## 出场", "\n".join(f"- {x}" for x in arc_lines[:6])]
    if stats:
        L += ["", "## 说话方式（据全部台词统计）"]
        for k in ("平均句长", "疑问句占比", "感叹句占比", "含省略号占比", "高频语气词", "常用自称"):
            if k in stats:
                L.append(f"- {k}：{stats[k]}")
        L += ["", "说话时贴合上面的句长与语气词密度，不要突然变得健谈或热情。"]
    if samples:
        L += ["", "## 真实台词样本（照着这个语感说话）"]
        L += [f"- 「{s}」" for s in samples[:8]]

    entries = []
    if arc_lines:
        entries.append({
            "title": "出场章节", "kind": "history", "keywords": [],
            "content": "；".join(arc_lines[:6])[:400],
            "always": False, "priority": 3,
        })
    return {
        "persona_md": "\n".join(L).strip(),
        "identity": archive[:120],
        "entries": entries,
    }


def knowledge_lore(k: str, idx: int) -> dict:
    text = (k or "").strip()
    title = text[:14].replace("\n", " ")
    title = title.rstrip("，。、；")
    return {
        "title": title, "kind": "world", "keywords": [],
        "content": text, "always": False, "priority": 4,
    }


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=LIB)
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    ap.add_argument("--force", action="store_true", help="跳过已存在的角色")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    lib = args.lib
    char_dir = os.path.join(lib, "characters")
    files = sorted(f for f in os.listdir(char_dir) if f.endswith(".md"))
    if args.limit:
        files = files[: args.limit]

    personas = {}
    pp = os.path.join(lib, "data", "personas.json")
    if os.path.exists(pp):
        for p in json.load(open(pp, encoding="utf-8")):
            personas[p["id"]] = p

    cards = CardStore(CHAR_DIR)
    lore = LoreStore(CHAR_DIR)
    existing = {c.name for c in cards.list()} if not args.force else set()

    cooccur = {}
    ok = skipped = failed = 0
    total_entries = 0

    for fn in files:
        path = os.path.join(char_dir, fn)
        fm = front_matter(path)
        name = fm.get("name") or fn.split("_", 1)[-1].replace(".md", "")
        cid = fm.get("id") or ""

        if name in existing:
            skipped += 1
            continue

        p = parse_persona_section(path)
        if p and p.get("identity"):
            persona_md = build_persona_md(p)
            entries = build_lore(p, name)
            rels = p.get("relationships") or []
        else:
            fb = parse_fallback(path)
            if not fb or len(fb["persona_md"]) < 120:
                failed += 1
                continue
            persona_md = fb["persona_md"]
            entries = fb["entries"]
            rels = []
            p = {"identity": fb["identity"]}

        if len(persona_md) < 120:
            failed += 1
            continue

        # 补充 personas JSON 里的 knowledge
        jp = personas.get(cid)
        if jp:
            for i, k in enumerate(jp.get("knowledge") or []):
                if (k or "").strip():
                    entries.append(knowledge_lore(k, i))
            # JSON 的 relationships 更结构化，覆盖 md 的
            if jp.get("relationships"):
                entries = [e for e in entries if e["kind"] != "person"]
                for r in jp["relationships"][:10]:
                    who = (r.get("name") or "").strip()
                    if not who or who == name:
                        continue
                    parts = [str(r.get("relation") or "").strip(),
                             str(r.get("attitude") or "").strip()]
                    entries.append({
                        "title": who[:20], "kind": "person",
                        "keywords": [who][:3],
                        "content": "；".join(x for x in parts if x),
                        "always": False, "priority": 5,
                    })

        co = parse_cooccur(path)
        if co:
            cooccur[name] = co[:12]

        accent = PALETTE[ok % len(PALETTE)]
        try:
            card = cards.create(
                name=name, persona_md=persona_md, series=SERIES,
                summary=(p["identity"] or "")[:40],
                interests=[r[0] for r in rels[:4]],
                tags=[SERIES, fm.get("tier") or ""],
                accent=accent,
                traits={"enthusiasm": 50 + (ok % 30), "affection": 40 + (ok % 40)},
                min_chars=80,
            )
        except Exception as e:
            failed += 1
            if not args.quiet:
                print(f"  ✗ {name}: {e}")
            continue

        for e in entries:
            try:
                lore.upsert(card.slug, e)
            except Exception:
                pass
        total_entries += len(entries)
        ok += 1
        if not args.quiet and ok % 20 == 0:
            print(f"  … 已导入 {ok} 个")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "cooccur.json"), "w", encoding="utf-8") as f:
        json.dump(cooccur, f, ensure_ascii=False, indent=1)

    print(f"\n完成：导入 {ok} 个 / 跳过 {skipped} 个 / 失败 {failed} 个")
    print(f"设定条目合计 {total_entries} 条；同场关系 {len(cooccur)} 人 -> data/cooccur.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
