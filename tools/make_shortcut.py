"""生成 Windows 快捷方式（.lnk）。

踩过的两个坑，都写在这里免得重复：

1. 不要手写 .lnk 二进制。只写 Header + LinkInfo 的版本资源管理器认不出来，
   双击没反应。Windows 需要 LinkTargetIDList（Shell Item ID 列表）。
2. **目标路径不能含中文**。`pylnk3` 构建 IDList 时遇到非 ASCII 目录名会截断，
   例如 `...\\dist\\99陪伴-桌面版-v1.6\\99陪伴.exe` 会只剩 `...\\dist`，
   双击就变成打开文件夹。所以程序目录与 exe 都应该是纯英文。

另外本机安全策略会拦 `cscript` 与 `WScript.Shell` 这类创建方式，
所以这里用纯 Python 的 pylnk3。

用法：
    python tools/make_shortcut.py <目标exe> <快捷方式路径>
"""
from __future__ import annotations

import os
import sys


def _has_non_ascii(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def make_shortcut(target: str, lnk_path: str, description: str = "") -> str:
    try:
        import pylnk3
    except ImportError:
        raise SystemExit("缺少依赖：pip install pylnk3")

    target = os.path.abspath(target)
    if not os.path.exists(target):
        raise FileNotFoundError(target)

    if _has_non_ascii(target):
        raise SystemExit(
            "目标路径含非 ASCII 字符，pylnk3 会截断 IDList、快捷方式会失效。\n"
            f"  当前：{target}\n"
            "  请把程序目录与 exe 改成英文名后再生成（例如 dist\\99peiban-desktop\\99peiban.exe）。"
        )

    if os.path.exists(lnk_path):
        os.remove(lnk_path)

    pylnk3.for_file(
        target, lnk_path,
        description=description,
        icon_file=target, icon_index=0,
        work_dir=os.path.dirname(target),
        window_mode="Normal",
    )

    # 回读校验：路径必须完整回放，否则等于没生成
    parsed = pylnk3.parse(lnk_path)
    if (parsed.path or "").lower() != target.lower():
        raise SystemExit(f"生成后回读不一致：写入 {target}，回读 {parsed.path}")
    return lnk_path


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    out = make_shortcut(sys.argv[1], sys.argv[2], description="99陪伴")
    print("created:", out, os.path.getsize(out), "bytes")
