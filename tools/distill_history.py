#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从故事库蒸馏角色过往经历。

不喂整章剧本（那会爆上下文），而是给模型三样东西：
  1. 每章的官方梗概（chapters.json 的 summary.brief，一句话）
  2. 该角色在这一章里说过的、信息量较高的若干句台词
  3. 该章的年代地点

让它据此写出「她这一段经历了什么」。产出直接进 lore.json（kind=history），
这样聊天时提到某个章节/事件才会被检索注入。

只做 20 位高人气角色——名单按 17173 的全 6 星喜爱度投票（2025-12 / 2026-02）
加台词量交叉确认。配角龙套的经历对陪伴场景价值不高，跳过。

用法：
    python tools/distill_history.py --dry            # 只看会处理谁
    python tools/distill_history.py --only 37        # 跑单个
    python tools/distill_history.py                  # 跑全部 20 位
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.llm import LLM, LLMError        # noqa: E402
from core.lore import LoreStore          # noqa: E402

LIB = r"C:\Users\17807\Desktop\重返未来1999_故事库"
CHAR_DIR = os.path.join(ROOT, "characters")
DATA_DIR = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config.json")

# 人气 20 人：投票名次为主，台词量为辅
TOP20 = [
    "37", "维尔汀", "十四行诗", "露西", "贝丽尔",
    "纸信圈儿", "玛丽安娜", "红弩箭", "斯奈德", "未锈铠",
    "苏菲亚", "鹭鸶剪", "程和光", "梁月", "牙仙",
    "雾行者", "霍夫曼", "星锑", "伊索尔德", "卡卡尼亚",
]

SYSTEM = """你在为角色扮演游戏整理角色履历。

规则：
- 只依据给定材料，不要编造材料里没有的剧情；材料没写的就写得笼统些。
- 用第三人称「她/他」+ 角色本名来写，不要用「这个角色」。
- 每条经历要能回答：他在这一段做了什么、遇到谁、付出或失去了什么。
- 不要写成剧情简介，要写成「这个人的经历」——带立场和代价。
- 不要出现「玩家」「主线」「关卡」这类游戏术语。

只输出一个 JSON 对象，不要任何解释和代码块：
{
  "timeline": [
    {"period": "章节名", "event": "120~220 字，她在这段经历了什么", "keywords": ["3到5个具体触发词"]}
  ],
  "lore": [
    {"title": "不超过 10 字的短标题", "content": "60~120 字，一条独立的事实：某个约定、某个人、某道伤疤", "keywords": ["2到4个触发词"]}
  ]
}

timeline 按材料里的章节顺序输出，有几章给几条。
lore 给 3~5 条，专挑「聊天时可能被提起」的细节，不要和 timeline 重复。"""


def load_chapters() -> dict:
    p = os.path.join(LIB, "data", "chapters.json")
    out = {}
    for c in json.load(open(p, encoding="utf-8")):
        out[c.get("code") or ""] = c
    return out


def collect_lines(name: str, aliases: list[str] | None = None) -> dict[str, list[str]]:
    """按章节收集该角色的台词。

    dialogue.jsonl 里中文名是 \\uXXXX 转义形式，所以廉价过滤要把两种写法都试一遍，
    否则一个字都捞不到（这个坑踩过）。
    """
    p = os.path.join(LIB, "data", "dialogue.jsonl")
    forms = [f'"{name}"', json.dumps(name, ensure_ascii=True)]
    for a in (aliases or []):
        forms += [f'"{a}"', json.dumps(a, ensure_ascii=True)]
    names = set([name] + list(aliases or []))

    by_ch: dict[str, list[str]] = defaultdict(list)
    with open(p, encoding="utf-8") as f:
        for line in f:
            if not any(x in line for x in forms):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "dialogue":
                continue
            if (d.get("speaker") or "").strip() not in names:
                continue
            t = (d.get("text") or "").strip()
            if t:
                by_ch[d.get("chapter") or ""].append(t)
    return by_ch


def pick_lines(texts: list[str], n: int = 8) -> list[str]:
    """挑信息量高的：长度适中、用词丰富。"""
    cand = [t for t in texts if 12 <= len(t) <= 62 and "……" not in t[:3]]
    # 词汇越丰富越可能有具体信息
    cand.sort(key=lambda t: (-len(set(t)), -len(t)))
    seen, out = set(), []
    for t in cand:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= n:
            break
    return out


def build_input(name: str, by_ch: dict, chapters: dict) -> tuple[str, list[str]]:
    order = [c.get("code") for c in sorted(chapters.values(), key=lambda x: x.get("order", 99))]
    blocks, used = [], []
    for code in order:
        lines = by_ch.get(code) or []
        if not lines:
            continue
        if len(used) >= 12:            # 章节太多会撑爆输出，取前 12 章
            break
        ch = chapters.get(code) or {}
        brief = ((ch.get("summary") or {}).get("brief") or "").strip()
        picked = pick_lines(lines, 6)
        if not picked and not brief:
            continue
        used.append(code)
        head = f"【{code} · {ch.get('title') or ''}】{ch.get('year') or ''}"
        parts = [head]
        if brief:
            parts.append(f"官方梗概：{brief}")
        if picked:
            parts.append("她说过：" + " / ".join(f"「{t}」" for t in picked))
        blocks.append("\n".join(parts))

    # 个人剧情/轶事：不在 chapters.json 里，没有官方梗概，只有台词
    for code, lines in by_ch.items():
        if code in order:
            continue
        if len(used) >= 14:
            break
        picked = pick_lines(lines, 6)
        if not picked:
            continue
        used.append(code)
        blocks.append(
            f"【{code}】（个人剧情）\n她说过：" + " / ".join(f"「{t}」" for t in picked))

    return "\n\n".join(blocks), used


