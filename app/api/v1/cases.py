"""案件接口：批量导入（支持02表中文表头）/ 查询。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_call_service
from app.knowledge.seed import CASE_FIELD_CN_MAP
from app.schemas.dialog import CaseImportRequest

router = APIRouter(prefix="/cases", tags=["cases"])

_NUM_FIELDS = {"principal_amount": float, "total_amount": float, "overdue_days": int}


def normalize_case_row(row: dict) -> dict:
    out: dict = {}
    for k, v in row.items():
        key = CASE_FIELD_CN_MAP.get(str(k).strip(), str(k).strip())
        if v in (None, ""):
            continue
        if key in _NUM_FIELDS:
            try:
                v = _NUM_FIELDS[key](v)
            except (TypeError, ValueError):
                continue
        out[key] = v
    return out


@router.post("/import")
async def import_cases(req: CaseImportRequest, calls=Depends(get_call_service)):
    rows = [normalize_case_row(r) for r in req.rows]
    rows = [r for r in rows if r.get("case_id")]
    if not rows:
        raise HTTPException(400, "no valid rows (case_id/案件编号 required)")
    n = await calls.upsert_cases(rows)
    return {"imported": n}


@router.get("/{case_id}")
async def get_case(case_id: str, calls=Depends(get_call_service)):
    case = await calls.get_case(case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    return case
