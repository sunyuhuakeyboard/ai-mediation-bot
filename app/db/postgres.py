"""异步 PostgreSQL 引擎与会话工厂（写路径全部走后台任务，不占用对话回合延迟）。"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.pg_dsn, pool_size=s.pg_pool_size, max_overflow=10,
            pool_pre_ping=True, pool_recycle=1800, echo=s.debug,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine, _session_factory = None, None
