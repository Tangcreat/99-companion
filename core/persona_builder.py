"""角色人格蒸馏：给一个名字，产出一张人设卡。

这是"自己导入角色"的最省事形态——不用手写两千字人设，只要一个名字。
原版 `persona_builder.pyc` 的常量显示它优先走 DeepSeek Responses API 的
web_search 工具（仅 v4-pro 支持），失败则退回"用模型已有知识蒸馏"。

这里实现同一策略的降级链：
  1. 若配置了 `search_endpoint`（自建 SearXNG），先搜资料再蒸馏
  2. 否则纯模型知识蒸馏
  3. 全部失败抛 LLMError，由上层展示可读错误

产出约束（照搬原版 prompt 的取向）：
  * 不要客服腔、不要大段抒情、不要「作为AI」
  * 被追问身份时如实承认
  * 必须写出"说话方式"，否则这个角色跑起来一定没味道
"""
from __future__ import annotations

import json
import re

import requests

from .llm import LLM, LLMError

PERSONA_HEADINGS = [
    "身份", "外貌与气质", "性格", "说话方式",
    "在意的事", "回避的事", "关系定位", "边界",
]

GENERATE_SYSTEM = f"""你是角色卡撰写者。根据给定的角色名，产出一张可以直接用于角色扮演的中文人格卡。

只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块：
{{
  "found": true,
  "name": "角色名",
  "series": "作品名（原创或不确定就留空字符串）",
  "gender": "male 或 female 或 other（按这个角色真实的性别填）",
  "aliases": ["别名"],
  "summary": "一句话概括（不超过 30 字）",
  "interests": ["角色自己的兴趣1", "兴趣2", "兴趣3", "兴趣4", "兴趣5"],
  "persona_md": "完整人格卡 Markdown",
  "lore": [
    {{
      "title": "短标题（不超过 8 字）",
      "kind": "history",
      "keywords": ["触发词1", "触发词2"],
      "content": "100~200 字，写具体的一件事或一条设定，不要写空话",
      "always": false
    }}
  ]
}}

persona_md 必须用下面这些小标题（没资料的小节可省略）：
{chr(10).join('- ' + h for h in PERSONA_HEADINGS)}

persona_md 的硬性要求：
- 全文不少于 400 字，重点写「性格」和「说话方式」两节
- 「说话方式」必须具体到可执行：句子长短、口头禅、标点习惯、遇到不知道的事怎么回应
- 不要写成客服腔、不要大段抒情、不要出现「作为AI」「我是一个模型」这类句子
- 不要编造具体剧情细节；不确定的地方就写得笼统些
- **人称必须与 gender 一致**：男性用「他」，女性用「她」，不确定或非二元用「TA」或直接省略主语。
  李白、苏轼这类历史男性人物必须写「他」，不能写成「她」。拿不准就查资料确认，不要默认成女性。
- 如果这个名字对应多个同名角色，选最知名的那个，并在 series 里写清作品

lore 的硬性要求：
- 写 4~6 条，覆盖这几类：一段过往（kind=history）、角色所在的地方（place）、
  角色在意或纠葛的人（person）、一个生活上的习惯（habit）
- kind 只能是 history / place / person / habit / world / rule 之一
- keywords 写 2~4 个"聊天时可能被提到"的词，要具体（"雨天"太宽泛，"旧书店的阁楼"才好）
- content 必须具体到能画面感，不要写"角色有一段复杂的过去"这种废话
- always 一律给 false；只有世界观级别的铁律才给 true"""


def _strip_fences(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.S)
    if m:
        s = m.group(1)
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        return s[i:j + 1]
    return s


def _search_context(name: str, endpoint: str, timeout: int = 12) -> str:
    """用自建 SearXNG 抓一段资料作参考。失败就返回空串，不阻塞主流程。"""
    try:
        r = requests.get(
            endpoint.rstrip("/") + "/search",
            params={"q": f"{name} 角色 设定", "format": "json", "language": "zh-CN"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        chunks = []
        for item in (data.get("results") or [])[:5]:
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if content:
                chunks.append(f"- {title}：{content[:220]}")
        return "\n".join(chunks)
    except Exception:
        return ""


def generate_card(llm: LLM, name: str, series_hint: str = "",
                  extra_notes: str = "", search_endpoint: str = "",
                  gender_hint: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise LLMError("请先填写角色名字")
    if not llm.configured:
        raise LLMError("模型还没配置好，先到「设置」填 Base URL、API Key 和模型名")

    context = ""
    if search_endpoint:
        context = _search_context(name, search_endpoint)

    user_parts = [f"角色名：{name}"]
    if series_hint:
        user_parts.append(f"作品/来源：{series_hint}")
    if gender_hint in ("male", "female", "other"):
        label = {"male": "男性", "female": "女性", "other": "非二元/不确定"}[gender_hint]
        user_parts.append(f"这个角色的性别是：{label}。人称必须与人设一致。")
    if extra_notes:
        user_parts.append(f"补充要求：{extra_notes}")
    if context:
        user_parts.append(f"以下是检索到的资料，可参考但不要照抄：\n{context}")
    else:
        user_parts.append("请根据你已有的知识整理；不确定的地方不要编造。")

    raw = llm.chat(
        [{"role": "system", "content": GENERATE_SYSTEM},
         {"role": "user", "content": "\n".join(user_parts)}],
        temperature=0.7, max_tokens=2600,
    )
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise LLMError(f"模型没有返回合法 JSON：{raw[:160]}") from e

    persona = (data.get("persona_md") or "").strip()
    if len(persona) < 200:
        raise LLMError(f"生成的人设只有 {len(persona)} 字，太短了，换个模型或再试一次")

    gender = (gender_hint if gender_hint in ("male", "female", "other")
              else str(data.get("gender") or "").strip().lower())
    if gender not in ("male", "female", "other"):
        gender = "other"

    return {
        "name": (data.get("name") or name).strip(),
        "gender": gender,
        "series": (data.get("series") or series_hint or "").strip(),
        "aliases": data.get("aliases") or [],
        "summary": (data.get("summary") or "").strip(),
        "interests": data.get("interests") or [],
        "persona_md": persona,
        "lore": [e for e in (data.get("lore") or []) if isinstance(e, dict) and e.get("title")],
        "found": bool(data.get("found", True)),
        "used_search": bool(context),
    }
