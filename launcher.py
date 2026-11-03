#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面版入口。

做的事很少：起本地服务 → 等它就绪 → 用 Edge/Chrome 的 --app 模式开一个
无地址栏窗口。这样既有桌面 App 的外观，又不用背 Electron 那一百多兆。

打包（PyInstaller）和直接 `python launcher.py` 都能跑。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

APP_NAME = "99陪伴"
PORT = int(os.environ.get("STARLIT_PORT") or 8787)


def base_dir() -> str:
    """可写数据所在目录（exe 旁边）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def wait_ready(timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/characters", timeout=1.5):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def find_browser() -> str | None:
    """找 Edge / Chrome 的可执行文件。"""
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def open_window():
    url = f"http://127.0.0.1:{PORT}/"
    exe = find_browser()
    if exe:
        profile = os.path.join(base_dir(), "data", "browser-profile")
        os.makedirs(profile, exist_ok=True)
        try:
            subprocess.Popen([
                exe,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--window-size=1380,880",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,PrivacySandboxSettings4",
            ], close_fds=True)
            return
        except Exception:
            pass
    # 兜底：默认浏览器
    import webbrowser
    webbrowser.open(url)


def ensure_layout():
    root = base_dir()
    for sub in ("characters", "data", "stickers", "web"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)


def main():
    ensure_layout()

    # 端口被占：说明已有一份在跑，直接开窗口
    s = socket.socket()
    busy = s.connect_ex(("127.0.0.1", PORT)) == 0
    s.close()
    if busy:
        open_window()
        return 0

    def boot_window():
        if wait_ready():
            open_window()
            print(f"{APP_NAME} 已就绪： http://127.0.0.1:{PORT}")
        else:
            print("服务启动超时，请检查端口是否被占用。")

    threading.Thread(target=boot_window, daemon=True).start()

    # 服务跑在主线程——进程生命周期就等于服务生命周期，
    # 不靠空循环保活（那样窗口一关进程就退）。
    import server as srv
    srv.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
