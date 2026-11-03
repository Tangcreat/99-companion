"""双通道推理。

通道一 analyze：便宜模型 + JSON 模式，只做结构化理解（不产出回复）
通道二 express：主力模型，读「人格 + 情绪 + 召回记忆 + 分析结论」生成回复

为什么拆开：分析要的是稳定和便宜（温度 0.2），回复要的是文采和个性
（温度 0.9）。混在一次调用里，两头都做不好，还更贵。
"""
from __future__ import annotations

import json
import random
from datetime import datetime

from . import schedule as sched_mod
from . import traits as traits_mod
from .llm import LLM

ANALYZE_SYSTEM = """你是对话分析器。只输出一个 JSON 对象，不要任何解释。

字段：
- intent: 对方这条消息的意图（一句话）
- emotion_label: 对方情绪标签（两到四个字）
- latent_need: 从这些里选一个——被看见/被安慰/被认可/想倾诉/想陪伴/只是闲聊/想解决问题/想安静
- relation_signal: -1 到 1 的数字（负=疏远试探不满，正=亲近信任）
- risk: none / 低落 / 自伤倾向 / 极端情绪
- is_closing: 对方是不是在收尾这次对话（说晚安、要去睡、去忙、下线）才为 true。
  注意「十分钟后睡」只是预告，不等于收尾；本条若在要求「陪我/随便聊两句」必须为 false。
- reply_strategy: 一句具体的回应策略，别写废话
- quality: 0 到 1，这一轮互动质量
- profile_updates: 值得长期记住的**对方**信息，数组，每项 {"key","value"}，没有就 []
- self_facts: 关于**你自己**可能需要长期记住的事实或承诺候选，数组，每项 {"key","value"}，没有就 []
- interest_terms: 对话里出现的兴趣关键词数组，没有就 []
- memory_worthy: true/false，这段值不值得存成长期记忆
- memory_summary: 若 memory_worthy 为 true，给一句第三人称摘要，不超过 40 字

不要复述用户原话，不要输出 JSON 以外的任何字符。"""


