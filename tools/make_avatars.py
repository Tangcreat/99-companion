"""为角色生成统一风格的印章式头像。

为什么不是直接抓立绘：试过五个源都对不上——
  huijiwiki 403、biligame 是 JS 渲染、Fandom 连不通、百度封爬、
  必应图片里混着宣传海报和他游角色（20 个只捞到 4 张能用的）。
与其塞一堆带水印的海报，不如先给一套风格统一、每个角色都不同的。

设计：角色主题色渐变底 + 由名字哈希决定的几何纹样 + 姓氏/首字。
同一套规则生成所有人，所以放在一起看是一套，不是一堆占位图。
"""
from __future__ import annotations

import colorsys
import hashlib
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PIL import Image, ImageDraw, ImageFont      # noqa: E402

CHAR_DIR = os.path.join(HERE, "characters")
SIZE = 256

FONTS = [
    r"C:\Windows\Fonts\STZHONGS.TTF",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def hex2rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#8fa0bd").lstrip("#")
    if len(h) != 6:
        h = "8fa0bd"
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def shift(rgb, dl: float, ds: float = 0.0) -> tuple[int, int, int]:
    """调明度/饱和度，用来从主题色派生深浅两档。"""
    r, g, b = [v / 255 for v in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + dl))
    s = max(0.0, min(1.0, s + ds))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def load_font(size: int):
    for p in FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gradient(size: int, c1, c2, angle: float):
    """按角度画线性渐变。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    for y in range(size):
        for x in range(size):
            t = (x / size) * dx + (y / size) * dy
            t = (t + 1) / 2
            px[x, y] = (
                int(c1[0] + (c2[0] - c1[0]) * t),
                int(c1[1] + (c2[1] - c1[1]) * t),
                int(c1[2] + (c2[2] - c1[2]) * t),
            )
    return img


def pattern(size: int, seed: int, color, style: int) -> Image.Image:
    """由名字哈希决定的纹样，四个角色有四种样子。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = color + (38,)
    if style == 0:      # 同心弧
        cx, cy = size * 0.5, size * 1.18
        for i in range(6):
            r = size * (0.42 + i * 0.11)
            d.arc([cx - r, cy - r, cx + r, cy + r], 200, 340, fill=c, width=3)
    elif style == 1:    # 斜线
        step = max(10, size // 16)
        for i in range(-size, size * 2, step):
            d.line([(i, 0), (i + size, size)], fill=c, width=2)
    elif style == 2:    # 散点
        for i in range(26):
            h = hashlib.md5(f"{seed}:{i}".encode()).digest()
            x = (h[0] * 256 + h[1]) % size
            y = (h[2] * 256 + h[3]) % size
            r = 2 + (h[4] % 5)
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
    else:               # 圆环
        cx, cy = size / 2, size / 2
        r = size * 0.34
        d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=c, width=3)
        d.arc([cx - r * 0.72, cy - r * 0.72, cx + r * 0.72, cy + r * 0.72],
              40, 300, fill=c, width=1)
    return layer


def make(name: str, accent: str, out_path: str):
    seed = hashlib.md5(name.encode("utf-8")).hexdigest()
    h = int(seed[:6], 16)
    style = h % 4
    angle = (h >> 4) % 90 + 100

    base = hex2rgb(accent)
    c_deep = shift(base, -0.20, 0.30)
    c_soft = shift(base, 0.12, 0.10)

    img = gradient(SIZE, c_deep, c_soft, angle).convert("RGBA")
    img.alpha_composite(pattern(SIZE, h, shift(base, 0.30, -0.1), style))

    # 外圈细边
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, SIZE - 8, SIZE - 8], outline=(255, 255, 255, 56), width=3)

    # 中文取首字；纯拉丁/数字取前两位，否则「37」只会显示「3」
    nm = (name or "?").strip()
    if any("\u4e00" <= ch <= "\u9fff" for ch in nm):
        label = next(ch for ch in nm if "\u4e00" <= ch <= "\u9fff")
    else:
        label = nm[:2]
    fs = int(SIZE * (0.46 if len(label) == 1 else 0.34))
    font = load_font(fs)
    bbox = d.textbbox((0, 0), label, font=font)
    w, ht = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (SIZE - w) / 2 - bbox[0]
    y = (SIZE - ht) / 2 - bbox[1]
    d.text((x + 3, y + 4), label, font=font, fill=(0, 0, 0, 52))
    d.text((x, y), label, font=font, fill=(255, 255, 255, 245))

    img.convert("RGB").save(out_path, "PNG")


def main():
    from core.cards import CardStore
    store = CardStore(CHAR_DIR)
    only = sys.argv[1:] or None
    n = 0
    for c in store.list():
        if only and c.name not in only:
            continue
        d = store.dir_of(c.slug)
        make(c.name, c.accent, os.path.join(d, "avatar.png"))
        n += 1
    print(f"生成 {n} 个头像")


if __name__ == "__main__":
    main()
