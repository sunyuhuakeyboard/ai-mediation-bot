"""OKCTI / LLM-IVR SSE 兼容接口。

CTI 以 HTTP POST 调业务平台；本接口把 OKCTI 的 START/QA/LEAVE/END
事件适配到现有对话编排，并按 text/event-stream + [E-N=D] 返回。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_call_service, get_orchestrator, get_quality_service
from app.config import Settings
from app.engines.orchestrator import TurnResult
from app.schemas.okcti import OkctiRequest
from app.services.call_service import CallBlocked

router = APIRouter(prefix="/ivr/okcti", tags=["okcti"])
logger = logging.getLogger(__name__)

END_MARK = "[E-N=D]"
EVENT_IVR = "ivr"
EVENT_MSG = "msg"
LOG_TEXT_LIMIT = 120


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


def _preview(text: Any, limit: int = LOG_TEXT_LIMIT) -> str:
    text = "" if text is None else str(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) < 7:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


def _state_summary(state) -> dict:
    if state is None:
        return {"exists": False}
    return {
        "exists": True,
        "node": state.current_node,
        "turn": state.turn_index,
        "ended": state.ended,
        "transfer": state.transfer_human,
        "slots": sorted(state.slots.keys()),
        "retries": dict(state.retries or {}),
        "history": len(state.history or []),
        "last_request": (state.okcti_last_request_key or "")[:12],
    }


def _ivr_summary(ivr: dict, msgs: list[dict]) -> dict:
    cmdcontent = ivr.get("cmdcontent") or ""
    return {
        "cmd": ivr.get("cmd"),
        "think": ivr.get("think"),
        "lastnode": ivr.get("lastnodeid"),
        "currentnode": ivr.get("currentnodeid"),
        "nodelabel": ivr.get("nodelabel"),
        "content_len": len(str(cmdcontent)),
        "content": _preview(cmdcontent),
        "msg_count": len(msgs or []),
        "msg_lens": [m.get("len") for m in (msgs or [])],
    }


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


def _last_bot_text(state) -> str:
    for role, text in reversed(state.history or []):
        if role == "bot" and text:
            return str(text)
    return ""


def _request_key(req: OkctiRequest) -> str:
    """OKCTI 可能重试同一轮 POST；用稳定字段生成幂等指纹。"""
    payload = {
        "type": req.type.upper(),
        "usrtype": req.usrtype,
        "usrcontent": (req.usrcontent or "").strip(),
        "usrrecurl": req.usrrecurl or "",
        "logid": req.logid or "",
        "taskid": req.taskid or "",
        "calltaskid": req.calltaskid or "",
        "talktimelong": req.talktimelong,
        "callresult": req.callresult,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached_response(state, key: str) -> tuple[dict, list[dict]] | None:
    if state is None or state.okcti_last_request_key != key:
        return None
    cached = state.okcti_last_response or {}
    ivr = cached.get("ivr")
    if not isinstance(ivr, dict):
        return None
    msgs = cached.get("msgs") or []
    if not isinstance(msgs, list):
        msgs = []
    return ivr, msgs


def _remember_response(state, key: str, ivr: dict, msgs: list[dict]) -> None:
    state.okcti_last_request_key = key
    state.okcti_last_response = {"ivr": ivr, "msgs": msgs}


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
    """把 bot 回复打包成 OKCTI 投递单元。短回复整体作为单段：
    避免 OKCTI 把 cmdcontent + msg 当成两段 TTS 接连播放，
    在某些 TTS 引擎上会被听成"每句话说两遍"。"""
    raw = [s.strip() for s in (segments or []) if s and s.strip()]
    if not raw:
        raw = _split_text(reply, chunk_chars)
    combined = "".join(raw)
    if combined and len(combined) <= chunk_chars:
        return [combined]
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


def _request_case_overrides(req: OkctiRequest, s: Settings) -> dict:
    raw = {
        "debtor_name": req.extra_value("debtor_name") or req.extra_value("name"),
        "respondent_name": req.extra_value("respondent_name") or req.extra_value("debtor_name") or req.extra_value("name"),
        "debtor_gender": req.extra_value("debtor_gender"),
        "platform_name": req.extra_value("platform_name"),
        "creditor_name": req.extra_value("creditor_name"),
        "mediation_org": req.extra_value("mediation_org"),
        "official_verify_channel": req.extra_value("official_verify_channel"),
        "total_amount": req.extra_value("total_amount"),
        "court_name": req.extra_value("court_name"),
        "court_contact": req.extra_value("court_contact"),
        "plaintiff_name": req.extra_value("plaintiff_name") or req.extra_value("creditor_name"),
        "lawsuit_type": req.extra_value("lawsuit_type"),
        "claim_amount": req.extra_value("claim_amount") or req.extra_value("total_amount"),
        "respondent_dir": req.extra_value("respondent_dir") or req.extra_value("address"),
        "notice_status": req.extra_value("notice_status"),
    }
    return {k: v for k, v in raw.items() if v not in (None, "")}


async def _case_from_request(req: OkctiRequest, calls, s: Settings) -> dict:
    explicit_case = req.extra_value("case")
    if isinstance(explicit_case, dict):
        return dict(explicit_case)

    case_id = (req.extra_value("case_id") or req.calltaskid or "").strip()
    if case_id:
        found = await calls.get_case(case_id)
        if found:
            found = dict(found)
            found.update(_request_case_overrides(req, s))
            return found

    phone = _customer_phone(req)
    return {
        "case_id": case_id or req.callid,
        "debtor_name": (req.extra_value("debtor_name") or req.extra_value("name")
                        or s.okcti_default_debtor_name),
        "respondent_name": (req.extra_value("respondent_name") or req.extra_value("debtor_name")
                            or req.extra_value("name") or s.edelivery_default_respondent_name),
        "debtor_gender": req.extra_value("debtor_gender") or "",
        "debtor_phone": phone,
        "platform_name": req.extra_value("platform_name") or s.okcti_default_platform_name,
        "creditor_name": req.extra_value("creditor_name") or s.okcti_default_creditor_name,
        "mediation_org": req.extra_value("mediation_org") or s.okcti_default_mediation_org,
        "official_verify_channel": req.extra_value("official_verify_channel") or "官方渠道",
        "total_amount": req.extra_value("total_amount"),
        "court_name": req.extra_value("court_name") or s.edelivery_default_court_name,
        "court_contact": req.extra_value("court_contact") or s.edelivery_default_court_contact,
        "plaintiff_name": (req.extra_value("plaintiff_name") or req.extra_value("creditor_name")
                           or s.edelivery_default_plaintiff_name),
        "lawsuit_type": req.extra_value("lawsuit_type") or s.edelivery_default_lawsuit_type,
        "claim_amount": req.extra_value("claim_amount") or req.extra_value("total_amount")
        or s.edelivery_default_claim_amount,
        "respondent_dir": (req.extra_value("respondent_dir") or req.extra_value("address")
                           or s.edelivery_default_respondent_dir),
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


# usrtype 7=按键错误 8=ASR识别异常 9=用户未回应 → 任何 usrcontent 都视为占位符
_NO_REAL_INPUT_USRTYPES = {7, 8, 9}
# 部分上游会以英文占位符填 usrcontent，需作为"无输入"处理而不是真实话语
_SYNTHETIC_ASR_HINTS = (
    "asr content always empty", "asr content empty",
    "content always empty", "asr empty", "no speech detected",
)


def _user_text(req: OkctiRequest) -> str:
    """从 OKCTI 请求中提取真实用户话语；占位符 / 无回应 / ASR 异常一律返回 ''."""
    if req.usrtype in _NO_REAL_INPUT_USRTYPES:
        return ""
    content = (req.usrcontent or "").strip()
    if not content:
        return ""
    if any(h in content.lower() for h in _SYNTHETIC_ASR_HINTS):
        logger.info("okcti synthetic asr placeholder ignored call=%s content=%r",
                    req.callid, _preview(content))
        return ""
    return content


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
        reply = _last_bot_text(existing)
        logger.info(
            "okcti start existing call=%s state=%s last_bot_len=%s last_bot=%r",
            req.callid, _state_summary(existing), len(reply), _preview(reply),
        )
        if not reply:
            opening = await orchestrator.opening(existing)
            await calls.save_state(existing)
            return opening, existing
        return TurnResult(call_id=existing.call_id, reply=reply, segments=[reply],
                          action_type="START_DUPLICATE",
                          node_before=existing.current_node,
                          node_after=existing.current_node,
                          slots=dict(existing.slots)), existing
    case = await _case_from_request(req, calls, s)
    logger.info(
        "okcti start new call=%s case_id=%s phone=%s force=%s taskid=%s calltaskid=%s logid=%s",
        req.callid, case.get("case_id"), _mask_phone(case.get("debtor_phone") or ""),
        s.okcti_force_start, req.taskid or "", req.calltaskid or "", req.logid or "",
    )
    try:
        state = await calls.start_call(case, call_id=req.callid, force=s.okcti_force_start)
    except CallBlocked as e:
        raise HTTPException(409, f"外呼被策略拦截[{e.kind}]：{e.reason}") from e
    opening = await orchestrator.opening(state)
    await calls.save_state(state)
    calls.persist_turn(state, opening, user_text=None)
    return opening, state


async def _handle_qa(req: OkctiRequest, calls, orchestrator, s: Settings):
    state = await calls.load(req.callid)
    if state is None:
        logger.warning(
            "okcti qa state_miss call=%s usertype=%s logid=%s user_len=%s user=%r",
            req.callid, req.usrtype, req.logid or "", len(_user_text(req)),
            _preview(_user_text(req)),
        )
        case = await _case_from_request(req, calls, s)
        state = await calls.start_call(case, call_id=req.callid, force=s.okcti_force_start)
        await orchestrator.opening(state)
    logger.info(
        "okcti qa handle call=%s state=%s usertype=%s user_len=%s user=%r recurl=%s",
        req.callid, _state_summary(state), req.usrtype, len(_user_text(req)),
        _preview(_user_text(req)), bool(req.usrrecurl),
    )
    result = await orchestrator.handle_turn(state, _user_text(req))
    await calls.save_state(state)
    calls.persist_turn(state, result, req.usrcontent)
    return result, state


async def _handle_leave(req: OkctiRequest, calls) -> dict:
    logger.info("okcti leave call=%s talktimelong=%s logid=%s",
                req.callid, req.talktimelong, req.logid or "")
    await calls.end_call(req.callid, "离开IVR")
    return _minimal_ivr(req, nodelabel="LEAVE", think="LEAVE")


async def _handle_end(req: OkctiRequest, calls, qa) -> dict:
    result = "正常结束" if req.callresult in (None, 1) else f"未接通:{req.callresult}"
    logger.info(
        "okcti end call=%s callresult=%s mapped_result=%s talktimelong=%s logid=%s",
        req.callid, req.callresult, result, req.talktimelong, req.logid or "",
    )
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
    started = time.perf_counter()
    charset = s.okcti_response_charset or "UTF-8"
    typ = req.type.upper()
    request_key = _request_key(req)
    state = await calls.load(req.callid)
    logger.info(
        "okcti request start call=%s type=%s usrtype=%s key=%s logid=%s taskid=%s calltaskid=%s "
        "direct=%s caller=%s callee=%s state=%s user_len=%s user=%r recurl=%s talktimelong=%s",
        req.callid, typ, req.usrtype, request_key[:12], req.logid or "", req.taskid or "",
        req.calltaskid or "", req.direct, _mask_phone(req.caller), _mask_phone(req.callee),
        _state_summary(state), len(_user_text(req)), _preview(_user_text(req)),
        bool(req.usrrecurl), req.talktimelong,
    )
    status = "ok"
    # 无 ASR 输入的事件（usrtype=7/8/9，或 usrcontent 为空）每次都需推进静音计数，
    # 不能被幂等缓存短路；只对有内容的真实回合启用 dedup。
    can_dedup = bool((req.usrcontent or "").strip()) and req.usrtype not in (7, 8, 9)
    try:
        cached = _cached_response(state, request_key) if can_dedup else None
        if cached is not None:
            ivr, msgs = cached
            logger.info(
                "okcti duplicate request replayed call=%s type=%s key=%s logid=%s response=%s",
                req.callid, typ, request_key[:12], req.logid or "", _ivr_summary(ivr, msgs),
            )
            yield _event_bytes(EVENT_IVR, ivr, charset)
            for msg in msgs:
                yield _event_bytes(EVENT_MSG, msg, charset)
            return

        state_to_cache = None

        if typ == "START":
            result, state_to_cache = await _handle_start(req, calls, orchestrator, s)
            ivr, msgs = _ivr_from_turn(req, result, s)
        elif typ == "QA":
            result, state_to_cache = await _handle_qa(req, calls, orchestrator, s)
            ivr, msgs = _ivr_from_turn(req, result, s)
        elif typ == "LEAVE":
            ivr, msgs = await _handle_leave(req, calls), []
        elif typ == "END":
            ivr, msgs = await _handle_end(req, calls, qa), []
        else:
            status = "unsupported"
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

        if state_to_cache is not None:
            if can_dedup:
                _remember_response(state_to_cache, request_key, ivr, msgs)
            await calls.save_state(state_to_cache)

        logger.info(
            "okcti response ready call=%s type=%s key=%s state_after=%s response=%s",
            req.callid, typ, request_key[:12], _state_summary(state_to_cache or state),
            _ivr_summary(ivr, msgs),
        )
        yield _event_bytes(EVENT_IVR, ivr, charset)
        for msg in msgs:
            yield _event_bytes(EVENT_MSG, msg, charset)
    except Exception:
        status = "error"
        logger.exception("okcti request failed call=%s type=%s key=%s logid=%s",
                         req.callid, typ, request_key[:12], req.logid or "")
        raise
    finally:
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("okcti request end call=%s type=%s key=%s status=%s elapsed_ms=%s",
                    req.callid, typ, request_key[:12], status, elapsed)


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
