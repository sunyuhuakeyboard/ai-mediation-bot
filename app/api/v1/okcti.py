"""OKCTI / LLM-IVR SSE 兼容接口。

CTI 以 HTTP POST 调业务平台；本接口把 OKCTI 的 START/QA/LEAVE/END
事件适配到现有对话编排，并按 text/event-stream + [E-N=D] 返回。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_call_service, get_orchestrator, get_quality_service
from app.config import Settings
from app.schemas.okcti import OkctiRequest
from app.services.call_service import CallBlocked

router = APIRouter(prefix="/ivr/okcti", tags=["okcti"])

END_MARK = "[E-N=D]"
EVENT_IVR = "ivr"
EVENT_MSG = "msg"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _event_text(event: str, data: dict) -> str:
    return f"event:{event}\ndata:{_json(data)}{END_MARK}\n\n"


def _event_bytes(event: str, data: dict, charset: str) -> bytes:
    return _event_text(event, data).encode(charset or "UTF-8", errors="replace")


def _len(text: str) -> int:
    return len(text or "")


def _voice_interaction(s: Settings) -> dict:
    return {
        "interactmode": "voice",
        "keyallowstop": 1,
        "voiceallowstop": s.okcti_voice_allow_stop,
        "voiceblocktime": s.okcti_voice_block_time,
        "voicetimeout": s.okcti_voice_timeout,
        "voiceminspeak": s.okcti_voice_min_speak,
        "voiceminpause": s.okcti_voice_min_pause,
    }


def _minimal_ivr(req: OkctiRequest, *, grade: str = "", nodelabel: str = "",
                 nodescore: int = 0, think: str = "") -> dict:
    return {
        "callid": req.callid,
        "currentnodeid": "",
        "grade": grade,
        "isknowledge": 0,
        "lastnodeid": "",
        "nodelabel": nodelabel,
        "nodescore": nodescore,
        "think": think,
    }


def _split_text(text: str, limit: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    limit = max(20, int(limit or 80))
    parts = [p for p in re.split(r"(?<=[。！？!?；;])", text) if p]
    chunks: list[str] = []
    current = ""
    for part in parts or [text]:
        if len(current) + len(part) <= limit:
            current += part
            continue
        if current:
            chunks.append(current)
        while len(part) > limit:
            chunks.append(part[:limit])
            part = part[limit:]
        current = part
    if current:
        chunks.append(current)
    return chunks or [text]


def _segments(reply: str, segments: list[str], chunk_chars: int) -> list[str]:
    raw = [s.strip() for s in (segments or []) if s and s.strip()]
    if not raw:
        raw = _split_text(reply, chunk_chars)
    out: list[str] = []
    for item in raw:
        out.extend(_split_text(item, chunk_chars))
    return out or [reply or ""]


def _ivr_from_turn(req: OkctiRequest, result, s: Settings) -> tuple[dict, list[dict]]:
    """把内部 TurnResult 转为 OKCTI ivr + msg。"""
    if result.transfer_human:
        return {
            "callid": req.callid,
            "cmd": 3,
            "cmdcontent": s.okcti_transfer_skill,
            "cmdparams": "",
            "ttsspkname": s.okcti_tts_spk_name,
            "nodelabel": result.intent or "",
            "nodescore": 0,
            "lastnodeid": result.node_before or "",
            "currentnodeid": result.node_after or "",
            "think": result.action_type or "",
            "interaction": {},
        }, []

    pieces = _segments(result.reply, result.segments, s.okcti_msg_chunk_chars)
    first, rest = pieces[0], pieces[1:]
    cmd = 99 if result.end_call else 2
    ivr = {
        "callid": req.callid,
        "cmd": cmd,
        "cmdcontent": first,
        "cmdparams": "",
        "ttsspkname": s.okcti_tts_spk_name,
        "nodelabel": result.intent or "",
        "nodescore": 0,
        "lastnodeid": result.node_before or "",
        "currentnodeid": result.node_after or "",
        "think": result.action_type or "",
        "interaction": {} if result.end_call else _voice_interaction(s),
    }
    msgs: list[dict] = []
    for offset, msg in enumerate(rest, start=2):
        index = 9999 if offset == len(rest) + 1 else offset
        msgs.append({"callid": req.callid, "index": index, "len": _len(msg),
                     "msg": msg, "type": 1})
    return ivr, msgs


def _grade(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 95:
        return "A"
    if score >= 85:
        return "B"
    if score >= 75:
        return "C"
    if score >= 65:
        return "D"
    if score >= 55:
        return "E"
    if score >= 45:
        return "F"
    if score >= 30:
        return "G"
    return "H"


def _customer_phone(req: OkctiRequest) -> str:
    # direct: 0=呼入，1=呼出；外呼时被叫通常是客户。
    return req.callee if req.direct == 1 and req.callee else req.caller


async def _case_from_request(req: OkctiRequest, calls) -> dict:
    explicit_case = req.extra_value("case")
    if isinstance(explicit_case, dict):
        return dict(explicit_case)

    case_id = (req.extra_value("case_id") or req.calltaskid or "").strip()
    if case_id:
        found = await calls.get_case(case_id)
        if found:
            return found

    phone = _customer_phone(req)
    return {
        "case_id": case_id or req.callid,
        "debtor_name": req.extra_value("debtor_name") or req.extra_value("name") or "客户",
        "debtor_gender": req.extra_value("debtor_gender") or "",
        "debtor_phone": phone,
        "platform_name": req.extra_value("platform_name") or "相关平台",
        "creditor_name": req.extra_value("creditor_name") or "委托方",
        "mediation_org": req.extra_value("mediation_org") or "XX民商事调解中心",
        "official_verify_channel": req.extra_value("official_verify_channel") or "官方渠道",
        "total_amount": req.extra_value("total_amount"),
        "notice_status": req.extra_value("notice_status") or "",
        "extra": {
            "okcti": {
                "caller": req.caller,
                "callee": req.callee,
                "direct": req.direct,
                "taskid": req.taskid,
                "logid": req.logid,
                "calltaskid": req.calltaskid,
                "sysid": req.sysid,
            }
        },
    }


def _user_text(req: OkctiRequest) -> str:
    if req.usrcontent:
        return req.usrcontent
    if req.usrtype == 9:
        return "用户未回应"
    if req.usrtype == 8:
        return "识别异常"
    if req.usrtype == 7:
        return "按键错误"
    return ""


def _check_auth(s: Settings, x_request_id: str | None, x_app_id: str | None,
                x_timestamp: str | None, x_sign: str | None) -> None:
    if not s.okcti_auth_enabled:
        return
    if not all([x_request_id, x_app_id, x_timestamp, x_sign]):
        raise HTTPException(401, "missing OKCTI signature headers")
    if s.okcti_app_id and x_app_id != s.okcti_app_id:
        raise HTTPException(401, "invalid OKCTI app id")
    raw = f"{x_app_id};{x_timestamp};{s.okcti_app_secret};{x_request_id}"
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected.lower(), str(x_sign).lower()):
        raise HTTPException(401, "invalid OKCTI signature")


async def _handle_start(req: OkctiRequest, calls, orchestrator, s: Settings):
    existing = await calls.load(req.callid)
    if existing is not None:
        opening = await orchestrator.opening(existing)
        await calls.save_state(existing)
        return opening
    case = await _case_from_request(req, calls)
    try:
        state = await calls.start_call(case, call_id=req.callid, force=s.okcti_force_start)
    except CallBlocked as e:
        raise HTTPException(409, f"外呼被策略拦截[{e.kind}]：{e.reason}") from e
    opening = await orchestrator.opening(state)
    await calls.save_state(state)
    calls.persist_turn(state, opening, user_text=None)
    return opening


async def _handle_qa(req: OkctiRequest, calls, orchestrator, s: Settings):
    state = await calls.load(req.callid)
    if state is None:
        case = await _case_from_request(req, calls)
        state = await calls.start_call(case, call_id=req.callid, force=s.okcti_force_start)
        await orchestrator.opening(state)
    result = await orchestrator.handle_turn(state, _user_text(req))
    await calls.save_state(state)
    calls.persist_turn(state, result, req.usrcontent)
    return result


async def _handle_leave(req: OkctiRequest, calls) -> dict:
    await calls.end_call(req.callid, "离开IVR")
    return _minimal_ivr(req, nodelabel="LEAVE", think="LEAVE")


async def _handle_end(req: OkctiRequest, calls, qa) -> dict:
    result = "正常结束" if req.callresult in (None, 1) else f"未接通:{req.callresult}"
    state = await calls.end_call(req.callid, result)
    report: dict[str, Any] = {}
    if state is not None:
        await calls.drain()
        report = await qa.inspect(req.callid, state)
    return _minimal_ivr(req, grade=_grade(report.get("score")),
                        nodelabel="END", nodescore=int(report.get("score") or 0),
                        think=f"talktimelong={req.talktimelong or 0}")


async def _event_stream(req: OkctiRequest, calls, orchestrator, qa,
                        s: Settings) -> AsyncIterator[bytes]:
    charset = s.okcti_response_charset or "UTF-8"
    typ = req.type.upper()

    if typ == "START":
        result = await _handle_start(req, calls, orchestrator, s)
        ivr, msgs = _ivr_from_turn(req, result, s)
    elif typ == "QA":
        result = await _handle_qa(req, calls, orchestrator, s)
        ivr, msgs = _ivr_from_turn(req, result, s)
    elif typ == "LEAVE":
        ivr, msgs = await _handle_leave(req, calls), []
    elif typ == "END":
        ivr, msgs = await _handle_end(req, calls, qa), []
    else:
        ivr, msgs = {
            "callid": req.callid,
            "cmd": 99,
            "cmdcontent": f"暂不支持的IVR事件类型：{req.type}",
            "cmdparams": "",
            "ttsspkname": s.okcti_tts_spk_name,
            "nodelabel": "ERROR",
            "nodescore": 0,
            "lastnodeid": "",
            "currentnodeid": "",
            "think": "unsupported type",
            "interaction": {},
        }, []

    yield _event_bytes(EVENT_IVR, ivr, charset)
    for msg in msgs:
        yield _event_bytes(EVENT_MSG, msg, charset)


@router.post("/welcome")
@router.post("/welcome/stream")
async def okcti_welcome(
    req: OkctiRequest,
    request: Request,
    x_request_id: str | None = Header(default=None, alias="X-request-Id"),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_sign: str | None = Header(default=None, alias="X-Sign"),
    calls=Depends(get_call_service),
    orchestrator=Depends(get_orchestrator),
    qa=Depends(get_quality_service),
):
    s = _settings(request)
    _check_auth(s, x_request_id, x_app_id, x_timestamp, x_sign)
    media_type = f"text/event-stream; charset={s.okcti_response_charset or 'UTF-8'}"
    return StreamingResponse(_event_stream(req, calls, orchestrator, qa, s),
                             media_type=media_type)
