#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只重建 data/cooccur.json（同场关系图），不动角色卡。

导入器是增量跑的，cooccur 每次会被覆盖；这个脚本直接扫全部
角色档案的「同场角色（按共同出场关卡数）」表重建，幂等。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools.import_1999 import LIB, DATA_DIR, front_matter, parse_cooccur  # noqa: E402


def main():
    char_dir = os.path.join(LIB, "characters")
    files = sorted(f for f in os.listdir(char_dir) if f.endswith(".md"))
    out = {}
    for fn in files:
        path = os.path.join(char_dir, fn)
        fm = front_matter(path)
        name = fm.get("name") or fn.split("_", 1)[-1].replace(".md", "")
        pairs = parse_cooccur(path)
        # 过滤掉明显不是人名的（纯数字、单字、超长）
        clean = [(n, c) for n, c in pairs
                 if n and not n.isdigit() and 1 < len(n) <= 12 and c > 0]
        if clean:
            out[name] = clean[:14]

    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, "cooccur.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total_edges = sum(len(v) for v in out.values())
    print(f"重建完成：{len(out)} 人有同场记录，共 {total_edges} 条关系边")

    rank = sorted(out.items(), key=lambda kv: -sum(c for _, c in kv[1]))[:5]
    print("关系最密的几位：")
    for k, v in rank:
        print(f"  {k}（{sum(c for _, c in v)} 场）-> {', '.join(f'{n}({c})' for n, c in v[:4])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
