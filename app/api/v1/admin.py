"""知识运营接口：查看 / 维护 / 热更新。

业务同事更新话术后调用 POST /knowledge/reload：
重新从 PG 拉取并重建快照，同时通过 Redis 广播通知其他副本，秒级生效、无需重启。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.api.deps import get_app_settings, get_cache, get_call_service

router = APIRouter(prefix="/admin", tags=["admin"])

_TABLES = {
    "nodes": "nodes", "labels": "labels_by_id", "templates": "templates",
    "strategies": "strategies", "components": "components", "qc": "qc_rules",
}

_MODEL_MAP = {
    "templates": ("ScriptTemplate", "template_id"),
    "routes": ("DecisionRoute", "route_id"),
    "strategies": ("Strategy", "strategy_id"),
    "labels": ("IntentLabel", "label_id"),
    "nodes": ("SopNode", "node_id"),
}


@router.get("/knowledge/version")
async def knowledge_version(cache=Depends(get_cache)):
    snap = cache.snap()
    return {"version": snap.version, "counts": snap.counts}


@router.get("/knowledge/{table}")
async def knowledge_table(table: str, cache=Depends(get_cache)):
    snap = cache.snap()
    if table == "routes":
        return {"routes": [r for v in snap.routes_index.values() for r in v]}
    if table == "compliance":
        return {"dynamic": snap.compliance_dynamic,
                "static": [r for r, _ in snap.compliance_static]}
    attr = _TABLES.get(table)
    if attr is None:
        raise HTTPException(404, f"unknown table {table}; one of {sorted(_TABLES) + ['routes', 'compliance']}")
    data = getattr(snap, attr)
    return {table: data}


@router.put("/knowledge/{table}/{pk}")
async def upsert_knowledge(table: str, pk: str, request: Request,
                           payload: dict = Body(...),
                           settings=Depends(get_app_settings),
                           cache=Depends(get_cache)):
    if settings.offline_mode:
        raise HTTPException(400, "offline_mode 下知识只读（请使用数据库模式维护知识）")
    if table not in _MODEL_MAP:
        raise HTTPException(404, f"table must be one of {sorted(_MODEL_MAP)}")
    model_name, pk_field = _MODEL_MAP[table]
    from app.db import models as M
    model = getattr(M, model_name)
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        obj = await session.get(model, pk)
        created = obj is None
        if created:
            obj = model(**{pk_field: pk})
            session.add(obj)
        for k, v in payload.items():
            if k != pk_field and hasattr(obj, k):
                setattr(obj, k, v)
        await session.commit()
    return {"table": table, "pk": pk, "created": created,
            "hint": "调用 POST /api/v1/admin/knowledge/reload 使其生效"}


# ---------------- DNC 谢绝名单运营 ----------------
@router.get("/dnc")
async def list_dnc(calls=Depends(get_call_service)):
    return {"phones": await calls.list_dnc()}


@router.post("/dnc/{phone}")
async def add_dnc(phone: str, calls=Depends(get_call_service)):
    await calls.add_dnc(phone, reason="人工登记")
    return {"phone": phone, "added": True}


@router.delete("/dnc/{phone}")
async def remove_dnc(phone: str, calls=Depends(get_call_service)):
    await calls.remove_dnc(phone)
    return {"phone": phone, "removed": True}


@router.post("/knowledge/reload")
async def knowledge_reload(request: Request,
                           settings=Depends(get_app_settings),
                           cache=Depends(get_cache)):
    if settings.offline_mode:
        cache.load_from_seed()
        return {"version": cache.version, "mode": "seed"}
    session_factory = request.app.state.session_factory
    await cache.load_from_db(session_factory)
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        await cache.publish_reload(redis)   # 广播给其他副本
    return {"version": cache.version, "mode": "db", "broadcast": redis is not None}
