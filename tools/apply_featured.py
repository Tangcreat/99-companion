"""重点角色的称呼与参数——人工定稿。

模型给的数值方向可用，但称呼一塌糊涂：它会把别的角色名当成对方的名字
（红弩箭→「维尔汀」、霍夫曼→「马库斯职员」、未锈铠→「两位新朋友」）。
称呼这种一眼就能看出对错的东西，不该交给模型拍脑袋。

三个数字的口径：
  enthusiasm 热情  —— 她主动开口的意愿（话多外向高，克制寡言低）
  affection  好感  —— 刚认识时的默认态度（起点，不是上限）
  initiative 主动  —— 主动频率倍率，0.6 冷淡 ~ 1.5 黏人
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from core.cards import CardStore      # noqa: E402
from core.db import Store             # noqa: E402

CHAR_DIR = os.path.join(HERE, "characters")
DB = os.path.join(HERE, "data", "companion.db")

# 名字: (称呼, 热情, 好感, 主动性, 依据)
TUNED: dict[str, tuple] = {
    # 称呼留空的，一律用「对方的昵称」（默认「老师」）。
    # 只保留三个既有出处、又不冒犯人的：司辰 / 阁下 / 先生。
    "37":      ("",    78, 42, 1.15, "把人也当数字看，见面先想给你归类"),
    "维尔汀":   ("司辰", 45, 42, 0.85, "寡言克制，只记录不表态"),
    "十四行诗":  ("阁下", 60, 58, 1.10, "礼仪周全，把助手身份当信仰"),
    "露西":     ("先生", 40, 40, 0.75, "时间即成本，只谈正事"),
    "贝丽尔":   ("",    42, 50, 0.85, "占卜师口吻，慢而耐心"),
    "纸信圈儿":  ("",    82, 60, 1.30, "为送信而生，见人就凑上来"),
    "玛丽安娜":  ("",    50, 38, 0.90, "军人习惯，先服从再谈感情"),
    "红弩箭":   ("",    72, 55, 1.15, "嘴上刻薄，实则兜底"),
    "斯奈德":   ("",    68, 40, 1.10, "笑意底下是枪口，越在乎越用玩笑隔开"),
    "未锈铠":   ("",    55, 62, 0.95, "骑士礼仪，长句慢条斯理"),
    "苏菲亚":   ("",    38, 40, 0.75, "宣读条文式的克制，自我贬抑"),
    "鹭鸶剪":   ("",    70, 58, 1.10, "洒脱任真，爱追问来龙去脉"),
    "程和光":   ("",    48, 52, 0.90, "守正之士，话短务实"),
    "梁月":     ("",    52, 48, 0.95, "接警式干练，公事公办"),
    "牙仙":     ("",    58, 58, 1.00, "哄人的耐心，语气轻"),
    "雾行者":   ("",    50, 45, 0.80, "公务在身，话都围着工作"),
    "霍夫曼":   ("",    45, 45, 0.85, "调查员的保密纪律，不闲聊"),
    "星锑":     ("",    88, 55, 1.40, "吵闹跳脱，像在直播"),
    "伊索尔德":  ("",    62, 45, 1.05, "礼貌而疏离，情绪外放"),
    "卡卡尼亚":  ("",    72, 52, 1.15, "心理医生，爱用反问步步紧逼"),
}


def main():
    dry = "--dry" in sys.argv
    store = CardStore(CHAR_DIR)

    rows = []
    for name, (addr, ent, aff, ini, why) in TUNED.items():
        try:
            card = store.load(name)
        except Exception:
            rows.append((name, addr, ent, aff, ini, "角色不存在"))
            continue
        rows.append((name, addr, ent, aff, ini, why))
        if dry:
            continue
        card.address = addr
        card.traits = {"enthusiasm": ent, "affection": aff, "initiative": ini}
        store.save(card)

    print(f"{'角色':12s}{'称呼':8s}{'热情':>5s}{'好感':>5s}{'主动':>6s}  依据")
    print("-" * 74)
    for name, addr, ent, aff, ini, why in rows:
        print(f"{name:12s}{addr:8s}{ent:5d}{aff:5d}{ini:6.2f}  {why}")

    if dry:
        print("\n（试运行，未写盘）")
        return 0

    # 状态表里已经有行的话，参数不会跟着卡变，得显式覆盖
    db = Store(DB)
    n = 0
    for name, (addr, ent, aff, ini, _why) in TUNED.items():
        try:
            card = store.load(name)
        except Exception:
            continue
        row = db.q1("SELECT char_id FROM char_state WHERE char_id=?", (card.id,))
        if row:
            db.x("UPDATE char_state SET enthusiasm=?,affection=?,initiative=? WHERE char_id=?",
                 (ent, aff, ini, card.id))
        else:
            db.x("INSERT INTO char_state(char_id,enthusiasm,affection,initiative) VALUES(?,?,?,?)",
                 (card.id, ent, aff, ini))
        n += 1
    print(f"\n已写入 {n} 个角色的卡与状态")
    return 0


if __name__ == "__main__":
    sys.exit(main())
