"""通话生命周期服务：开始 / 轮次落库（后台任务，不占回合延迟）/ 结束触发质检。

offline_mode 下使用进程内 MemRunStore，行为与 PG 模式一致，便于演示与单测。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from app.observability import metrics

from app.config import Settings
from app.engines.call_state import CallState, StateStore
from app.engines.orchestrator import TurnResult

logger = logging.getLogger(__name__)


class CallBlocked(Exception):
    """外呼被策略拦截（勿扰时段/谢绝名单/频次上限）。"""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


class MemRunStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.turns: dict[str, list[dict]] = {}
        self.reports: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.dnc: dict[str, dict] = {}
        self.freq: dict[str, int] = {}


class CallService:
    def __init__(self, settings: Settings, state_store: StateStore,
                 session_factory=None, mem: MemRunStore | None = None) -> None:
        self.s = settings
        self.states = state_store
        self.session_factory = session_factory
        self.mem = mem or MemRunStore()
        self._bg: set[asyncio.Task] = set()

    # ---------------- 案件 ----------------
    async def get_case(self, case_id: str) -> dict | None:
        if self.s.offline_mode or self.session_factory is None:
            if case_id in self.mem.cases:
                return self.mem.cases[case_id]
            from app.knowledge.seed import DEMO_CASE
            return dict(DEMO_CASE) if case_id == DEMO_CASE["case_id"] else None
        from app.db.models import Case
        async with self.session_factory() as session:
            row = await session.get(Case, case_id)
            if row is None:
                return None
            d = {c.key: getattr(row, c.key) for c in row.__table__.columns}
            for f in ("principal_amount", "total_amount"):
                if d.get(f) is not None:
                    d[f] = float(d[f])
            d.pop("created_at", None)
            d.pop("updated_at", None)
            return d

    async def upsert_cases(self, rows: list[dict]) -> int:
        if self.s.offline_mode or self.session_factory is None:
            for r in rows:
                if r.get("case_id"):
                    self.mem.cases[r["case_id"]] = r
            return len(rows)
        from app.db.models import Case
        count = 0
        async with self.session_factory() as session:
            for r in rows:
                if not r.get("case_id"):
                    continue
                obj = await session.get(Case, r["case_id"])
                if obj is None:
                    obj = Case(case_id=r["case_id"])
                    session.add(obj)
                for k, v in r.items():
                    if k != "case_id" and hasattr(obj, k):
                        setattr(obj, k, v)
                count += 1
            await session.commit()
        return count

    # ---------------- 外呼策略（商用合规） ----------------
    @staticmethod
    def _freq_key(phone: str, now: datetime) -> str:
        return f"callfreq:{phone}:{now:%Y%m%d}"

    async def is_dnc(self, phone: str) -> bool:
        if not phone:
            return False
        if self.states.redis is not None:
            return bool(await self.states.redis.sismember("dnc:phones", phone))
        return phone in self.mem.dnc

    async def add_dnc(self, phone: str, reason: str = "", call_id: str | None = None) -> None:
        if not phone:
            return
        entry = dict(phone=phone, reason=reason, source_call_id=call_id)
        if self.states.redis is not None:
            await self.states.redis.sadd("dnc:phones", phone)
        else:
            self.mem.dnc[phone] = entry
        if not self.s.offline_mode and self.session_factory is not None:
            try:
                from app.db.models import DncEntry
                async with self.session_factory() as session:
                    obj = await session.get(DncEntry, phone)
                    if obj is None:
                        session.add(DncEntry(**entry))
                        await session.commit()
            except Exception:
                logger.exception("persist dnc failed %s", phone)
        logger.info("DNC enrolled phone=%s reason=%s", phone, reason)

    async def remove_dnc(self, phone: str) -> None:
        if self.states.redis is not None:
            await self.states.redis.srem("dnc:phones", phone)
        else:
            self.mem.dnc.pop(phone, None)
        if not self.s.offline_mode and self.session_factory is not None:
            try:
                from app.db.models import DncEntry
                async with self.session_factory() as session:
                    obj = await session.get(DncEntry, phone)
                    if obj is not None:
                        await session.delete(obj)
                        await session.commit()
            except Exception:
                logger.exception("remove dnc failed %s", phone)

    async def list_dnc(self) -> list:
        if self.states.redis is not None:
            return sorted(await self.states.redis.smembers("dnc:phones"))
        return sorted(self.mem.dnc)

    async def check_outbound_policy(self, phone: str | None, now: datetime | None = None) -> None:
        """勿扰时段 / DNC谢绝名单 / 当日频次。违规抛 CallBlocked。"""
        now = now or datetime.now()
        start_h, end_h = self.s.dnd_start_hour, self.s.dnd_end_hour
        in_dnd = (now.hour >= start_h or now.hour < end_h) if start_h > end_h \
            else (start_h <= now.hour < end_h)
        if in_dnd:
            metrics.CALLS_BLOCKED.labels(reason="dnd").inc()
            raise CallBlocked("dnd", f"勿扰时段（{start_h}:00-{end_h}:00）禁止外呼")
        if not phone:
            return
        if await self.is_dnc(phone):
            metrics.CALLS_BLOCKED.labels(reason="dnc").inc()
            raise CallBlocked("dnc", "该号码已登记谢绝来电，禁止外呼")
        key = self._freq_key(phone, now)
        if self.states.redis is not None:
            count = int(await self.states.redis.get(key) or 0)
        else:
            count = self.mem.freq.get(key, 0)
        if count >= self.s.daily_call_limit:
            metrics.CALLS_BLOCKED.labels(reason="freq").inc()
            raise CallBlocked("freq", f"该号码当日外呼已达上限（{self.s.daily_call_limit}次）")
        if self.states.redis is not None:
            pipe = self.states.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 86400)
            await pipe.execute()
        else:
            self.mem.freq[key] = count + 1

    # ---------------- 通话 ----------------
    async def start_call(self, case: dict, call_id: str | None = None, *,
                         force: bool = False, now: datetime | None = None) -> CallState:
        if self.s.outbound_policy_enabled and not force:
            await self.check_outbound_policy((case or {}).get("debtor_phone"), now)
        call_id = call_id or f"CALL{uuid.uuid4().hex[:16]}"
        state = CallState(call_id=call_id, case=case)
        await self.states.save(state)
        self._spawn(self._persist_session(state, status="进行中"))
        return state

    async def load(self, call_id: str) -> CallState | None:
        return await self.states.get(call_id)

    async def save_state(self, state: CallState) -> None:
        await self.states.save(state)

    def persist_turn(self, state: CallState, result: TurnResult, user_text: str | None) -> None:
        """后台落库（写路径不阻塞回合）。"""
        self._spawn(self._persist_turn(state, result, user_text))
        if result.end_call or result.transfer_human:
            self._spawn(self._persist_session(state, status="转人工" if result.transfer_human else "已结束",
                                              ended=True))

    async def end_call(self, call_id: str, result: str | None = None) -> CallState | None:
        state = await self.states.get(call_id)
        if state is None:
            return None
        state.ended = True
        state.call_result = result or state.call_result or "正常结束"
        await self.states.save(state)
        await self._persist_session(state, status="已结束", ended=True)
        return state

    async def transcript(self, call_id: str) -> list[dict]:
        if self.s.offline_mode or self.session_factory is None:
            return list(self.mem.turns.get(call_id, []))
        from sqlalchemy import select

        from app.db.models import CallTurn
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(CallTurn).where(CallTurn.call_id == call_id)
                .order_by(CallTurn.turn_index))).scalars().all()
            return [{c.key: getattr(r, c.key) for c in r.__table__.columns
                     if c.key not in ("id", "created_at")} for r in rows]

    # ---------------- 内部持久化 ----------------
    async def _persist_session(self, state: CallState, status: str, ended: bool = False) -> None:
        try:
            if self.s.offline_mode or self.session_factory is None:
                self.mem.sessions[state.call_id] = dict(
                    call_id=state.call_id, case_id=(state.case or {}).get("case_id"),
                    status=status, current_node=state.current_node, slots=dict(state.slots),
                    call_result=state.call_result, risk_flags=list(state.risk_flags),
                    transfer_human=state.transfer_human)
                return
            from app.db.models import CallSession
            async with self.session_factory() as session:
                obj = await session.get(CallSession, state.call_id)
                if obj is None:
                    obj = CallSession(call_id=state.call_id)
                    session.add(obj)
                obj.case_id = (state.case or {}).get("case_id")
                obj.status = status
                obj.current_node = state.current_node
                obj.slots = dict(state.slots)
                obj.call_result = state.call_result
                obj.risk_flags = list(state.risk_flags)
                obj.transfer_human = state.transfer_human
                if ended:
                    obj.ended_at = datetime.now(timezone.utc)
                await session.commit()
        except Exception:
            logger.exception("persist session failed call=%s", state.call_id)
        finally:
            if ended and state.slots.get("dnc_request"):
                phone = (state.case or {}).get("debtor_phone")
                if phone:
                    await self.add_dnc(phone, "用户要求停止联系", state.call_id)

    async def _persist_turn(self, state: CallState, r: TurnResult, user_text: str | None) -> None:
        row = dict(call_id=r.call_id, turn_index=state.turn_index, user_text=user_text,
                   intent_label=r.intent, objection_label=r.objection, emotion_label=r.emotion,
                   risk_level=r.risk, confidence=r.confidence, route_id=r.route_id,
                   action_type=r.action_type, node_before=r.node_before, node_after=r.node_after,
                   bot_reply=r.reply, llm_used=r.llm_used, compliance=r.compliance,
                   latency_ms=r.latency_ms)
        try:
            if self.s.offline_mode or self.session_factory is None:
                self.mem.turns.setdefault(r.call_id, []).append(row)
                return
            from app.db.models import CallTurn
            async with self.session_factory() as session:
                session.add(CallTurn(**row))
                await session.commit()
        except Exception:
            logger.exception("persist turn failed call=%s", r.call_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """等待后台写任务完成（测试/优雅停机用）。"""
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
