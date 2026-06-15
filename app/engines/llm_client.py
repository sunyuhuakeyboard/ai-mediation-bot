"""LLM 短句生成客户端（OpenAI 兼容流式接口）。

低时延关键设计（对应方案 §LLM短句生成）：
- stream=True，边收边判：累计到首个句末标点（≥5字）即截断返回，不等全量；
- 总预算 asyncio.timeout（默认1.5s）：超时返回已收内容或 None，由编排层回退参考话术；
- max_tokens 限制在 60 内，temperature 0.2~0.5；输出经 sanitize_tts 清洗后供 TTS。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import orjson

from app.config import Settings
from app.utils.text import first_sentence_cut, sanitize_tts

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        timeout = httpx.Timeout(
            connect=settings.llm_connect_timeout_ms / 1000,
            read=settings.llm_total_timeout_ms / 1000,
            write=5.0, pool=5.0,
        )
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self.client = httpx.AsyncClient(base_url=settings.llm_base_url,
                                        timeout=timeout, headers=headers)

    async def short_reply(self, messages: list[dict], max_chars: int | None = None) -> str | None:
        """流式生成一句话；失败/超时返回 None（调用方回退模板）。"""
        max_chars = max_chars or self.s.reply_max_chars
        payload = {
            "model": self.s.llm_model,
            "messages": messages,
            "stream": True,
            "temperature": self.s.llm_temperature,
            "max_tokens": self.s.llm_max_tokens,
        }
        buf = ""
        try:
            async with asyncio.timeout(self.s.llm_total_timeout_ms / 1000):
                async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                    if resp.status_code != 200:
                        logger.warning("llm http %s", resp.status_code)
                        return None
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = (orjson.loads(data)["choices"][0]
                                     .get("delta", {}).get("content")) or ""
                        except Exception:
                            continue
                        if not delta:
                            continue
                        buf += delta
                        text, done = first_sentence_cut(buf, max_chars)
                        if done:
                            return sanitize_tts(text) or None
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("llm degraded (%s), partial=%r", type(exc).__name__, buf[:20])
        except Exception:
            logger.exception("llm unexpected error")
            return None
        return sanitize_tts(buf) or None

    async def complete(self, messages: list[dict], max_tokens: int = 300,
                       temperature: float = 0.0) -> str | None:
        """非流式完整生成（离线质检/变体生成用，不在通话回合路径上）。"""
        try:
            resp = await self.client.post("/chat/completions", json={
                "model": self.s.llm_model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens})
            if resp.status_code != 200:
                return None
            return resp.json()["choices"][0]["message"]["content"]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("llm complete failed")
            return None

    async def aclose(self) -> None:
        await self.client.aclose()
