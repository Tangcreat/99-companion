"""作息：让角色"有自己的生活"。

最关键的一个设计（照搬原版 schedules.json 的 _说明）：
    没配置作息的角色，按**名字 hash 派生**一份稳定的作息
    （睡点 23:00~01:40、起点 07:00~10:20），所以人物天生就错开。

好处：你新导入一个角色，不需要手动排班，她也不会跟别人同一秒醒来。

另外 `wake_label` 是"被吵醒时的搪塞理由"——不是干巴巴说"我在睡觉"，
而是给出符合人设的借口（"明天还要值班"）。
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import clock

DEFAULT_SLEEP_RANGE = (23 * 60, 23 * 60 + 160)   # 23:00 ~ 01:40
DEFAULT_WAKE_RANGE = (7 * 60, 7 * 60 + 200)      # 07:00 ~ 10:20


def _stable_rng(name: str) -> random.Random:
    h = hashlib.sha256(name.encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "little"))


def _hhmm_to_min(s: str) -> int:
    hh, mm = str(s).split(":")
    return int(hh) * 60 + int(mm)


def _min_to_hhmm(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class Schedule:
    sleep_min: int          # 0~1439，可视为"当晚的睡点"
    wake_min: int
    nap: tuple[int, int] | None = None
    nocturnal: bool = False
    label: str = ""
    derived: bool = True

    @property
    def sleep(self) -> str:
        return _min_to_hhmm(self.sleep_min)

    @property
    def wake(self) -> str:
        return _min_to_hhmm(self.wake_min)

    def _in_window(self, t: int, start: int, end: int) -> bool:
        if start == end:
            return False
        if start < end:
            return start <= t < end
        return t >= start or t < end          # 跨午夜

    def is_asleep(self, now: datetime | None = None) -> bool:
        now = now or clock.now()
        t = now.hour * 60 + now.minute
        if self._in_window(t, self.sleep_min, self.wake_min):
            return True
        if self.nap:
            if self._in_window(t, self.nap[0], self.nap[1]):
                return True
        return False

    def next_wake(self, now: datetime | None = None) -> datetime:
        now = now or clock.now()
        t = now.hour * 60 + now.minute
        delta = (self.wake_min - t) % 1440 or 1440
        return (now + timedelta(minutes=delta)).replace(second=0, microsecond=0)

    def to_dict(self) -> dict:
        d = {
            "sleep": self.sleep, "wake": self.wake,
            "nocturnal": self.nocturnal, "label": self.label,
            "derived": self.derived,
        }
        if self.nap:
            d["nap"] = [_min_to_hhmm(self.nap[0]), _min_to_hhmm(self.nap[1])]
        return d


def build(name: str, cfg: dict | None = None) -> Schedule:
    """有配置用配置；没有就按名字派生——这是让新角色天然错开的关键。"""
    cfg = cfg or {}
    if cfg.get("sleep") and cfg.get("wake"):
        nap = None
        if cfg.get("nap") and len(cfg["nap"]) == 2:
            nap = (_hhmm_to_min(cfg["nap"][0]), _hhmm_to_min(cfg["nap"][1]))
        return Schedule(
            sleep_min=_hhmm_to_min(cfg["sleep"]),
            wake_min=_hhmm_to_min(cfg["wake"]),
            nap=nap,
            nocturnal=bool(cfg.get("nocturnal")),
            label=cfg.get("label", ""),
            derived=False,
        )

    rng = _stable_rng(name)
    sleep_m = rng.randint(*DEFAULT_SLEEP_RANGE)
    # 睡得晚会起得晚：起点跟着睡点走，再叠加一点个人差异
    offset = sleep_m - DEFAULT_SLEEP_RANGE[0]
    wake_m = DEFAULT_WAKE_RANGE[0] + offset + rng.randint(-20, 60)
    wake_m = max(5 * 60, min(12 * 60, wake_m))
    nap = None
    if rng.random() < 0.3:
        nap_start = rng.randint(12 * 60, 13 * 60 + 30)
        nap = (nap_start, nap_start + rng.choice([30, 40, 50]))
    labels = [
        "明天还要早起", "今天有点累", "刚躺下没多久",
        "明天有事要准备", "已经睡下了",
    ]
    return Schedule(sleep_m, wake_m, nap, rng.random() < 0.15,
                    rng.choice(labels), derived=True)


def describe_phase(sch: Schedule, now: datetime | None = None) -> str:
    """给提示词用的自然语言时间状态。

    注意：午睡和夜间睡眠必须分开说。之前两者都返回「现在是深夜」，
    结果大中午找她说话，模型看到「深夜」就答「现在是凌晨一点」——
    这是被实测抓到的真故障，不是模型幻觉。
    """
    now = now or clock.now()
    t = now.hour * 60 + now.minute

    if sch.is_asleep(now):
        if sch.nap and sch._in_window(t, sch.nap[0], sch.nap[1]):
            return (f"你在午睡（平时 {_min_to_hhmm(sch.nap[0])}~{_min_to_hhmm(sch.nap[1])}），"
                    f"刚被消息吵醒，人还有点懵")
        return f"你已经在睡了（平时 {sch.wake} 左右起），这条消息把你吵醒了"

    mins_to_sleep = (sch.sleep_min - t) % 1440
    if mins_to_sleep <= 45:
        return f"快到你的睡觉时间了（{sch.sleep} 睡）"
    if now.hour < 5:
        return "凌晨，你还没睡"
    if now.hour < 11:
        return "早上，你刚起来不久"
    if now.hour < 14:
        return "中午"
    if now.hour < 18:
        return "下午"
    if now.hour < 22:
        return "晚上"
    return "夜深了"