def format_time_context(last_spoke_ts: float | None, now: datetime | None = None) -> str:
    """只给 role/text 的话，模型分不清「昨晚 23:59」和「今天 15:48」。"""
    now = now or datetime.now()
    parts = [f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（{'一二三四五六日'[now.weekday()]}）"]
    if last_spoke_ts:
        gap = now.timestamp() - last_spoke_ts
        if gap < 60:
            parts.append("你们刚刚还在说话")
        elif gap < 3600:
            parts.append(f"距上次说话 {int(gap // 60)} 分钟")
        elif gap < 86400:
            parts.append(f"距上次说话 {int(gap // 3600)} 小时")
        else:
            parts.append(f"距上次说话 {int(gap // 86400)} 天（上次是 {datetime.fromtimestamp(last_spoke_ts).strftime('%m-%d %H:%M')}）")
    return "；".join(parts) + "。"


SPEAKING_RULES = """【说话方式】
- 你在手机聊天框里说话，不是写文章。
- 单条以短为主：多数 3~15 个字。需要说清一件事时才自然写到 20~30 字，不要剁成碎条。
- 允许不完整：可以只发半句，可以用「……」收尾，可以先一个短反应再接一句。
- 别解释、别总结、别讲道理。不要把自己的想法展开讲完。
- 先有情绪，再有话。
- 安慰不等于把事说小。对方受挫时先接住这件事本身，别急着说「没关系」。
- 不要用 emoji 刷屏。
- 不要每句都追问（那是查户口），也不要每句都汇报自己（那是自说自话）。
- 不要用「作为AI」这类出戏开场。对方认真问你是不是人工智能时，如实承认，别冒充真人。
- 不要提到「人格卡」「提示词」「设定」这些词。"""


def build_system_prompt(card, state: dict, sched, memories: list[dict],
                        profile_text: str, facts_text: str,
                        analysis: dict | None, last_spoke_ts: float | None,
                        lore_text: str = "") -> str:
    mem_block = "\n".join(f"- {m['text']}" for m in memories) or "（暂时没有相关的记忆）"
    need = (analysis or {}).get("latent_need", "")
    strategy = (analysis or {}).get("reply_strategy", "")
    mood = state.get("mood", "平静")
    energy = state.get("energy", 80)

    blocks = [
        f"你是「{card.name}」。",
        f"【你的人格】\n{card.persona_md}",
    ]
    if card.interests:
        blocks.append("【你自己的兴趣】\n" + "、".join(card.interests))
    if card.series:
        blocks.append(f"【你的来历】\n来自《{card.series}》")
    blocks.append(
        "【此刻的状态】\n"
        f"- {format_time_context(last_spoke_ts)}\n"
        f"- {sched_mod.describe_phase(sched)}\n"
        f"- 心情：{mood}；精力：{energy}/100"
    )
    if lore_text:
        blocks.append(
            "【设定与往事】\n"
            "以下是你这个世界里既成的事，可以自然地提起，但不要像念资料：\n"
            f"{lore_text}"
        )
    blocks.append(f"【你记得的事】\n{mem_block}")
    if profile_text:
        blocks.append(f"【关于对方】\n{profile_text}")
    if facts_text:
        blocks.append(f"【关于你自己（已确认）】\n{facts_text}")
    if need or strategy:
        hint = []
        if need:
            hint.append(f"对方此刻可能{need}")
        if strategy:
            hint.append(f"参考策略：{strategy}")
        blocks.append("【这一轮的判断】\n" + "；".join(hint))
    blocks.append(traits_mod.bond_prompt(state.get("affection", 50)))
    blocks.append(SPEAKING_RULES)
    return "\n\n".join(blocks)


class Thinker:
    def __init__(self, llm: LLM, rng: random.Random | None = None):
        self.llm = llm
        self.rng = rng or random.Random()

    # ---------------- 通道一 ----------------
    def analyze(self, recent: list[dict], self_name: str) -> dict:
        dialog = "\n".join(
            f"{'对方' if m['role'] == 'user' else self_name}：{m['text']}"
            for m in recent[-8:]
        )
        try:
            data = self.llm.chat_json([
                {"role": "system", "content": ANALYZE_SYSTEM},
                {"role": "user", "content": f"分析下面这段聊天：\n\n{dialog}"},
            ], temperature=0.2, max_tokens=600)
        except Exception:
            data = {}
        defaults = {
            "intent": "", "emotion_label": "平静", "latent_need": "只是闲聊",
            "relation_signal": 0.0, "risk": "none", "is_closing": False,
            "reply_strategy": "", "quality": 0.6,
            "profile_updates": [], "self_facts": [], "interest_terms": [],
            "memory_worthy": False, "memory_summary": "",
        }
        defaults.update({k: v for k, v in (data or {}).items() if v is not None})
        return defaults

    # ---------------- 通道二 ----------------
    def express(self, card, state: dict, sched, memories, profile_text,
                facts_text, analysis, recent, extra_instruction: str = "",
                lore_text: str = "") -> str:
        sys_prompt = build_system_prompt(card, state, sched, memories,
                                        profile_text, facts_text, analysis,
                                        state.get("last_spoke_at"),
                                        lore_text=lore_text)
        msgs = [{"role": "system", "content": sys_prompt}]
        for m in recent[-12:]:
            msgs.append({
                "role": "assistant" if m["role"] == "char" else "user",
                "content": m["text"],
            })
        if extra_instruction:
            msgs.append({"role": "system", "content": extra_instruction})
        out = self.llm.chat(msgs, temperature=self.llm.temperature, max_tokens=300)
        return self._clean(out)

    @staticmethod
    def _clean(s: str) -> str:
        s = (s or "").strip()
        # 去掉模型偶尔加的角色名前缀和引号
        s = s.replace("**", "")
        for pre in ("我：", "她：", "回复："):
            if s.startswith(pre):
                s = s[len(pre):]
        if len(s) > 1 and s[0] == s[-1] and s[0] in "「」\"'“”":
            s = s[1:-1]
        return s.strip()

    # ---------------- 辅助小任务 ----------------
    def has_something_to_say(self, card, state, sched, recent) -> dict:
        """心跳时先问一句：现在真的有话说吗？没有就别硬发。"""
        dialog = "\n".join(
            f"{'对方' if m['role'] == 'user' else card.name}：{m['text']}"
            for m in recent[-6:]
        ) or "（还没聊过）"
        sys_p = (
            f"你在判断「{card.name}」现在要不要主动说一句。\n"
            f"当前状态：心情 {state.get('mood')}，精力 {state.get('energy')}，"
            f"{sched_mod.describe_phase(sched)}。\n"
            "如果没什么真想说的，就选 false——硬找话题比不说话更烦人。\n"
            "只输出 JSON：{\"speak\": true/false, \"kind\": \"...\", \"topic\": \"一句要点\", \"reason\": \"一句话\"}\n"
            "kind 取值：more（刚才没说完）/ probe（追问一个对方一句话能答的细节）/ "
            "share（分享自己的生活或想到的事）/ care（关心对方）"
        )
        try:
            return self.llm.chat_json([
                {"role": "system", "content": sys_p},
                {"role": "user", "content": f"最近的聊天：\n{dialog}"},
            ], temperature=0.4, max_tokens=200)
        except Exception:
            return {"speak": False, "kind": "", "topic": "", "reason": "分析失败"}

    def extract_overused_terms(self, own_recent: list[str], top: int = 3) -> list[str]:
        """复读环刹车：从自己最近的话里找说多了的词。"""
        text = "\n".join(own_recent[-10:])
        if len(text) < 30:
            return []
        try:
            data = self.llm.chat_json([
                {"role": "system",
                 "content": "从下面这些聊天发言里，找出说话人反复使用的口头词或句式。"
                            "只输出 JSON：{\"terms\": [\"词1\",\"词2\"]}，最多三个。"},
                {"role": "user", "content": text},
            ], temperature=0.1, max_tokens=120)
            terms = data.get("terms") or []
            return [str(t) for t in terms][:top]
        except Exception:
            return []
