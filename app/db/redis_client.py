"""Redis 连接池：通话状态缓存 + 知识库版本广播（多副本热更新）。"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        s = get_settings()
        _redis = aioredis.from_url(
            s.redis_url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=2, socket_keepalive=True,
            health_check_interval=30, max_connections=50,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
