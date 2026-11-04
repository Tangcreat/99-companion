"""统一的「现在几点」。

为什么要有这一层：
1. Android 上 Chaquopy 的 Python 不一定拿得到设备时区，可能按 UTC 算。
   所以由 Kotlin 侧把 UTC 偏移传进来，这里以它为准。
2. 时区一旦漂了，角色会把中午说成凌晨——这是对外表现最明显的一类错误，
   不能指望每台设备、每个运行时都猜对。

不传偏移时退回系统本地时间（桌面版就走这条路）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 设备时区相对 UTC 的偏移（分钟）。None 表示用系统本地时区。
_OFFSET_MIN: int | None = None
_SOURCE = "system"

MAX_ABS_OFFSET = 14 * 60      # 现实中最大 UTC+14


def set_offset(minutes) -> None:
    """由 Android 侧调用：TimeZone.getDefault().getOffset(...) / 60000。"""
    global _OFFSET_MIN, _SOURCE
    try:
        v = int(minutes)
    except (TypeError, ValueError):
        return
    if abs(v) > MAX_ABS_OFFSET:
        return                      # 明显异常的值不要，宁可退回系统时区
    _OFFSET_MIN = v
    _SOURCE = "device"


def offset_minutes() -> int | None:
    return _OFFSET_MIN


def source() -> str:
    return _SOURCE


def now() -> datetime:
    if _OFFSET_MIN is None:
        return datetime.now()
    tz = timezone(timedelta(minutes=_OFFSET_MIN))
    return datetime.now(timezone.utc).astimezone(tz)


def from_ts(ts: float) -> datetime:
    """把时间戳还原成本地时区的 datetime（显示「上次是几点」用）。"""
    return datetime.fromtimestamp(ts, tz=now().tzinfo)


def tz_name() -> str:
    if _OFFSET_MIN is None:
        return str(now().astimezone().tzname() or "system")
    sign = "+" if _OFFSET_MIN >= 0 else "-"
    hh, mm = divmod(abs(_OFFSET_MIN), 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}（设备）"
