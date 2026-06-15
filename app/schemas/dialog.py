"""API 请求/响应模型（pydantic v2）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class StartCallRequest(BaseModel):
    case_id: str | None = None
    case: dict | None = None          # 也可直接内联案件字段（演示/外呼对接）
    call_id: str | None = None
    force: bool = False               # 跳过外呼策略（人工坐席强呼/呼入场景）


class TurnRequest(BaseModel):
    call_id: str
    text: str = Field(min_length=1, max_length=2000)


class TurnResponse(BaseModel):
    call_id: str
    reply: str
    segments: list[str] = []
    intent: str = "UNKNOWN"
    objection: str | None = None
    emotion: str = "平稳"
    risk: str = "低"
    confidence: float = 0.0
    action_type: str = ""
    route_id: str | None = None
    node_before: str = ""
    node_after: str = ""
    slots: dict = {}
    end_call: bool = False
    transfer_human: bool = False
    llm_used: bool = False
    call_result: str | None = None
    compliance: dict = {}
    latency_ms: dict = {}


class StartCallResponse(BaseModel):
    call_id: str
    case_id: str | None = None
    opening: TurnResponse


class EndCallRequest(BaseModel):
    result: str | None = None


class CaseImportRequest(BaseModel):
    rows: list[dict]


def to_turn_response(r) -> TurnResponse:
    return TurnResponse(**{k: getattr(r, k) for k in TurnResponse.model_fields})
