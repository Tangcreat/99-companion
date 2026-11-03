"""OpenAI 兼容的 LLM 客户端（零 SDK 依赖，只用 requests）。

支持任何 /chat/completions 兼容端点：DeepSeek、OpenAI、Moonshot、本地
Ollama / vLLM / LM Studio 都行。分开配置 analysis_model 与 express_model
是这套架构省钱的关键——分析任务不需要文采。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.S)
    if m:
        s = m.group(1)
    # 容错：从第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        return s[i:j + 1]
    return s


class LLM:
    def __init__(self, cfg: dict):
        self.base = (cfg.get("base_url") or "").rstrip("/")
        self.key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "gpt-4o-mini"
        self.analysis_model = cfg.get("analysis_model") or self.model
        self.timeout = int(cfg.get("timeout") or 90)
        self.temperature = float(cfg.get("temperature") or 0.9)
        self.max_tokens = int(cfg.get("max_tokens") or 800)

    @property
    def configured(self) -> bool:
        return bool(self.base and self.key)

    def _post(self, payload: dict) -> str:
        if not self.configured:
            raise LLMError("模型还没有配置好。请打开「设置」，填写 Base URL、API Key 和模型名")
        url = f"{self.base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        last = None
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code == 401:
                    raise LLMError("API Key 无效或已过期（401）")
                if r.status_code >= 400:
                    raise LLMError(f"模型接口返回 {r.status_code}：{r.text[:300]}")
                data = r.json()
                return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            except requests.Timeout as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
            except requests.RequestException as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"模型请求失败：{last}")

    # ---- 普通对话 ----
    def chat(self, messages: list[dict], *, model: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return self._post(payload)

    def chat_json(self, messages: list[dict], *, model: str | None = None,
                  temperature: float = 0.3, max_tokens: int | None = None) -> dict:
        raw = self.chat(messages, model=model or self.analysis_model,
                        temperature=temperature, max_tokens=max_tokens, json_mode=True)
        try:
            return json.loads(_strip_fences(raw))
        except json.JSONDecodeError:
            # 有些端点不支持 json_object，退回手动解析
            try:
                return json.loads(_strip_fences(raw))
            except Exception as e:
                raise LLMError(f"模型没有返回合法 JSON：{raw[:200]}") from e

    def ping(self) -> dict:
        t0 = time.time()
        msg = self.chat([{"role": "user", "content": "回复两个字：收到"}], max_tokens=16)
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "reply": msg.strip()[:40]}
