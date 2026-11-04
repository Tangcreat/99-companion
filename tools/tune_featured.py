"""给 20 位重点角色调参：称呼、热情、好感、主动性。

为什么要单独调：默认值让所有人都一样——一样热情、一样黏人、一样叫你「你」。
但 37 会追着问数字，维尔汀只会记下来，红弩箭张口就是「小鬼」。
参数不区分，二十个人说话就是同一个人的二十套衣服。

用法：
    python tools/tune_featured.py --dry      # 只看模型建议，不写盘
    python tools/tune_featured.py            # 写回角色卡与状态表
    python tools/tune_featured.py --only 37,维尔汀
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from core.cards import CardStore                   # noqa: E402
from core.db import Store                          # noqa: E402
from core.llm import LLM, LLMError                 # noqa: E402

CHAR_DIR = os.path.join(HERE, "characters")
CONFIG = os.path.join(HERE, "config.json")
DB = os.path.join(HERE, "data", "companion.db")

FEATURED = [
    "37", "维尔汀", "十四行诗", "露西", "贝丽尔",
    "纸信圈儿", "玛丽安娜", "红弩箭", "斯奈德", "未锈铠",
    "苏菲亚", "鹭鸶剪", "程和光", "梁月", "牙仙",
    "雾行者", "霍夫曼", "星锑", "伊索尔德", "卡卡尼亚",
]

SYSTEM = """你在给角色定「陪伴参数」。每个角色三个数字 + 一个称呼。

判断标准（不要套模板，按人物本身来）：

**enthusiasm 热情（0-100）** — 她主动找对方说话的意愿。
  话多、好奇心重、外向、闲不住 → 70-90
  正常社交、会主动但不黏 → 50-70
  克制、冷淡、忙于自己的事 → 25-45

**affection 初始好感（0-100）** — 刚认识时她对陌生人的默认态度。
  亲和、温柔、对谁都客气 → 55-70
  中立、职业化 → 40-55
  戒备、傲慢、不轻易信人 → 25-40
  注意：这是"起点"，不是"上限"，别给 90。

**initiative 主动频率倍率（0.6-1.5）** — 决定她多久找你一次。
  黏人、闲、在乎 → 1.1-1.5
  普通 → 0.9-1.1
  疏离、忙 → 0.6-0.9

**address 称呼（1-4 个字）** — 她固定怎么叫对方。
  优先用作品里她真实的称呼方式；
  没有明确称呼时，用符合她身份与语气的通称（如「阁下」「老师」「同志」「小鬼」「先生」）；
  **不要**一律用「你」，也**不要**用「宝贝」「亲爱的」这种通用爱称（除非她本来就这么说话）。

只输出 JSON 数组，不要解释：
[{"name":"角色名","address":"称呼","enthusiasm":70,"affection":45,"initiative":1.1,"why":"一句话理由"}]"""


def prompt_for(cards: list[dict]) -> str:
    parts = []
    for c in cards:
        parts.append(
            f"### {c['name']}\n"
            f"简介：{c['summary'] or '（无）'}\n"
            f"人格片段：{c['persona'][:600]}"
        )
    return "\n\n".join(parts)


def parse(raw: str) -> list[dict]:
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M)
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        d = json.loads(m.group())
        return [x for x in d if isinstance(x, dict)]
    except Exception:
        return []


def clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    llm = LLM(cfg["llm"])
    if not llm.configured:
        print("模型未配置")
        return 1

    names = [x.strip() for x in args.only.split(",")] if args.only else FEATURED
    store = CardStore(CHAR_DIR)

    cards = []
    for n in names:
        try:
            c = store.load(n)
        except Exception:
            print(f"  跳过 {n}：角色不存在")
            continue
        cards.append({"name": c.name, "slug": c.slug,
                      "summary": c.summary, "persona": c.persona_md})

    if not cards:
        print("没有可处理的角色")
        return 0

    print(f"处理 {len(cards)} 个角色...")
    try:
        raw = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt_for(cards)}],
            temperature=0.5, max_tokens=2600,
        )
    except LLMError as e:
        print("模型调用失败:", e)
        return 1

    items = parse(raw)
    if not items:
        print("模型没有返回可解析的 JSON")
        return 1

    by_name = {c["name"]: c for c in cards}
    db = Store(DB)
    applied = 0

    print(f"\n{'角色':12s}{'称呼':8s}{'热情':>5s}{'好感':>5s}{'主动':>6s}  理由")
    print("-" * 70)
    for it in items:
        name = str(it.get("name") or "").strip()
        c = by_name.get(name)
        if not c:
            continue
        addr = str(it.get("address") or "").strip().strip("「」\"'")[:6]
        ent = int(clamp(it.get("enthusiasm"), 0, 100, 55))
        aff = int(clamp(it.get("affection"), 0, 100, 45))
        ini = round(clamp(it.get("initiative"), 0.6, 1.5, 1.0), 2)
        why = str(it.get("why") or "")[:34]
        print(f"{name:12s}{addr:8s}{ent:5d}{aff:5d}{ini:6.2f}  {why}")

        if args.dry:
            continue
        card = store.load(c["slug"])
        card.address = addr
        card.traits = {"enthusiasm": ent, "affection": aff, "initiative": ini}
        store.save(card)
        # 状态表里已有行的话，参数不会自动跟着卡变，得显式覆盖
        row = db.q1("SELECT char_id FROM char_state WHERE char_id=?", (card.id,))
        if row:
            db.x("UPDATE char_state SET enthusiasm=?,affection=?,initiative=? WHERE char_id=?",
                 (ent, aff, ini, card.id))
        else:
            db.x("INSERT INTO char_state(char_id,enthusiasm,affection,initiative) VALUES(?,?,?,?)",
                 (card.id, ent, aff, ini))
        applied += 1

    if args.dry:
        print("\n（试运行，未写盘）")
    else:
        print(f"\n已写入 {applied} 个角色")
    return 0


if __name__ == "__main__":
    sys.exit(main())
