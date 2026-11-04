"""批量导入真实立绘当头像。

用法：
    python tools/apply_avatars.py D:\\立绘

目录里的文件按角色名命名即可，扩展名随意（png/jpg/webp）：
    37.png
    维尔汀.jpg
    红弩箭.png

会自动居中裁成正方形、缩到 256px、存成的角色目录下的 avatar.png。
裁切默认取「上部」——立绘是全身像，几何中心会切到腰。
需要居中裁的加 --center。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PIL import Image                     # noqa: E402
from core.cards import CardStore          # noqa: E402

CHAR_DIR = os.path.join(HERE, "characters")
SIZE = 256
EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def crop_top(img, size: int):
    w, h = img.size
    side = min(w, int(h * 0.45))
    left = (w - side) // 2
    return img.crop((left, 0, left + side, side)).resize((size, size), Image.LANCZOS)


def crop_center(img, size: int):
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    center = "--center" in sys.argv
    if not args:
        print("用法: python tools/apply_avatars.py <图片所在目录> [--center]")
        return 1
    src = args[0]
    if not os.path.isdir(src):
        print("目录不存在:", src)
        return 1

    store = CardStore(CHAR_DIR)
    by_name = {c.name: c for c in store.list()}

    files = {}
    for fn in os.listdir(src):
        base, ext = os.path.splitext(fn)
        if ext.lower() in EXTS:
            files[base.strip()] = os.path.join(src, fn)

    ok, miss = 0, []
    for name, path in files.items():
        card = by_name.get(name)
        if not card:
            miss.append(name)
            continue
        try:
            im = Image.open(path).convert("RGBA")
        except Exception as e:
            print(f"  跳过 {name}：{e}")
            continue
        out = crop_center(im, SIZE) if center else crop_top(im, SIZE)
        out.save(os.path.join(store.dir_of(card.slug), "avatar.png"), "PNG")
        ok += 1
        print(f"  ✓ {name}")

    print(f"\n已应用 {ok} 张")
    if miss:
        print("没有对应角色：" + "、".join(miss[:10]) + ("…" if len(miss) > 10 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
