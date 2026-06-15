"""通话质检服务（对应 10_通话质检规则 QC001~QC010）。

输入：通话轮次记录 + 终态会话；输出：结构化质检报告（得分/扣分项/是否人工复核），
得分 = 100 - Σ命中规则扣分（扣分值取自质检规则表，知识可热更新）。
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.engines.call_state import CallState

logger = logging.getLogger(__name__)

_PRIVACY_RULES = {"CR001", "CR005"}
_EXPRESSION_RULES = {"CR002", "CR003", "CR004", "CR009", "CR010"}
_HOT_INTENTS = {"COMPLAINT_THREAT", "ABUSIVE_LANGUAGE"}
_RISK_INTENTS = {"COMPLAINT_THREAT", "REQUEST_HUMAN"}


class QualityService:
    def __init__(self, settings: Settings, call_service, cache, llm=None) -> None:
        self.s = settings
        self.calls = call_service
        self.cache = cache
        self.llm = llm

    async def inspect(self, call_id: str, state: CallState | None = None) -> dict:
        snap = self.cache.snap()
        turns = await self.calls.transcript(call_id)
        state = state or await self.calls.load(call_id)
        slots = (state.slots if state else {}) or {}
        trail = list(state.node_trail) if state else []
        for t in turns:
            for nid in (t.get("node_before"), t.get("node_after")):
                if nid and nid not in trail:
                    trail.append(nid)

        deductions: list[dict] = []
        need_review = False

        def hit(qc_id: str, detail: str) -> None:
            nonlocal need_review
            rule = snap.qc_rules.get(qc_id, {})
            deductions.append({"qc_id": qc_id, "dimension": rule.get("dimension"),
                               "deduct": rule.get("deduct", 0), "detail": detail})
            if rule.get("manual_review"):
                need_review = True

        violations = [v for t in turns
                      for v in ((t.get("compliance") or {}).get("violations") or [])]
        vio_ids = {v.get("rule_id") for v in violations}

        # QC001 隐私：未确认本人披露敏感信息（命中CR001/CR005即严重违规）
        if vio_ids & _PRIVACY_RULES:
            hit("QC001", f"命中隐私规则: {sorted(vio_ids & _PRIVACY_RULES)}")
        # QC002 SOP完整性：身份确认 + 意愿确认 + 总结/结束
        ended_ok = ("N023" in trail) or ("N024" in trail) or ("N025" in trail)
        if not ("N002" in trail and ("N009" in trail or "N005" in trail or "N025" in trail) and ended_ok):
            hit("QC002", f"关键节点缺失, trail={trail}")
        # QC003 是否询问调解意愿（非本人/早转人工除外）
        if "N009" not in trail and not ({"N005", "N025"} & set(trail)):
            hit("QC003", "未经过意愿确认节点N009")
        # QC004 抗性识别置信度
        low_conf = [t for t in turns if (t.get("confidence") or 1.0) < 0.6]
        if len(low_conf) >= 2:
            hit("QC004", f"{len(low_conf)}轮低置信度识别，建议人工复核")
        # QC005 有意愿但未收集方案要素
        if slots.get("willingness") and not (
                (slots.get("repayment_amount") and slots.get("repayment_date"))
                or (slots.get("installment_count") and slots.get("installment_amount"))
                or slots.get("callback_time")):
            hit("QC005", "用户有意愿但缺少方案/回访要素")
        # QC006 合规表达：威胁/冒充/承诺等
        if vio_ids & _EXPRESSION_RULES:
            hit("QC006", f"命中表达类合规规则: {sorted(vio_ids & _EXPRESSION_RULES)}")
        # QC007 情绪处理：激烈情绪后是否降温/转人工
        hot = any(t.get("intent_label") in _HOT_INTENTS or t.get("emotion_label") == "激动"
                  for t in turns)
        if hot and "N025" not in trail and not (state and state.ended):
            hit("QC007", "用户情绪激烈但未降温结束或转人工")
        # QC008 高风险是否转人工
        risky = any(t.get("intent_label") in _RISK_INTENTS for t in turns)
        if risky and "N025" not in trail:
            hit("QC008", "出现高风险意图但未进入转人工节点")
        # QC009 结果归档
        call_result = (state.call_result if state else None)
        if not call_result:
            hit("QC009", "缺少通话结果归档字段")
        # QC010 响应延迟
        slow = [t for t in turns if ((t.get("latency_ms") or {}).get("total") or 0) > self.s.latency_warn_ms]
        if slow:
            hit("QC010", f"{len(slow)}轮响应超过{self.s.latency_warn_ms}ms")
        # LLM深度质检（规则引擎盲区兜底：同义改写的威胁/承诺等；离线/未配LLM自动跳过）
        audit_issues = await self._llm_audit(turns)
        if audit_issues:
            hit("QC006", "LLM深度质检发现疑似违规：" + "；".join(audit_issues[:5]))

        score = max(0, 100 - sum(d["deduct"] for d in deductions))
        plan = {k: slots.get(k) for k in ("repayment_amount", "repayment_date",
                                          "installment_count", "installment_amount",
                                          "callback_time") if slots.get(k)}
        objections = [t.get("objection_label") for t in turns if t.get("objection_label")]
        report = dict(
            call_id=call_id, score=score,
            identity_confirmed=bool(slots.get("identity_confirmed")),
            mediation_willingness=slots.get("mediation_willingness"),
            main_objection=objections[0] if objections else None,
            repayment_plan=plan,
            risk_flags=sorted(set((state.risk_flags if state else []) or [])),
            deductions=deductions, need_human_review=need_review,
            call_result=call_result,
        )
        await self._persist(report)
        return report

    async def _llm_audit(self, turns: list[dict]) -> list[str]:
        if self.llm is None or not self.s.llm_audit_enabled:
            return []
        lines = [t.get("bot_reply") or "" for t in turns][: self.s.llm_audit_max_lines]
        numbered = chr(10).join(f"{i + 1}. {x}" for i, x in enumerate(lines) if x)
        if not numbered:
            return []
        prompt = ("你是电话调解合规质检员。以下是机器人在一通电话中说过的话，"
                  "找出存在威胁恐吓、冒充司法机关、承诺减免或处理结果、泄露隐私、"
                  "施压立即还款的句子。只输出JSON数组，每项格式"
                  '{"line":序号,"problem":"问题"}，没有问题输出[]。\n' + numbered)
        try:
            import asyncio

            import orjson
            async with asyncio.timeout(8):
                out = await self.llm.complete([{"role": "user", "content": prompt}])
            if not out:
                return []
            data = orjson.loads(out.replace("```json", "").replace("```", "").strip())
            return [f"第{i.get('line')}句:{i.get('problem')}" for i in data if isinstance(i, dict)]
        except Exception:
            logger.warning("llm audit skipped", exc_info=True)
            return []

    async def _persist(self, report: dict) -> None:
        try:
            if self.s.offline_mode or self.calls.session_factory is None:
                self.calls.mem.reports[report["call_id"]] = report
                return
            from app.db.models import QualityReport
            async with self.calls.session_factory() as session:
                obj = await session.get(QualityReport, report["call_id"])
                if obj is None:
                    obj = QualityReport(call_id=report["call_id"])
                    session.add(obj)
                for k, v in report.items():
                    if k != "call_id" and hasattr(obj, k):
                        setattr(obj, k, v)
                await session.commit()
        except Exception:
            logger.exception("persist quality report failed %s", report.get("call_id"))
