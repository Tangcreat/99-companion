"""给角色生成个性签名。

签名是「她自己会说出口的一句短话」，不是人物简介——简介是第三人称的档案，
签名是第一人称的态度。差别很大：

    简介：阿派朗学派的天才质数，能看见万事万物的「数字」
    签名：一切都可以被证明，除了我自己

用法：
    python tools/make_signatures.py               # 只补没有签名的
    python tools/make_signatures.py --force       # 全部重写
    python tools/make_signatures.py --only 37,维尔汀
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

from core.cards import CardStore                     # noqa: E402
from core.llm import LLM, LLMError                   # noqa: E402

CHAR_DIR = os.path.join(HERE, "characters")
CONFIG = os.path.join(HERE, "config.json")

BATCH = 8          # 一次让模型写几个角色的签名
MAX_SIG = 16       # 签名上限（字符）

SYSTEM = f"""你在给角色写「个性签名」。

个性签名 = 这个角色会自己写在主页上的那一句话。要求：

- 第一人称，或格言式的旁观口吻，两种都可以
- 不超过 {MAX_SIG} 个中文字符，越短越好
- 要贴她的性格、职业、在意的东西，或她说话的方式
- 不要写成人物简介（「xx组织的成员，擅长xx」这种一律不要）
- 不要剧透关键剧情，不要出现具体人名地名
- 不用引号、不加句号以外的标点堆砌
- 读完要能感觉到「这是一个人」，而不是「这是一条设定」

只输出一个 JSON 对象，键是角色名，值是签名，不要任何解释：
{{"角色名": "签名", ...}}

反例（不要这样写）：
  「阿派朗学派的天才，能看见万事万物的数字」——这是简介
  「我很孤独但我很坚强」——太空泛，谁都能说
正例：
  「一切都可以被证明。除了我自己。」
  「雨下多久，我就等多久。」
  「别问我值不值，我算过了。」"""


def load_cards() -> list[dict]:
    store = CardStore(CHAR_DIR)
    out = []
    for c in store.list():
        out.append({
            "slug": c.slug,
            "name": c.name,
            "summary": (c.summary or "").strip(),
            "persona": (c.persona_md or "").strip()[:420],
            "signature": (c.signature or "").strip(),
        })
    return out


def make_prompt(batch: list[dict]) -> str:
    parts = []
    for c in batch:
        parts.append(
            f"### {c['name']}\n"
            f"简介：{c['summary'] or '（无）'}\n"
            f"人格片段：{c['persona'] or '（无）'}"
        )
    return "\n\n".join(parts)


def parse(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {}
    try:
        d = json.loads(m.group())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def clean_sig(s: str) -> str:
    s = str(s or "").strip()
    s = s.strip("「」\"'“”‘’《》")
    s = re.sub(r"\s+", "", s)
    if len(s) > MAX_SIG:
        s = s[:MAX_SIG]
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    llm = LLM(cfg["llm"])

    cards = load_cards()
    by_name = {c["name"]: c for c in cards}

    if args.only:
        targets = [by_name[n.strip()] for n in args.only.split(",") if n.strip() in by_name]
    else:
        targets = [c for c in cards if args.force or not c["signature"]]

    if not targets:
        print("所有角色都已有签名。要重写加 --force")
        return 0

    print(f"待生成 {len(targets)} 个角色，每批 {BATCH} 个")
    if args.dry:
        for c in targets[:5]:
            print(f"  - {c['name']}")
        return 0

    if not llm.configured:
        print("模型未配置，先在设置里填 API Key")
        return 1

    store = CardStore(CHAR_DIR)
    done, failed = 0, []
    t0 = time.time()

    for i in range(0, len(targets), BATCH):
        batch = targets[i:i + BATCH]
        names = [c["name"] for c in batch]
        print(f"  [{i + 1}-{i + len(batch)}/{len(targets)}] {', '.join(names)}", end="")
        try:
            raw = llm.chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": make_prompt(batch)}],
                temperature=0.85, max_tokens=900,
            )
            got = parse(raw)
        except LLMError as e:
            print(f"  × {e}")
            failed.extend(names)
            continue

        hit = 0
        for c in batch:
            sig = clean_sig(got.get(c["name"], ""))
            if not sig:
                # 模型偶尔漏一个，用简介兜底（截断到首句）
                sig = clean_sig((c["summary"] or "").split("，")[0])
            if not sig:
                failed.append(c["name"])
                continue
            card = store.load(c["slug"])
            card.signature = sig
            store.save(card)
            hit += 1
            done += 1
        print(f"  ✓ {hit}/{len(batch)}")

    print(f"\n完成 {done} 个，用时 {time.time() - t0:.0f}s")
    if failed:
        print("未生成：", ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
