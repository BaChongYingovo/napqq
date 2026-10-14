"""OpenAI 兼容 Chat Completions 客户端（标准库 urllib + 线程池，不阻塞事件循环）。"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("qqagent.llm")


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.api_key = str(cfg.get("api_key", ""))
        self.model = str(cfg.get("model", ""))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.max_tokens = int(cfg.get("max_tokens", 800))
        self.timeout = float(cfg.get("timeout_seconds", 90))
        self.extra_body = cfg.get("extra_body") or {}

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _blocking_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        payload.update(self.extra_body)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "qq-agent/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"网络错误: {e.reason}") from None
        except TimeoutError:
            raise LLMError("请求超时") from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise LLMError(f"响应不是 JSON: {raw[:200]}") from None
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应缺少 choices: {raw[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = (content or "").strip()
        if not text:
            reasoning = message.get("reasoning_content") or ""
            if reasoning:
                log.warning("模型只返回了 reasoning_content，正文为空")
            raise LLMError("模型返回空内容")
        return text

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                return await asyncio.to_thread(self._blocking_chat, messages, temperature, model, max_tokens)
            except LLMError as e:
                last_err = e
                msg = str(e)
                retryable = ("超时" in msg) or ("网络错误" in msg) or any(f"HTTP {c}" in msg for c in (429, 500, 502, 503, 504))
                if not retryable or attempt == 3:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
        raise last_err or LLMError("未知错误")