def distill(llm: LLM, name: str, material: str) -> dict:
    raw = llm.chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": f"角色：{name}\n\n材料：\n{material}"}],
        temperature=0.6, max_tokens=4096,
    )
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        s = s.split("\n", 1)[1] if "\n" in s else s
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def persona_material(slug: str) -> str:
    """从 persona.md 里抽出档案/身份段落，作为蒸馏的兜底材料。"""
    p = os.path.join(CHAR_DIR, slug, "persona.md")
    if not os.path.exists(p):
        return ""
    try:
        text = open(p, encoding="utf-8").read()
    except Exception:
        return ""
    # 优先取「档案」「身份」「来历」这几节
    keep = []
    cur_hit = False
    for line in text.splitlines():
        if line.startswith("#"):
            cur_hit = any(k in line for k in ("档案", "身份", "来历", "经历", "生平"))
            if cur_hit:
                keep.append(line)
            continue
        if cur_hit and line.strip():
            keep.append(line.strip())
    body = "\n".join(keep).strip()
    if len(body) < 40:
        body = text.strip()[:900]
    return f"【角色档案】\n{body[:1200]}" if body else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--all", action="store_true",
                    help="处理 characters/ 下全部角色（已蒸馏的自动跳过）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.only:
        names = [x.strip() for x in args.only.split(",") if x.strip()]
    elif args.all:
        names = sorted(d for d in os.listdir(CHAR_DIR)
                       if os.path.isdir(os.path.join(CHAR_DIR, d)))
        names = [n for n in names if os.path.exists(
            os.path.join(CHAR_DIR, n, "card.json"))]
    else:
        names = TOP20

    if args.dry:
        print(f"计划处理 {len(names)} 位：")
        for n in names:
            by = collect_lines(n)
            total = sum(len(v) for v in by.values())
            print(f"  {n:8s} 台词 {total:5d} 条 · 出场 {len(by):2d} 章")
        return 0

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    llm = LLM(cfg["llm"])
    if not llm.configured:
        print("模型未配置，先到设置里填 API Key")
        return 1

    chapters = load_chapters()
    # 别名表：有些角色的台词署名和显示名不一致（如「牙仙-书信」）
    alias_map: dict[str, list[str]] = {}
    try:
        p = os.path.join(LIB, "data", "characters.json")
        for c in json.load(open(p, encoding="utf-8")):
            ks = [k for k in (c.get("keys") or []) if k and k != c.get("name")]
            if ks:
                alias_map[c["name"]] = ks
    except Exception:
        pass

    lore = LoreStore(CHAR_DIR)
    state_path = os.path.join(DATA_DIR, "distilled.json")
    done = {}
    if os.path.exists(state_path) and not args.force:
        try:
            done = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            done = {}

    ok = skip = fail = 0
    for idx, name in enumerate(names, 1):
        slug = name
        if not os.path.isdir(os.path.join(CHAR_DIR, slug)):
            print(f"[{idx}/{len(names)}] 跳过 {name}：角色不存在")
            skip += 1
            continue
        if name in done and not args.force:
            print(f"[{idx}/{len(names)}] 跳过 {name}：已蒸馏")
            skip += 1
            continue

        t0 = time.time()
        by_ch = collect_lines(name, alias_map.get(name))
        material, used = build_input(name, by_ch, chapters)
        if not material:
            # 小配角没有台词记录，用 persona.md 的档案部分兜底，
            # 至少让她有一条能讲出来的来历，而不是空的。
            material = persona_material(slug)
        if not material:
            print(f"[{idx}/{len(names)}] 跳过 {name}：无任何材料")
            skip += 1
            continue

        try:
            data = distill(llm, name, material)
        except (LLMError, json.JSONDecodeError) as e:
            print(f"[{idx}/{len(names)}] ✗ {name}：{type(e).__name__} {str(e)[:80]}")
            fail += 1
            continue

        tl = [x for x in (data.get("timeline") or []) if isinstance(x, dict) and x.get("event")]
        lr = [x for x in (data.get("lore") or []) if isinstance(x, dict) and x.get("content")]

        added = 0
        for item in tl:
            try:
                lore.upsert(slug, {
                    "title": f"【{item.get('period', '')}】"[:20],
                    "kind": "history",
                    "keywords": [str(k) for k in (item.get("keywords") or [])][:6],
                    "content": str(item["event"])[:400],
                    "always": False, "priority": 6,
                })
                added += 1
            except Exception:
                pass
        for item in lr:
            try:
                lore.upsert(slug, {
                    "title": str(item.get("title") or "")[:20] or "旧事",
                    "kind": "history",
                    "keywords": [str(k) for k in (item.get("keywords") or [])][:6],
                    "content": str(item["content"])[:300],
                    "always": False, "priority": 5,
                })
                added += 1
            except Exception:
                pass

        done[name] = {"chapters": used, "entries": added, "at": time.time()}
        json.dump(done, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[{idx}/{len(names)}] ✓ {name}：{len(used)} 章 → {added} 条经历（{time.time()-t0:.1f}s）")
        ok += 1

    print(f"\n完成：成功 {ok} / 跳过 {skip} / 失败 {fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
