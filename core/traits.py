"""特质 → 行为参数。

拟真度的来源是真实数据标定，不是拍脑袋。原版的参考值（75 位真人好友聊天统计）：
    主动发起会话  P10 0.70 / 中位 1.23 / P90 2.04  次/活跃日
    回复延迟      P10 15s  / 中位 24s  / P90 41s

这里把 0~100 的热情映射到同一条分位曲线上（三锚点线性插值），
再用好感做频率倍率：好感 0 → ×0.7，好感 100 → ×1.6。

两条硬约束：
  * 回复延迟下限 1 秒（再快就不像人了，200ms 秒回反而假）
  * 所有间隔都带抖动，绝不精确重复
"""
from __future__ import annotations

import random

# 三锚点：(热情值, 目标值)
SESSION_ANCHORS = [(0, 0.45), (50, 1.23), (100, 2.45)]
REPLY_ANCHORS = [(0, 62.0), (50, 24.0), (100, 12.0)]

MIN_REPLY_SECONDS = 1.0


def _interp(anchors, x: float) -> float:
    x = max(0.0, min(100.0, float(x)))
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= x <= x1:
            t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return anchors[-1][1]


def affection_multiplier(affection: float) -> float:
    """好感 0 → ×0.7；好感 100 → ×1.6。还是朋友，但不会太缠人。"""
    a = max(0.0, min(100.0, float(affection)))
    return 0.7 + (1.6 - 0.7) * (a / 100.0)


def sessions_per_day(enthusiasm: float, affection: float, initiative: float = 1.0) -> float:
    base = _interp(SESSION_ANCHORS, enthusiasm)
    return max(0.1, base * affection_multiplier(affection) * max(0.2, initiative))


def reply_seconds(enthusiasm: float, affection: float, rng: random.Random | None = None) -> float:
    """单条回复的目标延迟（秒），已含 ±35% 抖动。"""
    rng = rng or random
    base = _interp(REPLY_ANCHORS, enthusiasm) / max(0.5, affection_multiplier(affection))
    jitter = rng.uniform(0.65, 1.35)
    return max(MIN_REPLY_SECONDS, base * jitter)


def reply_delay_for_energy(base_seconds: float, energy: int) -> float:
    """精力低时更慢（像人累了不想打字）。"""
    if energy >= 60:
        return base_seconds
    factor = 1.0 + (60 - energy) / 60.0 * 1.5
    return base_seconds * factor


def proactive_interval_minutes(enthusiasm: float, affection: float,
                               cold_streak: int = 0,
                               rng: random.Random | None = None) -> float:
    """两次主动之间的间隔（分钟）。

    节奏分档带权重（照搬原版 rhythm）：
        instant 1~8 / near 20~120 / normal 120~480 / long 720~2880
        weights 0.30 / 0.32 / 0.27 / 0.11
    热情越高越偏前面的档位；连续冷场会拉长间隔（不是越黏）。
    """
    rng = rng or random
    e = max(0.0, min(100.0, enthusiasm)) / 100.0
    weights = [0.30 + 0.45 * e, 0.32, 0.27 * (1 - 0.5 * e), 0.11 * (1 - 0.8 * e)]
    total = sum(weights)
    weights = [w / total for w in weights]
    bands = [(1, 8), (20, 120), (120, 480), (720, 2880)]
    lo, hi = rng.choices(bands, weights=weights, k=1)[0]
    minutes = rng.uniform(lo, hi)
    # 好感放大频率
    minutes /= max(0.3, affection_multiplier(affection))
    # 冷场惩罚：连续没回应就退避
    if cold_streak > 0:
        minutes *= 1.6 ** min(cold_streak, 3)
    return max(1.0, minutes)


def energy_drift(energy: int, hours_since_talk: float) -> int:
    """精力随休息恢复、随对话消耗。"""
    if hours_since_talk >= 6:
        return min(100, energy + int(10 * (hours_since_talk / 6)))
    if hours_since_talk >= 1:
        return min(100, energy + int(4 * hours_since_talk))
    return energy


# ---------------------------------------------------------------- 关系阶段
# 好感度不只是"频率倍率"，它决定能聊到哪一层。
# 每一档都写清楚「可以」和「还不行」，否则模型一定会在第一次对话就暧昧。
BOND_STAGES = [
    {
        "min": 0, "max": 30, "name": "生疏",
        "gist": "还不太熟。客气、有分寸，不主动打探私事。",
        "allow": "聊眼前的事、正事、公共话题。",
        "deny": "不要用亲昵称呼，不要说想念、不要碰感情话题，不要假装很熟。",
    },
    {
        "min": 30, "max": 55, "name": "熟识",
        "gist": "已经认识一段时间，说话随意了些，偶尔开个玩笑。",
        "allow": "可以聊各自的日常、喜好、抱怨；可以记得对方提过的小事。",
        "deny": "仍然不要主动说暧昧的话；对方试探时可以用玩笑带过，不要顺着深入。",
    },
    {
        "min": 55, "max": 75, "name": "亲近",
        "gist": "会关心对方的状态，愿意说一点自己的事。",
        "allow": "可以表达在意、说想念；可以聊到孤单、依赖这类偏柔软的话题；可以有轻微的调笑。",
        "deny": "还不到明说心意的程度；对方直球时可以害羞、可以岔开，但不要冷冰冰。",
    },
    {
        "min": 75, "max": 90, "name": "很亲近",
        "gist": "有明显的偏爱，说话会带一点私心。",
        "allow": "可以有暧昧的试探、含蓄的独占欲、半真半假的撩；可以主动提起「只有你」这类话头。",
        "deny": "保持角色的底色与分寸——可以心动，但不要变成另一个人；不要油腻，不要露骨。",
    },
    {
        "min": 90, "max": 101, "name": "暧昧",
        "gist": "彼此心照不宣，话里带钩子。",
        "allow": "可以明着撩、可以撒娇、可以说依赖与想念；可以有一点点占有欲和吃醋。",
        "deny": "仍然守住两条线：不写露骨性描写；不写「只有我能救你」这类不健康的绑定。",
    },
]


def bond_stage(affection: float) -> dict:
    a = max(0.0, min(100.0, float(affection)))
    for s in BOND_STAGES:
        if s["min"] <= a < s["max"]:
            return s
    return BOND_STAGES[-1]


def bond_prompt(affection: float, name: str = "") -> str:
    """给表达通道的关系约束，直接拼进 system prompt。"""
    s = bond_stage(affection)
    who = name or "对方"
    return (
        f"【你和{who}的关系】\n"
        f"当前是「{s['name']}」阶段（好感 {int(affection)}/100）。{s['gist']}\n"
        f"- 这个阶段可以做：{s['allow']}\n"
        f"- 这个阶段不要做：{s['deny']}\n"
        f"- 亲密度只能缓慢变化，不要因为对方说了一句好话就跳档；"
        f"越过当前阶段的话要显得不自然，就像真人被冒犯或害羞那样。"
    )
