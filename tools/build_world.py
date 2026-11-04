"""把故事库的术语表转成「共用世界观」。

为什么单独抽出来：162 个角色同处一个故事体系，暴雨、基金会、重塑之手这些
常识如果写进每个角色的设定集，等于重复 162 遍，还会被各自的往事挤掉。
放一份共用的，谁都查得到。

用法：
    python tools/build_world.py
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIB = r"C:\Users\17807\Desktop\重返未来1999_故事库"
OUT = os.path.join(HERE, "data", "world.json")

# 词条别名里常见的分隔符
SPLIT_RE = re.compile(r"\s*[/／、,，]\s*")

# 玩家口语里的简称，光靠表格里的全称匹配不到
EXTRA_ALIASES = {
    "拉普拉斯科算中心": ["拉普拉斯", "科算中心"],
    "圣洛夫基金会": ["基金会", "圣洛夫"],
    "芝诺军备学院": ["芝诺"],
    "第一防线学校": ["第一防线"],
    "联合委员会": ["委员会"],
    "非对称免疫蛋白 G": ["免疫蛋白"],
    "阿派朗岛": ["阿派朗"],
}


def parse_glossary(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    entries = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        term, desc = cells[0], cells[1]
        if not term or term in ("词", "---") or set(term) <= {"-", ":"}:
            continue
        if not desc or set(desc) <= {"-", ":"}:
            continue
        aliases = [x for x in SPLIT_RE.split(term) if x and x != term]
        title = SPLIT_RE.split(term)[0]
        for k, extra in EXTRA_ALIASES.items():
            if k in term or term in k:
                aliases = aliases + [x for x in extra if x not in aliases]
        entries.append({
            "title": title[:12],
            "kind": "world",
            "keywords": ([term] + aliases)[:5],
            "content": desc,
            "always": False,
            "priority": 4,
        })
    return entries


def main():
    lib = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIB
    path = os.path.join(lib, "world", "07_术语表.md")
    entries = parse_glossary(path)
    if not entries:
        print(f"没解析到词条，检查路径：{path}")
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, ensure_ascii=False, indent=1)
    print(f"已写入 {len(entries)} 条共用世界观 -> {OUT}")
    for e in entries[:6]:
        print(f"  {e['title']:10s} {e['keywords']}  {e['content'][:34]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
