"""抓角色官方立绘，裁成头像。

绕了三条路才通：
  huijiwiki  → 403（有反爬挑战）
  biligame 页面 → JS 渲染，静态抓不到图
  必应图片   → 能用，而且能搜到 patchwiki 上的官方立绘
               文件名形如「立绘_37_03.png」，可以按名字精确过滤

立绘是全身像，直接压成正方形会把头切掉，所以要按"头部区域"裁。
"""
from __future__ import annotations

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


def curl_get(url: str, out: str, timeout: int = 25) -> bool:
    r = subprocess.run(
        [CURL, "-sL", "-m", str(timeout), "-A", UA,
         "-H", "Referer: https://cn.bing.com/", "-o", out, url],
        capture_output=True,
    )
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 5000


def search(name: str, extra: str = "立绘") -> list[str]:
    q = f"重返未来1999 {name} {extra}"
    url = ("https://cn.bing.com/images/search?q="
           + urllib.parse.quote(q) + "&form=HDRSC2")
    tmp = os.path.join(OUT, "_search.html")
    if not curl_get(url, tmp):
        return []
    html = open(tmp, encoding="utf-8", errors="ignore").read()
    raws = re.findall(r'murl&quot;:&quot;(.*?)&quot;', html)
    out, seen = [], set()
    for u in raws:
        u = u.replace("\\/", "/")
        if u in seen:
            continue
        seen.add(u)
        if u.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp")):
            out.append(u)
    return out


def score(url: str, name: str) -> int:
    """越像官方立绘分越高。"""
    s = 0
    if "patchwiki.biligame.com" in url:
        s += 10
    if "立绘" in url:
        s += 20
    if f"立绘_{name}" in urllib.parse.unquote(url):
        s += 40
    if "头像" in urllib.parse.unquote(url):
        s += 25
    if "thumb" in url:
        s -= 3          # 缩略图，优先原图
    if any(k in url for k in ("media.9game", "aigei", "duitang", "sinaimg")):
        s -= 8          # 搬运站，常有水印
    return s


def upsize(url: str) -> str:
    """wiki 缩略图路径里的 /700px- 可以换成更大的尺寸。"""
    return re.sub(r"/(\d{2,4})px-", "/1200px-", url)


def crop_head(img, size: int = 256):
    """立绘是全身像，取上部的头肩区域，而不是几何中心。"""
    from PIL import Image
    w, h = img.size
    # 头部大约占身高的上部 1/3；取正方形，宽度受限时以宽度为准
    side = min(w, int(h * 0.42))
    left = (w - side) // 2
    top = int(h * 0.02)
    box = (left, top, left + side, top + side)
    return img.crop(box).resize((size, size), Image.LANCZOS)


def main():
    names = sys.argv[1:]
    if not names:
        print("用法: python tools/fetch_avatars.py 37 维尔汀 ...")
        return 1
    os.makedirs(OUT, exist_ok=True)

    from PIL import Image

    report = []
    for name in names:
        d = os.path.join(OUT, name)
        os.makedirs(d, exist_ok=True)
        urls = search(name)
        if not urls:
            report.append((name, 0, "没搜到"))
            print(f"  {name}: 没搜到")
            continue

        ranked = sorted(urls, key=lambda u: -score(u, name))
        picked = None
        for i, u in enumerate(ranked[:4]):
            for cand in (upsize(u), u):
                fp = os.path.join(d, f"try{i}.bin")
                if not curl_get(cand, fp):
                    continue
                try:
                    im = Image.open(fp)
                    im.load()
                except Exception:
                    continue
                if im.width < 220 or im.height < 220:
                    continue
                picked = (im, cand)
                break
            if picked:
                break

        if not picked:
            report.append((name, 0, "下载或解码失败"))
            print(f"  {name}: 下载失败")
            continue

        im, src = picked
        rgb = im.convert("RGBA")
        av = crop_head(rgb)
        av.save(os.path.join(d, "avatar.png"))
        report.append((name, 1, f"{im.width}x{im.height} <- {src[:70]}"))
        print(f"  {name}: {im.width}x{im.height} -> avatar.png")

    print(f"\n完成 {sum(1 for r in report if r[1])}/{len(names)}")
    json.dump([{"name": n, "ok": ok, "note": note} for n, ok, note in report],
              open(os.path.join(OUT, "_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
