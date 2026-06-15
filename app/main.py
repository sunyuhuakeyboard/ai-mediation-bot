"""FastAPI 应用入口。

启动装配（lifespan）：
- offline_mode=1：内置种子知识 + 内存状态，零外部依赖即可演示完整链路；
- 默认模式：PG（知识/运行数据）+ Redis（会话状态/热更新广播），
  自动建表，知识表为空时自动灌入Excel种子，并启动知识热更新订阅。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import ORJSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1 import admin, calls, cases, dialog
from app.cache.knowledge_cache import KnowledgeCache
from app.config import get_settings
from app.engines.call_state import StateStore
from app.engines.classifier import IntentClassifier
from app.engines.compliance import ComplianceEngine
from app.engines.llm_client import LLMClient
from app.engines.orchestrator import DialogOrchestrator
from app.services.call_service import CallService, MemRunStore
from app.services.quality_service import QualityService

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")


async def _ensure_seeded(session_factory) -> None:
    from sqlalchemy import func, select

    from app.db import models as M
    from app.knowledge import seed as S

    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(M.SopNode))).scalar()
        if count:
            return
        logger.info("knowledge tables empty -> seeding from built-in Excel mirror")
        pairs = [(M.SopNode, S.NODES), (M.IntentLabel, S.LABELS),
                 (M.DecisionRoute, S.ROUTES), (M.Strategy, S.STRATEGIES),
                 (M.ScriptTemplate, S.TEMPLATES), (M.PromptComponent, S.COMPONENTS),
                 (M.ComplianceRule, S.COMPLIANCE_RULES), (M.QcRule, S.QC_RULES)]
        for model, rows in pairs:
            for row in rows:
                await session.merge(model(**row))
        await session.merge(M.Case(**S.DEMO_CASE))
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.settings = s
    cache = KnowledgeCache()
    app.state.cache = cache

    classifier_http = httpx.AsyncClient() if s.classifier_url else None
    llm = None
    redis = None
    session_factory = None

    if s.offline_mode:
        cache.load_from_seed()
        state_store = StateStore(redis=None, ttl=s.call_state_ttl)
        call_service = CallService(s, state_store, session_factory=None, mem=MemRunStore())
        logger.info("running in OFFLINE mode (seed knowledge, in-memory state)")
    else:
        from app.db.models import Base
        from app.db.postgres import init_engine, session_factory as sf
        from app.db.redis_client import get_redis

        engine = init_engine()
        if s.auto_create_tables:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        session_factory = sf()
        if s.auto_seed_if_empty:
            await _ensure_seeded(session_factory)
        await cache.load_from_db(session_factory)

        redis = get_redis()
        await redis.ping()
        await cache.start_listener(redis, session_factory)
        state_store = StateStore(redis=redis, ttl=s.call_state_ttl)
        call_service = CallService(s, state_store, session_factory=session_factory)
        llm = LLMClient(s) if s.llm_api_key else None
        if llm is None:
            logger.warning("LLM_API_KEY 未配置：LLM_SHORT_REPLY 将全部回退参考话术模板")

    classifier = IntentClassifier(s, http=classifier_http)
    compliance = ComplianceEngine()
    orchestrator = DialogOrchestrator(cache, classifier, llm, compliance, s)
    quality = QualityService(s, call_service, cache, llm=llm)

    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.call_service = call_service
    app.state.quality_service = quality
    app.state.orchestrator = orchestrator
    logger.info("startup complete: knowledge version=%s", cache.version)

    try:
        yield
    finally:
        await cache.stop_listener()
        await call_service.drain()
        if llm is not None:
            await llm.aclose()
        if classifier_http is not None:
            await classifier_http.aclose()
        if not s.offline_mode:
            from app.db.postgres import dispose_engine
            from app.db.redis_client import close_redis
            await close_redis()
            await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="AI电话调解机器人", version="1.0.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan)

    api_prefix = "/api/v1"
    app.include_router(dialog.router, prefix=api_prefix)
    app.include_router(calls.router, prefix=api_prefix)
    app.include_router(cases.router, prefix=api_prefix)
    app.include_router(admin.router, prefix=api_prefix)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "knowledge_version": app.state.cache.version,
                "offline_mode": s.offline_mode}

    @app.get("/metrics")
    async def metrics_endpoint():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
