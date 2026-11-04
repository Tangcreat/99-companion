"""抓立绘候选并拼成对照表，供人工挑选。

为什么要拼表：搜索结果里混着官方立绘、宣传横幅、同人图、攻略截图。
尺寸和文件名都不足以判断"这张脸对不对"，只能真的看一眼。
一次看 60 张比逐张看快 60 倍。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "_avatar_candidates")
CURL = r"C:\Windows\System32\curl.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PER_CHAR = 3          # 每个角色留几张候选
THUMB = 150           # 对照表里每格的长边


def curl(url: str, path: str, timeout: int = 20) -> bool:
    r = subprocess.run(
        [CURL, "-sL", "-m", str(timeout), "-A", UA,
         "-H", "Referer: https://cn.bing.com/", "-o", path, url],
        capture_output=True,
    )
    return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 6000


def search(name: str) -> list[str]:
    out, seen = [], set()
    for q in (f"重返未来1999 {name} 立绘",
              f"{name} 重返未来1999 全身立绘 png"):
        url = ("https://cn.bing.com/images/search?q="
               + urllib.parse.quote(q) + "&form=HDRSC2")
        tmp = os.path.join(OUT, "_s.html")
        if not curl(url, tmp):
            continue
        html = open(tmp, encoding="utf-8", errors="ignore").read()
        for u in re.findall(r'murl&quot;:&quot;(.*?)&quot;', html):
            u = u.replace("\\/", "/")
            if u in seen:
                continue
            seen.add(u)
            if u.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp")):
                out.append(u)
    return out


def rank(url: str) -> int:
    s = 0
    dec = urllib.parse.unquote(url)
    if "patchwiki.biligame.com" in url:
        s += 8
    if "立绘" in dec:
        s += 6
    if "thumb" in url:
        s -= 2
    if any(k in url for k in ("9game", "aigei", "duitang", "sinaimg", "hdslb")):
        s -= 4
    return s


def main():
    names = sys.argv[1:]
    if not names:
        print("用法: python tools/grab_portraits.py 37 维尔汀 ...")
        return 1
    os.makedirs(OUT, exist_ok=True)

    from PIL import Image, ImageDraw

    catalog = {}
    for name in names:
        d = os.path.join(OUT, name)
        os.makedirs(d, exist_ok=True)
        urls = sorted(set(search(name)), key=lambda u: -rank(u))
        got = []
        for i, u in enumerate(urls):
            if len(got) >= PER_CHAR:
                break
            fp = os.path.join(d, f"c{len(got)}.img")
            if not curl(u, fp):
                continue
            try:
                im = Image.open(fp)
                im.load()
            except Exception:
                continue
            if im.width < 300 or im.height < 300:
                continue
            ext = ".png" if im.format == "PNG" else ".jpg"
            final = os.path.join(d, f"c{len(got)}{ext}")
            im.save(final)
            os.remove(fp)
            got.append({"file": os.path.basename(final),
                        "w": im.width, "h": im.height,
                        "alpha": im.mode in ("RGBA", "LA"),
                        "src": u})
        catalog[name] = got
        print(f"  {name}: {len(got)} 张")
        for g in got:
            print(f"      {g['file']:8s} {g['w']}x{g['h']} alpha={g['alpha']}")

    json.dump(catalog, open(os.path.join(OUT, "_catalog.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 拼对照表
    cols = PER_CHAR
    rows = len(names)
    cw, ch = THUMB + 8, THUMB + 26
    sheet = Image.new("RGB", (cols * cw + 60, rows * ch + 10), (250, 250, 252))
    draw = ImageDraw.Draw(sheet)
    for r, name in enumerate(names):
        y = 6 + r * ch
        draw.text((6, y + THUMB // 2), name, fill=(20, 20, 30))
        for c, g in enumerate(catalog.get(name) or []):
            p = os.path.join(OUT, name, g["file"])
            try:
                im = Image.open(p).convert("RGBA")
            except Exception:
                continue
            im.thumbnail((THUMB, THUMB))
            bg = Image.new("RGBA", (THUMB, THUMB), (255, 255, 255, 255))
            bg.alpha_composite(im, ((THUMB - im.width) // 2, (THUMB - im.height) // 2))
            x = 60 + c * cw + 4
            sheet.paste(bg.convert("RGB"), (x, y + 18))
            draw.rectangle([x, y + 18, x + THUMB, y + 18 + THUMB], outline=(210, 212, 220))
            draw.text((x + 2, y + 3), f"{c} {g['w']}x{g['h']}", fill=(110, 112, 125))
    path = os.path.join(OUT, "_sheet.png")
    sheet.save(path)
    print(f"\n对照表：{path}  ({sheet.width}x{sheet.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
