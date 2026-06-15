"""通话管理：结束 / 转写 / 质检报告。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_call_service, get_quality_service
from app.schemas.dialog import EndCallRequest

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/{call_id}/end")
async def end_call(call_id: str, req: EndCallRequest | None = None,
                   calls=Depends(get_call_service), qa=Depends(get_quality_service)):
    state = await calls.end_call(call_id, (req.result if req else None))
    if state is None:
        raise HTTPException(404, "call not found")
    await calls.drain()
    report = await qa.inspect(call_id, state)
    return {"call_id": call_id, "call_result": state.call_result, "quality": report}


@router.get("/{call_id}/transcript")
async def transcript(call_id: str, calls=Depends(get_call_service)):
    await calls.drain()
    turns = await calls.transcript(call_id)
    if not turns:
        raise HTTPException(404, "no turns for this call")
    return {"call_id": call_id, "turns": turns}


@router.get("/{call_id}/quality")
async def quality(call_id: str, calls=Depends(get_call_service),
                  qa=Depends(get_quality_service)):
    await calls.drain()
    state = await calls.load(call_id)
    report = await qa.inspect(call_id, state)
    return report


@router.get("/{call_id}/state")
async def call_state(call_id: str, calls=Depends(get_call_service)):
    state = await calls.load(call_id)
    if state is None:
        raise HTTPException(404, "call not found or expired")
    return {"call_id": state.call_id, "current_node": state.current_node,
            "slots": state.slots, "node_trail": state.node_trail,
            "call_result": state.call_result, "ended": state.ended,
            "transfer_human": state.transfer_human, "turn_index": state.turn_index}
