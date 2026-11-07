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
APP_VERSION = "1.7"
# 桌面版没有 Android 的 BuildConfig，版本号从这里进诊断页
os.environ.setdefault("STARLIT_APP_VERSION", APP_VERSION)
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
        # 放到系统的应用数据目录，不要放 data/。
        # 放 data/ 的后果：Edge 会往里写几十 MB 缓存（component_crx_cache、
        # onnx 模型），一旦把整个文件夹打包分发，压缩包会凭空胖一倍。
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        profile = os.path.join(root, "99peiban", "browser-profile")
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


def service_healthy(port: int, timeout: float = 1.2) -> bool:
    """端口上真有我们的服务在响应吗。

    只看端口能不能连是不够的：进程被强杀后，Windows 上偶尔会留下
    处于 LISTENING 的僵尸 socket，connect 能成功但没人 accept。
    原逻辑据此认为「已经有一份在运行」，于是只开窗口不启动服务 —— 用户看到白屏。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/characters", timeout=timeout) as r:
            return r.getcode() == 200
    except Exception:
        return False


def port_free(port: int) -> bool:
    """能否在本机回环上 bind。不设 SO_REUSEADDR —— Windows 上那会让已占用的端口也假成功。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def resolve_port(preferred: int) -> tuple[int, bool]:
    """返回 (实际端口, 是否已有健康实例)。僵尸监听会被跳过，向后找空位。"""
    if service_healthy(preferred):
        return preferred, True
    if port_free(preferred):
        return preferred, False
    for offset in range(1, 8):
        p = preferred + offset
        if service_healthy(p):
            return p, True
        if port_free(p):
            return p, False
    return preferred, False


def main():
    ensure_layout()

    global PORT
    port, running = resolve_port(PORT)
    if port != PORT:
        print(f"端口 {PORT} 被占用且无服务响应，改用 {port}")
        PORT = port
        os.environ["STARLIT_PORT"] = str(port)
    if running:
        print(f"已有实例在 {PORT} 上运行，直接开窗口。")
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
