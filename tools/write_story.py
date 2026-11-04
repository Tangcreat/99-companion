"""把主线剧情改写成小说体的「剧情回顾」。

素材是故事库的 chapters.json：每章有官方梗概、情节段落、关键转折、主要角色。
模型要做的不是摘要，是"用小说笔法重讲一遍"——有场景、有动作、有对话的转述。

用法：
    python tools/write_story.py --dry         # 只看要处理哪几章
    python tools/write_story.py               # 全部（已写的跳过）
    python tools/write_story.py --force       # 重写
    python tools/write_story.py --only 7TH
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from core.llm import LLM, LLMError         # noqa: E402

LIB = r"C:\Users\17807\Desktop\重返未来1999_故事库"
CHAPTERS = os.path.join(LIB, "data", "chapters.json")
OUT = os.path.join(HERE, "data", "story.json")
CONFIG = os.path.join(HERE, "config.json")

SYSTEM = """你在为一款叙事游戏写「剧情回顾」——用小说笔法把这一章重讲一遍。

硬性要求：

- 第三人称，小说体。有场景、有动作、有对话的转述，不是剧情摘要。
- 1200~1600 字，8~14 个自然段，段与段之间空一行。
- **禁止**出现「本章」「这一章」「剧情」「玩家」「主角们」「综上所述」「首先/其次/最后」这类词。
- 不要写章节标题，不要写「第X章」，直接从第一个场景开始。
- 保留原作的年代与地点氛围，写具体的物、天气、声音、气味。
- 只写这一章发生的事，**不要剧透后面的章节**。
- 人物说话要贴各自的语气，不要所有人都一个腔调。
- 结尾停在这一章的收束处，可以留一点余味，但不要写"欲知后事如何"。

反例（不要这样写）：
  「本章讲述了维尔汀一行人来到芝加哥，遇到了……」

正例：
  「雨在午夜停了。芝加哥的街灯还亮着，湿漉漉的石板路把光揉碎成一片一片……」"""


def load_chapters() -> list[dict]:
    data = json.load(open(CHAPTERS, encoding="utf-8"))
    data.sort(key=lambda c: c.get("order", 99))
    return data


def build_prompt(c: dict) -> str:
    s = c.get("summary") or {}
    plot = s.get("plot") or []
    turns = s.get("turns") or []
    roles = s.get("roles") or []
    parts = [
        f"章节：{c.get('code')} · {c.get('title')}",
        f"时代与地点：{c.get('year') or '（未标注）'}",
    ]
    if c.get("intro"):
        parts.append(f"官方简介：{c['intro']}")
    if plot:
        parts.append("情节脉络：\n" + "\n".join(f"- {p}" for p in plot))
    if turns:
        parts.append("关键转折：\n" + "\n".join(f"- {t}" for t in turns))
    if roles:
        parts.append("主要角色：\n" + "\n".join(f"- {r[0]}：{r[1]}" for r in roles if len(r) >= 2))
    parts.append("现在用小说笔法把这一章写出来。")
    return "\n\n".join(parts)


def clean(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[\w]*\n?|```$", "", t, flags=re.M).strip()
    # 去掉模型自作主张加的一级标题
    t = re.sub(r"^#+\s*.*\n+", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    chapters = load_chapters()
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        chapters = [c for c in chapters if str(c.get("code", "")).upper() in wanted]

    store = {"chapters": []}
    if os.path.exists(OUT):
        try:
            store = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    done = {c["code"]: c for c in store.get("chapters", [])}
    if args.force:
        done = {}

    todo = [c for c in chapters if c.get("code") not in done]
    print(f"共 {len(chapters)} 章，待写 {len(todo)} 章")
    if args.dry:
        for c in todo:
            print(f"  {c.get('code'):6s} {c.get('title')}  ({c.get('year')})")
        return 0
    if not todo:
        print("都写完了。要重写加 --force")
        return 0

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    llm = LLM(cfg["llm"])
    if not llm.configured:
        print("模型未配置")
        return 1

    t0 = time.time()
    for i, c in enumerate(todo, 1):
        code = c.get("code")
        print(f"[{i}/{len(todo)}] {code} {c.get('title')} ...", end="", flush=True)
        try:
            raw = llm.chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": build_prompt(c)}],
                temperature=0.75, max_tokens=2600,
            )
        except LLMError as e:
            print(f" ✗ {str(e)[:60]}")
            continue
        body = clean(raw)
        if len(body) < 500:
            print(f" ✗ 太短（{len(body)} 字）")
            continue
        done[code] = {
            "code": code,
            "order": c.get("order"),
            "title": c.get("title"),
            "title_en": c.get("title_en"),
            "year": c.get("year"),
            "chars": len(body),
            "body": body,
        }
        store = {"chapters": sorted(done.values(), key=lambda x: x.get("order", 99))}
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(store, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f" ✓ {len(body)} 字")

    total = sum(c.get("chars", 0) for c in store.get("chapters", []))
    print(f"\n完成 {len(store.get('chapters', []))} 章，共 {total} 字，耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
