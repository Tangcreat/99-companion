#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把后端代码与前端资源同步进 Android 工程。

Android 侧的分工：
    app/src/main/python/     ← Chaquopy 打包成 Python 源码（server.py + core/）
    app/src/main/assets/     ← 运行时首次启动复制到 filesDir（web / 角色 / 表情）

改了 starlit-clone 里的任何后端代码或前端页面，跑一次这个脚本再编译。
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AND = os.path.join(ROOT, "android", "app", "src", "main")
PY_DIR = os.path.join(AND, "python")
ASSETS = os.path.join(AND, "assets")


def clean_copy(src: str, dst: str, skip: tuple[str, ...] = ()):
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*skip))


def main():
    os.makedirs(PY_DIR, exist_ok=True)
    os.makedirs(ASSETS, exist_ok=True)

    # 1) Python 后端
    clean_copy(os.path.join(ROOT, "core"), os.path.join(PY_DIR, "core"),
               skip=("__pycache__", "*.pyc"))
    for fn in ("server.py", "android_entry.py"):
        shutil.copyfile(os.path.join(ROOT, fn), os.path.join(PY_DIR, fn))
    print(f"  后端: core/ + server.py + android_entry.py -> {PY_DIR}")

    # 2) 前端与数据（首次启动会复制到 filesDir）
    clean_copy(os.path.join(ROOT, "web"), os.path.join(ASSETS, "web"))
    print("  前端: web/")

    for sub in ("characters", "stickers"):
        src = os.path.join(ROOT, sub)
        if os.path.isdir(src):
            clean_copy(src, os.path.join(ASSETS, sub))
            n = sum(len(f) for _, _, f in os.walk(os.path.join(ASSETS, sub)))
            print(f"  数据: {sub}/（{n} 个文件）")

    # 只挑需要的几个文件，不要整个 data/（里面有聊天库和密钥）
    dst = os.path.join(ASSETS, "data")
    os.makedirs(dst, exist_ok=True)
    for fn, label in (("world.json", "共用世界观"), ("story.json", "剧情回顾")):
        src_f = os.path.join(ROOT, "data", fn)
        if os.path.exists(src_f):
            shutil.copyfile(src_f, os.path.join(dst, fn))
            print(f"  数据: data/{fn}（{label}）")

    # 3) 顺带统计
    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(ASSETS) for f in fs)
    print(f"\nassets 合计 {total/1048576:.1f} MB")
    print("接下来：cd android && gradle assembleDebug")


if __name__ == "__main__":
    sys.exit(main())
