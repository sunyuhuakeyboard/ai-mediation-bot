"""对话接口。

- POST /api/v1/dialog/turn ：单回合（ASR最终文本进，话术出），落库走后台任务；
- WS   /api/v1/dialog/ws   ：电话网关长连接协议
    上行: {"event":"start","case_id":"..."} | {"event":"user_text","text":"..."}
          | {"event":"barge_in"} | {"event":"end"}
    下行: {"event":"bot_reply", ...TurnResponse字段}
  barge_in（用户抢话）会取消在途的生成任务，符合电话场景实时打断需求。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.api.deps import get_call_service, get_orchestrator
from app.services.call_service import CallBlocked
from app.schemas.dialog import (StartCallRequest, StartCallResponse, TurnRequest,
                                TurnResponse, to_turn_response)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dialog", tags=["dialog"])


async def _start(calls, orchestrator, req: StartCallRequest) -> tuple:
    case = req.case
    if case is None and req.case_id:
        case = await calls.get_case(req.case_id)
    if case is None:
        raise HTTPException(404, "case not found; provide case_id or inline case")
    try:
        state = await calls.start_call(case, call_id=req.call_id, force=req.force)
    except CallBlocked as e:
        raise HTTPException(409, f"外呼被策略拦截[{e.kind}]：{e.reason}") from e
    opening = await orchestrator.opening(state)
    await calls.save_state(state)
    calls.persist_turn(state, opening, user_text=None)
    return state, opening


@router.post("/start", response_model=StartCallResponse)
async def start_dialog(req: StartCallRequest,
                       calls=Depends(get_call_service),
                       orchestrator=Depends(get_orchestrator)):
    state, opening = await _start(calls, orchestrator, req)
    return StartCallResponse(call_id=state.call_id,
                             case_id=(state.case or {}).get("case_id"),
                             opening=to_turn_response(opening))


@router.post("/turn", response_model=TurnResponse)
async def dialog_turn(req: TurnRequest,
                      calls=Depends(get_call_service),
                      orchestrator=Depends(get_orchestrator)):
    state = await calls.load(req.call_id)
    if state is None:
        raise HTTPException(404, f"call {req.call_id} not found or expired")
    result = await orchestrator.handle_turn(state, req.text)
    await calls.save_state(state)              # 1次Redis写
    calls.persist_turn(state, result, req.text)  # PG落库后台执行
    return to_turn_response(result)


@router.websocket("/ws")
async def dialog_ws(ws: WebSocket):
    await ws.accept()
    calls = ws.app.state.call_service
    orchestrator = ws.app.state.orchestrator
    state = None
    inflight: asyncio.Task | None = None

    async def run_turn(text: str):
        nonlocal state
        result = await orchestrator.handle_turn(state, text)
        await calls.save_state(state)
        calls.persist_turn(state, result, text)
        await ws.send_json({"event": "bot_reply",
                            **to_turn_response(result).model_dump()})

    try:
        while True:
            msg = await ws.receive_json()
            event = msg.get("event")
            if event == "start":
                req = StartCallRequest(case_id=msg.get("case_id"),
                                       case=msg.get("case"), call_id=msg.get("call_id"))
                try:
                    state, opening = await _start(calls, orchestrator, req)
                except HTTPException as e:
                    await ws.send_json({"event": "error", "detail": e.detail})
                    continue
                await ws.send_json({"event": "bot_reply", "call_id": state.call_id,
                                    **to_turn_response(opening).model_dump()})
            elif event == "user_text":
                if state is None:
                    await ws.send_json({"event": "error", "detail": "send start first"})
                    continue
                if inflight and not inflight.done():   # 抢话：取消在途生成
                    inflight.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await inflight
                inflight = asyncio.create_task(run_turn(msg.get("text") or ""))
            elif event == "barge_in":
                if inflight and not inflight.done():
                    inflight.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await inflight
                    await ws.send_json({"event": "cancelled"})
            elif event == "end":
                if state is not None:
                    await calls.end_call(state.call_id)
                await ws.send_json({"event": "ended"})
                break
            else:
                await ws.send_json({"event": "error", "detail": f"unknown event {event}"})
    except WebSocketDisconnect:
        logger.info("ws disconnected call=%s", state.call_id if state else "-")
    finally:
        if inflight and not inflight.done():
            inflight.cancel()
