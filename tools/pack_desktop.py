"""打 Windows 分发包：清空密钥、排除运行数据、压缩。

关键点：config.json 的密钥是在**内存里**替换的（writestr），
不会去改磁盘上那份 —— 之前用「备份-清空-压缩-恢复」的写法，
一旦进程被中断就会把用户本机的 Key 清掉。
"""
from __future__ import annotations

import json
import os
import time
import zipfile

SRC = os.path.abspath(r"dist\99peiban")
OUT = os.path.abspath(r"dist\99peiban-Desktop-v1.7.zip")

SKIP_DIR = {"browser-profile", "uploads", "sticker_thumbs", "__pycache__"}
SKIP_FILE = {
    "companion.db", "companion.db-wal", "companion.db-shm",
    "companion.log", "desktop.log", ".traits_version",
    "me.json", "me.png", "config.json.bak", "crash.log",
}


def main() -> None:
    if not os.path.isdir(SRC):
        raise SystemExit(f"找不到目录：{SRC}")
    if os.path.exists(OUT):
        os.remove(OUT)

    t0 = time.time()
    n = 0
    used_key = False

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR]
            for f in files:
                if f in SKIP_FILE:
                    continue
                full = os.path.join(root, f)
                arc = os.path.join("99peiban", os.path.relpath(full, SRC))

                if f == "config.json":
                    data = json.load(open(full, encoding="utf-8"))
                    if (data.get("llm", {}).get("api_key") or "").strip():
                        used_key = True
                    data.setdefault("llm", {})["api_key"] = ""
                    z.writestr(arc, json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    z.write(full, arc)
                n += 1

    size = os.path.getsize(OUT) / 1048576
    print(f"  {n} 个文件 -> {OUT}")
    print(f"  {size:.1f} MB，耗时 {time.time()-t0:.0f}s")
    print(f"  包内 config.json 的密钥已清空（本机那份{'原本有 Key，未改动' if used_key else '本来就没有 Key'}）")

    # 校验：包里确实没有密钥、没有数据库
    with zipfile.ZipFile(OUT) as z:
        cfg = json.loads(z.read("99peiban/config.json").decode("utf-8"))
        print("  校验 api_key 为空:", not (cfg["llm"].get("api_key") or "").strip())
        names = z.namelist()
        print("  校验无 companion.db:", not any("companion.db" in x for x in names))
        print("  校验无 browser-profile:", not any("browser-profile" in x for x in names))
        print("  含 story.json:", any("data/story.json" in x for x in names))
        print("  角色卡数量:", len([x for x in names if x.startswith("99peiban/characters/") and x.endswith("card.json")]))


if __name__ == "__main__":
    main()
