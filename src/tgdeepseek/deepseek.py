from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import DeepSeekConfig

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature if temperature is None else temperature,
            "thinking": {"type": "enabled" if self.config.thinking_enabled else "disabled"},
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    raise DeepSeekError(f"DeepSeek 暂时不可用，HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise DeepSeekError("DeepSeek 返回了空内容")
                return content.strip()
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, DeepSeekError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                logger.warning("DeepSeek 请求失败，准备重试（第 %s 次）: %s", attempt + 1, type(exc).__name__)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise DeepSeekError(f"DeepSeek 请求失败：{last_error}") from last_error

    async def should_join(self, system: str, prompt: str) -> bool:
        result = await self.complete(system, prompt, max_tokens=8, temperature=0.1)
        decision = result.strip().upper()
        logger.info("群聊参与判断=%s", decision[:20])
        return decision.startswith("REPLY")

    async def is_advertising(self, system: str, prompt: str) -> bool:
        result = await self.complete(system, prompt, max_tokens=8, temperature=0.0)
        decision = result.strip().upper()
        logger.info("广告内容判断=%s", decision[:20])
        return decision.startswith("AD")

