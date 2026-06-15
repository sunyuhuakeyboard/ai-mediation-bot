"""知识库 L1 内存快照 + Redis 热更新广播。

设计目标（对应方案 §低时延）：
- 决策表/话术/策略/组件全部常驻内存，单次路由匹配 < 1ms；
- 快照整体替换（immutable swap），读路径无锁；
- 知识在 PG 中维护，更新后通过 Redis pubsub 通知所有副本重建快照（秒级生效）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _row_to_dict(obj: Any) -> dict:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


@dataclass
class Snapshot:
    version: str
    nodes: dict[str, dict] = field(default_factory=dict)
    labels: list[dict] = field(default_factory=list)              # 全局优先标签排前
    labels_by_id: dict[str, dict] = field(default_factory=dict)
    routes_index: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    templates: dict[str, dict] = field(default_factory=dict)
    strategies: dict[str, dict] = field(default_factory=dict)
    components: dict[str, dict] = field(default_factory=dict)
    compliance_static: list[tuple[dict, list[re.Pattern]]] = field(default_factory=list)
    compliance_dynamic: list[dict] = field(default_factory=list)
    qc_rules: dict[str, dict] = field(default_factory=dict)
    slot_questions: dict[str, str] = field(default_factory=dict)
    polar_map: dict[str, dict[str, str]] = field(default_factory=dict)
    label_effects: dict[str, dict] = field(default_factory=dict)
    no_end_append: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)


def build_snapshot(*, nodes: list[dict], labels: list[dict], routes: list[dict],
                   strategies: list[dict], templates: list[dict], components: list[dict],
                   compliance_rules: list[dict], qc_rules: list[dict],
                   slot_questions: dict, polar_map: dict, label_effects: dict,
                   no_end_append: set) -> Snapshot:
    snap = Snapshot(version=time.strftime("%Y%m%d%H%M%S"))

    snap.nodes = {n["node_id"]: dict(n) for n in nodes}
    snap.labels_by_id = {l["label_id"]: dict(l) for l in labels}
    snap.labels = sorted((dict(l) for l in labels),
                         key=lambda l: (not l.get("global_priority"),))

    idx: dict[tuple[str, str], list[dict]] = {}
    for r in routes:
        if not r.get("enabled", True):
            continue
        idx.setdefault((r["current_node"], r["intent_label"]), []).append(dict(r))
    for v in idx.values():
        v.sort(key=lambda r: r.get("priority", 50), reverse=True)
    snap.routes_index = idx

    snap.templates = {t["template_id"]: dict(t) for t in templates if t.get("enabled", True)}
    snap.strategies = {s["strategy_id"]: dict(s) for s in strategies}
    snap.components = {c["component_id"]: dict(c) for c in components if c.get("enabled", True)}

    static, dynamic = [], []
    for r in compliance_rules:
        if not r.get("enabled", True):
            continue
        r = dict(r)
        if r.get("dynamic_kind"):
            dynamic.append(r)
        elif r.get("intercept", True):
            pats = []
            for kw in r.get("trigger_keywords") or []:
                if kw:
                    with contextlib.suppress(re.error):
                        pats.append(re.compile(re.escape(kw)))
            if pats:
                static.append((r, pats))
    # 动态规则在前（隐私优先级最高），静态按规则ID序
    static.sort(key=lambda x: x[0]["rule_id"])
    dynamic.sort(key=lambda r: r["rule_id"])
    snap.compliance_static, snap.compliance_dynamic = static, dynamic

    snap.qc_rules = {q["qc_id"]: dict(q) for q in qc_rules}
    snap.slot_questions = dict(slot_questions)
    snap.polar_map = {k: dict(v) for k, v in polar_map.items()}
    snap.label_effects = {k: dict(v) for k, v in label_effects.items()}
    snap.no_end_append = set(no_end_append)
    from app.knowledge.validate import validate_refs
    for issue in validate_refs(nodes, routes, strategies=strategies,
                               templates=templates, components=components):
        logger.warning("knowledge integrity: %s", issue)

    snap.counts = dict(nodes=len(snap.nodes), labels=len(snap.labels),
                       routes=sum(len(v) for v in idx.values()),
                       templates=len(snap.templates), strategies=len(snap.strategies),
                       components=len(snap.components),
                       compliance=len(static) + len(dynamic), qc=len(snap.qc_rules))
    return snap


class KnowledgeCache:
    """进程内单例：持有当前快照；支持从种子/数据库构建与热更新。"""

    def __init__(self) -> None:
        self._snap: Snapshot | None = None
        self._listener_task: asyncio.Task | None = None

    # ---------- 读 ----------
    def snap(self) -> Snapshot:
        if self._snap is None:
            raise RuntimeError("knowledge snapshot not loaded")
        return self._snap

    @property
    def version(self) -> str:
        return self._snap.version if self._snap else "-"

    # ---------- 构建 ----------
    def load_from_seed(self) -> Snapshot:
        from app.knowledge import seed as S
        self._snap = build_snapshot(
            nodes=S.NODES, labels=S.LABELS, routes=S.ROUTES, strategies=S.STRATEGIES,
            templates=S.TEMPLATES, components=S.COMPONENTS,
            compliance_rules=S.COMPLIANCE_RULES, qc_rules=S.QC_RULES,
            slot_questions=S.SLOT_QUESTIONS, polar_map=S.NODE_POLAR_MAP,
            label_effects=S.LABEL_SLOT_EFFECTS, no_end_append=S.NO_END_APPEND)
        logger.info("knowledge loaded from seed: %s", self._snap.counts)
        return self._snap

    async def load_from_db(self, session_factory) -> Snapshot:
        from sqlalchemy import select

        from app.db import models as M
        from app.knowledge import seed as S

        async with session_factory() as session:
            async def all_of(model):
                rows = (await session.execute(select(model))).scalars().all()
                return [_row_to_dict(r) for r in rows]

            nodes = await all_of(M.SopNode)
            labels = await all_of(M.IntentLabel)
            routes = await all_of(M.DecisionRoute)
            strategies = await all_of(M.Strategy)
            templates = await all_of(M.ScriptTemplate)
            components = await all_of(M.PromptComponent)
            compliance = await all_of(M.ComplianceRule)
            qc = await all_of(M.QcRule)

        self._snap = build_snapshot(
            nodes=nodes, labels=labels, routes=routes, strategies=strategies,
            templates=templates, components=components, compliance_rules=compliance,
            qc_rules=qc, slot_questions=S.SLOT_QUESTIONS, polar_map=S.NODE_POLAR_MAP,
            label_effects=S.LABEL_SLOT_EFFECTS, no_end_append=S.NO_END_APPEND)
        logger.info("knowledge loaded from db: %s", self._snap.counts)
        return self._snap

    # ---------- 热更新 ----------
    async def publish_reload(self, redis) -> None:
        await redis.publish(self._channel(), "reload")

    async def start_listener(self, redis, session_factory) -> None:
        """订阅知识变更频道；收到消息后重建快照（多副本同时生效）。"""
        async def _loop():
            pubsub = redis.pubsub()
            await pubsub.subscribe(self._channel())
            logger.info("knowledge reload listener started on %s", self._channel())
            try:
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        await self.load_from_db(session_factory)
                        logger.info("knowledge hot-reloaded, version=%s", self.version)
                    except Exception:
                        logger.exception("knowledge reload failed")
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe(self._channel())
                    await pubsub.aclose()

        self._listener_task = asyncio.create_task(_loop(), name="knowledge-listener")

    async def stop_listener(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

    @staticmethod
    def _channel() -> str:
        from app.config import get_settings
        return get_settings().knowledge_channel
