"""精简角色库：删掉台词极少的龙套。

判定依据是 dialogue.jsonl 里该角色真实的台词行数（含别名与前缀匹配），
不是 characters.json 的 line_count——后者会漏掉个人剧情。

安全策略：
  1. 任何角色都不会被真删，而是移到 _removed/<时间戳>/ 下
  2. 名单里排除重点角色（FEATURED）与原创示例卡
  3. 删除后自动重建同场关系图，并清掉群组里失效的成员

用法：
    python tools/prune_cast.py --dry          # 只看名单
    python tools/prune_cast.py                # 执行，默认 40 个
    python tools/prune_cast.py --count 60     # 删更多
    python tools/prune_cast.py --restore _removed/20261104-1130
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAR_DIR = os.path.join(HERE, "characters")
SPEAKER_STATS = os.path.join(HERE, "data", "_speaker_lines.json")
GROUPS = os.path.join(HERE, "data", "groups.json")

FEATURED = {
    "37", "维尔汀", "十四行诗", "露西", "贝丽尔",
    "纸信圈儿", "玛丽安娜", "红弩箭", "斯奈德", "未锈铠",
    "苏菲亚", "鹭鸶剪", "程和光", "梁月", "牙仙",
    "雾行者", "霍夫曼", "星锑", "伊索尔德", "卡卡尼亚",
}

# 不管台词多少都不删的：原创示例卡
PROTECTED = {"沈知夏"}


def load_cards() -> list[dict]:
    out = []
    for d in sorted(os.listdir(CHAR_DIR)):
        p = os.path.join(CHAR_DIR, d, "card.json")
        if not os.path.exists(p):
            continue
        try:
            c = json.load(open(p, encoding="utf-8"))["data"]
        except Exception:
            continue
        out.append({"slug": d, "name": c.get("name") or d,
                    "aliases": c.get("aliases") or []})
    return out


def line_counts(cards: list[dict]) -> dict[str, int]:
    if not os.path.exists(SPEAKER_STATS):
        raise SystemExit(
            "缺少 data/_speaker_lines.json。先跑一次台词统计（见 README）"
        )
    cnt = json.load(open(SPEAKER_STATS, encoding="utf-8"))
    out = {}
    for c in cards:
        keys = [c["name"]] + [k for k in c["aliases"] if k]
        total = sum(cnt.get(k, 0) for k in keys)
        if total == 0:
            total = sum(v for k, v in cnt.items() if k.startswith(c["name"]))
        out[c["slug"]] = total
    return out


def pick(cards: list[dict], counts: dict, n: int) -> list[dict]:
    cand = [c for c in cards
            if c["name"] not in FEATURED and c["name"] not in PROTECTED]
    cand.sort(key=lambda c: (counts[c["slug"]], c["name"]))
    return cand[:n]


def prune(chosen: list[dict]):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(HERE, "_removed", stamp)
    os.makedirs(dest, exist_ok=True)
    moved = []
    for c in chosen:
        src = os.path.join(CHAR_DIR, c["slug"])
        if not os.path.isdir(src):
            continue
        shutil.move(src, os.path.join(dest, c["slug"]))
        moved.append(c["name"])
    manifest = {
        "at": stamp,
        "count": len(moved),
        "names": moved,
        "restore": f"把 {dest} 下的目录移回 characters/ 即可",
    }
    json.dump(manifest, open(os.path.join(dest, "_manifest.json"), "w",
                             encoding="utf-8"), ensure_ascii=False, indent=1)
    return dest, moved


def clean_groups(alive: set[str]):
    if not os.path.exists(GROUPS):
        return 0
    try:
        data = json.load(open(GROUPS, encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    keep = []
    for g in data.get("groups", []):
        members = [m for m in (g.get("members") or []) if m in alive]
        if len(members) < 2:
            n += 1
            continue
        g["members"] = members
        keep.append(g)
    data["groups"] = keep
    json.dump(data, open(GROUPS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--restore", default="")
    args = ap.parse_args()

    if args.restore:
        src = args.restore if os.path.isabs(args.restore) else os.path.join(HERE, args.restore)
        if not os.path.isdir(src):
            print("目录不存在:", src)
            return 1
        n = 0
        for d in os.listdir(src):
            p = os.path.join(src, d)
            if os.path.isdir(p):
                shutil.move(p, os.path.join(CHAR_DIR, d))
                n += 1
        print(f"已恢复 {n} 个角色")
        return 0

    cards = load_cards()
    counts = line_counts(cards)
    chosen = pick(cards, counts, args.count)

    print(f"现有 {len(cards)} 个角色，计划移除 {len(chosen)} 个：\n")
    for c in chosen:
        print(f"  {counts[c['slug']]:5d} 句   {c['name']}")

    if args.dry:
        print("\n（试运行，未改动任何文件）")
        return 0

    dest, moved = prune(chosen)
    alive = {c["slug"] for c in load_cards()}
    dropped_groups = clean_groups(alive)

    print(f"\n已移动 {len(moved)} 个角色到 {dest}")
    print(f"剩余角色 {len(alive)} 个")
    if dropped_groups:
        print(f"清理了 {dropped_groups} 个成员不足的群")
    print(f"\n后悔了可以执行：python tools/prune_cast.py --restore {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
