"""对话编排引擎（主链路，商用增强版）。

单回合处理链（对应方案 §架构链路）：
  ASR文本 → 意图分类（含否定语境检测）→ 决策表路由 →（未命中且存在上轮碎片→拼接重试）→
  槽位写入 → 动作执行（模板/变体轮换直出 · LLM短句+超时回退 · 追问槽位 · 转人工 · 结束）→
  长尾兜底（受限LLM自由应答拉回流程 → "没听清"重试 → 节点兜底跳转）→
  链式衔接 → 合规规则引擎整段校验 → 状态更新（落库走后台任务）

商用增强：
- 开场合规披露（录音告知 + AI身份披露，settings.opening_disclosure）；
- 话术变体池轮换：同模板多个人审变体按通话顺序轮换，防复读机且零LLM延迟；
- 金额合理性复核：承诺金额异常（>欠款×系数 或 <1元）清槽请用户复述，防ASR误识别入档；
- "别再打"等明确停止联系诉求 → dnc_request 槽位，结束时入谢绝名单；
- Prometheus 埋点：回合数/延迟分布/合规拦截/LLM成败。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.engines.call_state import CallState
from app.engines.classifier import ClsResult, IntentClassifier
from app.engines.compliance import ComplianceEngine
from app.engines.prompt_builder import build_messages
from app.engines.route_engine import match_route
from app.knowledge.seed import (DNC_PHRASES, FREEFORM_COMPONENTS,
                                FREEFORM_STRATEGY, TERMINAL_END,
                                TERMINAL_TRANSFER)
from app.observability import metrics
from app.utils.text import render, sanitize_tts, strip_unfilled

logger = logging.getLogger(__name__)

_PLAN_SLOTS_ONCE = ("repayment_amount", "repayment_date")
_QUESTION_ENDINGS = ("？", "?")
_NORM_RE = re.compile(r"[\s，,。.！!？?；;：:、~\-—_]+")


def _norm(text: str) -> str:
    """归一化用于复读比对：去标点/空白，保留语义字符。"""
    return _NORM_RE.sub("", str(text or ""))


def _is_dup(candidate: str, refs) -> bool:
    """candidate 与任一 ref 归一化后相等或互为子串，视为复读。"""
    nc = _norm(candidate)
    if not nc:
        return False
    for ref in refs:
        nr = _norm(ref)
        if nr and (nc == nr or nc in nr or nr in nc):
            return True
    return False


@dataclass
class TurnResult:
    call_id: str
    reply: str = ""
    segments: list[str] = field(default_factory=list)
    intent: str = "UNKNOWN"
    objection: str | None = None
    emotion: str = "平稳"
    risk: str = "低"
    confidence: float = 0.0
    action_type: str = ""
    route_id: str | None = None
    node_before: str = ""
    node_after: str = ""
    slots: dict = field(default_factory=dict)
    end_call: bool = False
    transfer_human: bool = False
    llm_used: bool = False
    call_result: str | None = None
    compliance: dict = field(default_factory=dict)
    latency_ms: dict = field(default_factory=dict)


def _now_ms() -> float:
    return time.perf_counter() * 1000


def _fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class DialogOrchestrator:
    def __init__(self, cache, classifier: IntentClassifier, llm,
                 compliance: ComplianceEngine, settings: Settings) -> None:
        self.cache = cache
        self.classifier = classifier
        self.llm = llm
        self.compliance = compliance
        self.s = settings

    # ================= 开场 =================
    async def opening(self, state: CallState) -> TurnResult:
        t0 = _now_ms()
        snap = self.cache.snap()
        ctx = self._ctx(state)
        n001 = snap.nodes["N001"]
        segments = [self._entry(snap, n001, ctx, state)]
        if self.s.opening_disclosure and self.s.opening_disclosure_text:
            segments.append(self.s.opening_disclosure_text)   # 录音告知 + AI身份披露
        listen, _, _ = self._advance(snap, segments, n001,
                                     n001.get("entry_template_id"), ctx, state)
        state.visit("N001")
        state.visit(listen["node_id"])
        state.current_node = listen["node_id"]
        reply = sanitize_tts("".join(s for s in segments if s))
        state.remember("bot", reply)
        return TurnResult(call_id=state.call_id, reply=reply, segments=segments,
                          action_type="OPENING", node_before="N001",
                          node_after=state.current_node, slots=dict(state.slots),
                          latency_ms={"total": int(_now_ms() - t0)})

    # ================= 回合 =================
    async def handle_turn(self, state: CallState, user_text: str) -> TurnResult:
        t0 = _now_ms()
        snap = self.cache.snap()

        if state.ended:
            return TurnResult(call_id=state.call_id, reply="本次通话已结束，感谢您的配合。",
                              segments=["本次通话已结束，感谢您的配合。"], action_type="ENDED",
                              node_before=state.current_node, node_after=state.current_node,
                              end_call=True, latency_ms={"total": 0})

        node_before = state.current_node
        state.remember("user", user_text)

        # 用户明确要求停止联系 → 结束时进谢绝名单（外呼策略层强制生效）
        if any(p in user_text for p in DNC_PHRASES):
            state.slots["dnc_request"] = True

        # ---- 1. 意图分类（内含否定语境检测）----
        cls = await self.classifier.classify(snap, node_before, user_text, state.history)
        t1 = _now_ms()

        # ---- 2. 抽取槽位写入（标签效应槽在路由匹配后写入：
        #         路由条件如 identity_confirmed=false 指"本轮识别前"的状态）----
        for k, v in cls.slots.items():
            if v not in (None, ""):
                state.slots[k] = v

        # ---- 3. 决策表路由 ----
        route = match_route(snap, node_before, cls, state.slots)

        # ---- 3b. ASR碎片合并：上轮未命中的短文本与本轮拼接后重试一次 ----
        if route is None and self.s.asr_merge_enabled and state.last_fragment:
            merged_text = state.last_fragment + user_text
            cls_m = await self.classifier.classify(snap, node_before, merged_text, state.history)
            tmp = dict(state.slots)
            for k, v in cls_m.slots.items():
                if v not in (None, ""):
                    tmp[k] = v
            route_m = match_route(snap, node_before, cls_m, tmp)
            if route_m is not None:
                logger.info("asr fragments merged: %r + %r", state.last_fragment, user_text)
                cls, route, user_text = cls_m, route_m, merged_text
                state.slots = tmp
        state.last_fragment = user_text if route is None else None

        for k, v in (snap.label_effects.get(cls.intent) or {}).items():
            state.slots[k] = v
        t2 = _now_ms()

        segments: list[str] = []
        end_call = transfer = False
        llm_used = False
        route_id: str | None = None
        action = "FALLBACK"
        ctx = self._ctx(state)

        if route is None:
            segments, listen, end_call, transfer, llm_used = \
                await self._fallback(snap, state, ctx, cls, user_text)
        else:
            route_id, action = route["route_id"], route["action_type"]
            state.retries.pop(node_before, None)
            for k, v in (route.get("set_slots") or {}).items():
                if k == "call_result":
                    state.call_result = v
                else:
                    state.slots[k] = v
            ctx = self._ctx(state)  # set_slots 可能影响上下文变量
            target = snap.nodes.get(route["next_node"]) or snap.nodes[TERMINAL_END]
            strategy = snap.strategies.get(route.get("strategy_id") or "")
            template = snap.templates.get(route.get("template_id") or "")

            if action == "TRANSFER_HUMAN":
                segments = [self._tpl(snap, route.get("template_id") or "TPL_TRANSFER_001", ctx, state)]
                listen, transfer = snap.nodes[TERMINAL_TRANSFER], True
            elif action == "END_CALL":
                segments = [self._tpl(snap, route.get("template_id") or "TPL_END_001", ctx, state)]
                listen, end_call = snap.nodes[TERMINAL_END], True
            else:
                primary, listen, llm_used = await self._primary_reply(
                    snap, state, route, target, strategy, template, cls, user_text, ctx)
                if primary:
                    segments.append(primary)
                listen, chain_end, chain_tr = self._advance(
                    snap, segments, listen, route.get("template_id"), ctx, state)
                end_call = end_call or chain_end
                transfer = transfer or chain_tr
        t3 = _now_ms()

        # ---- 4. 合规规则引擎（整段校验，命中即替换为预审修复话术）----
        reply = sanitize_tts("".join(s for s in segments if s))
        comp = self.compliance.check(snap, reply, state.slots, state.case)
        if comp.repaired:
            reply = sanitize_tts(comp.text)
            segments = [reply]
            for v in comp.violations:
                state.risk_flags.append(v["rule_id"])
                metrics.COMPLIANCE_HITS.labels(rule_id=v["rule_id"]).inc()
            logger.warning("compliance repaired call=%s rules=%s",
                           state.call_id, [v["rule_id"] for v in comp.violations])
        reply, segments = self._avoid_repeat(reply, segments, state)
        t4 = _now_ms()

        # ---- 5. 状态推进 ----
        state.current_node = listen["node_id"]
        state.visit(listen["node_id"])
        state.turn_index += 1
        state.remember("bot", reply)
        if transfer:
            state.ended = state.transfer_human = True
            state.call_result = state.call_result or "转人工"
        elif end_call:
            state.ended = True
            state.call_result = state.call_result or "正常结束"

        total = int(_now_ms() - t0)
        metrics.TURNS_TOTAL.labels(action=action).inc()
        metrics.TURN_LATENCY_MS.observe(total)

        return TurnResult(
            call_id=state.call_id, reply=reply, segments=segments,
            intent=cls.intent, objection=cls.objection, emotion=cls.emotion,
            risk=cls.risk, confidence=round(cls.confidence, 3),
            action_type=action, route_id=route_id,
            node_before=node_before, node_after=state.current_node,
            slots=dict(state.slots), end_call=end_call, transfer_human=transfer,
            llm_used=llm_used, call_result=state.call_result,
            compliance={"passed": comp.passed, "violations": comp.violations},
            latency_ms={"classify": int(t1 - t0), "route": int(t2 - t1),
                        "reply": int(t3 - t2), "compliance": int(t4 - t3),
                        "total": total},
        )

    # ================= 内部 =================
    def _ctx(self, state: CallState) -> dict:
        case = state.case or {}
        ctx = {
            "mediation_org": case.get("mediation_org") or "调解中心",
            "official_verify_channel": case.get("official_verify_channel") or "官方渠道",
            "name": case.get("debtor_name") or "",
        }
        if state.slots.get("identity_confirmed"):
            for f in ("platform_name", "creditor_name", "case_id"):
                if case.get(f):
                    ctx[f] = case[f]
            if case.get("total_amount") not in (None, ""):
                ctx["total_amount"] = _fmt_num(float(case["total_amount"]))
        for k in ("repayment_amount", "repayment_date", "installment_count",
                  "installment_amount", "callback_time"):
            v = state.slots.get(k)
            if v not in (None, ""):
                ctx[k] = _fmt_num(v) if isinstance(v, (int, float)) else v
        return ctx

    def _tpl(self, snap, template_id: str | None, ctx: dict,
             state: CallState | None = None) -> str:
        t = snap.templates.get(template_id or "")
        if not t:
            return ""
        texts = [t["template_text"], *(t.get("variants") or [])]
        if state is not None and self.s.variant_rotation and len(texts) > 1:
            cur = state.variant_cursor.get(t["template_id"], 0)
            text = texts[cur % len(texts)]                  # 顺序轮换，本通不重复
            state.variant_cursor[t["template_id"]] = cur + 1
        else:
            text = texts[0]
        return strip_unfilled(render(text, ctx))

    def _entry(self, snap, node: dict, ctx: dict, state: CallState | None = None) -> str:
        return self._tpl(snap, node.get("entry_template_id"), ctx, state)

    @staticmethod
    def _last_bot(state: CallState) -> str:
        for role, text in reversed(state.history or []):
            if role == "bot" and text:
                return str(text)
        return ""

    @staticmethod
    def _strip_overlap_prefix(reply: str, last: str) -> str:
        """若 reply 归一化后以 last 为前缀，剥去重复前缀，仅保留新增尾段。"""
        nlast, nreply = _norm(last), _norm(reply)
        if not nlast or not nreply.startswith(nlast):
            return ""
        consumed, cut = 0, 0
        for i, ch in enumerate(reply):
            if _norm(ch):
                consumed += 1
            if consumed >= len(nlast):
                cut = i + 1
                break
        return reply[cut:].lstrip("，,。.；;！!？? ")

    def _avoid_repeat(self, reply: str, segments: list[str],
                      state: CallState) -> tuple[str, list[str]]:
        """同一通电话连续两轮不原样复读同一句（归一化比对，跨标点/空白）。"""
        last = self._last_bot(state)
        if not reply or not last or not _is_dup(reply, [last]):
            return reply, segments
        tail = self._strip_overlap_prefix(reply, last)
        if tail and _norm(tail) != _norm(reply):
            text = sanitize_tts(tail)
            return text, [text]
        logger.info("avoid_repeat compressed call=%s", state.call_id)
        text = sanitize_tts("我这边再补充一句，请您看是否方便回应一下。")
        return text, [text]

    # ---------- 方案确认（含金额合理性复核） ----------
    def _amount_anomaly(self, state: CallState) -> str | None:
        """承诺金额异常返回语境（'inst'/'once'），正常返回 None。"""
        total = float((state.case or {}).get("total_amount") or 0)
        if total <= 0:
            return None
        factor = self.s.amount_anomaly_factor
        cnt, per = state.slots.get("installment_count"), state.slots.get("installment_amount")
        if per:
            committed = (cnt or 1) * float(per)
            if committed > total * factor or float(per) < 1:
                return "inst"
        amt = state.slots.get("repayment_amount")
        if amt and (float(amt) > total * factor or float(amt) < 1):
            return "once"
        return None

    def _plan_confirm(self, snap, slots: dict, ctx: dict,
                      state: CallState) -> tuple[str | None, str | None]:
        """返回 (确认话术, 缺失槽位)。分期优先；强制模板渲染保证金额零幻觉。"""
        has_cnt, has_per = slots.get("installment_count"), slots.get("installment_amount")
        if has_cnt and has_per:
            return self._tpl(snap, "TPL_PLAN_CONFIRM_INST_001", ctx, state), None
        if has_cnt and not has_per:
            return None, "installment_amount"
        if has_per and not has_cnt:
            return None, "installment_count"
        for s in _PLAN_SLOTS_ONCE:
            if not slots.get(s):
                return None, s
        return self._tpl(snap, "TPL_PLAN_CONFIRM_001", ctx, state), None

    async def _primary_reply(self, snap, state: CallState, route: dict, target: dict,
                             strategy: dict | None, template: dict | None,
                             cls: ClsResult, user_text: str, ctx: dict):
        """执行路由动作，返回 (主话术, 监听节点, 是否用LLM)。"""
        action = route["action_type"]

        # ---- 方案确认门控（N021）----
        if target["node_id"] == "N021":
            # 金额合理性复核优先：异常金额清槽请用户复述，绝不带病进确认
            anomaly = self._amount_anomaly(state)
            if anomaly:
                state.risk_flags.append("AMOUNT_ANOMALY")
                for k in ("repayment_amount", "installment_amount"):
                    state.slots.pop(k, None)
                stay = "N019" if anomaly == "inst" else "N017"
                logger.warning("amount anomaly call=%s text=%r -> recheck",
                               state.call_id, user_text)
                return (self._tpl(snap, "TPL_AMOUNT_RECHECK_001", ctx, state),
                        snap.nodes[stay], False)
            ctx = self._ctx(state)
            text, missing = self._plan_confirm(snap, state.slots, ctx, state)
            if missing:
                ask = self._tpl(snap, snap.slot_questions.get(missing, ""), ctx, state)
                stay = "N019" if missing.startswith("installment") else "N017"
                return (ask or self._entry(snap, snap.nodes[stay], ctx, state),
                        snap.nodes[stay], False)
            return text, target, False

        if action in ("DIRECT_TEMPLATE", "RECORD_ONLY"):
            txt = self._tpl(snap, route.get("template_id"), ctx, state) \
                or self._entry(snap, target, ctx, state)
            return txt, target, False

        if action == "ASK_SLOT":
            for s in target.get("required_slots") or []:
                if not state.slots.get(s) and s in snap.slot_questions:
                    return self._tpl(snap, snap.slot_questions[s], ctx, state), target, False
            return self._entry(snap, target, ctx, state), target, False

        if action in ("LLM_SHORT_REPLY", "TEMPLATE_REWRITE"):
            if self.llm is not None and target.get("allow_llm", True):
                try:
                    msgs = build_messages(snap, route, target, strategy, template,
                                          cls, user_text, ctx, state.slots,
                                          history=state.history)
                    out = await self.llm.short_reply(msgs)
                    if out and len(out) >= 4:
                        metrics.LLM_CALLS.labels(outcome="ok").inc()
                        return out, target, True
                    metrics.LLM_CALLS.labels(outcome="fallback").inc()
                except Exception:
                    metrics.LLM_CALLS.labels(outcome="fallback").inc()
                    logger.exception("llm path failed, fallback to template")
            # 超时/失败 → 参考话术模板（含变体轮换）兜底
            txt = self._tpl(snap, route.get("template_id"), ctx, state) \
                or self._entry(snap, target, ctx, state)
            return txt, target, False

        return self._entry(snap, target, ctx, state), target, False

    def _advance(self, snap, segments: list[str], node: dict,
                 primary_tpl_id: str | None, ctx: dict, state: CallState | None = None):
        """auto_chain 链式衔接 + 终态处理。返回 (监听节点, end, transfer)。"""
        end = transfer = False
        guard = 0
        while guard < 5:
            guard += 1
            nid = node["node_id"]
            if nid == TERMINAL_END:
                if primary_tpl_id not in snap.no_end_append:
                    end_txt = self._tpl(snap, "TPL_END_001", ctx, state)
                    if end_txt and not _is_dup(end_txt, segments):
                        segments.append(end_txt)
                end = True
                break
            if nid == TERMINAL_TRANSFER:
                if not segments or not segments[-1]:
                    segments.append(self._entry(snap, node, ctx, state))
                transfer = True
                break
            if not node.get("auto_chain"):
                break
            nxt = snap.nodes.get(node.get("default_next") or "")
            if not nxt:
                break
            if nxt["node_id"] in (TERMINAL_END, TERMINAL_TRANSFER):
                node = nxt
                continue
            entry = self._entry(snap, nxt, ctx, state)
            if entry and not _is_dup(entry, segments):
                segments.append(entry)
            node = nxt
        return node, end, transfer

    # ---------- 长尾兜底 ----------
    async def _freeform(self, snap, state: CallState, node: dict,
                        user_text: str, ctx: dict, cls: ClsResult) -> str | None:
        """UNKNOWN长尾：受限LLM简短回应并拉回当前节点问题（仍过合规引擎）。"""
        if self.llm is None or not self.s.freeform_fallback_llm:
            return None
        try:
            pseudo_route = {"prompt_component_ids": FREEFORM_COMPONENTS,
                            "action_type": "LLM_SHORT_REPLY"}
            msgs = build_messages(snap, pseudo_route, node, FREEFORM_STRATEGY, None,
                                  cls, user_text, ctx, state.slots, history=state.history)
            out = await self.llm.short_reply(msgs)
            if out and len(out) >= 4:
                metrics.LLM_CALLS.labels(outcome="ok").inc()
                entry = self._entry(snap, node, ctx, state)
                last_bot = self._last_bot(state)
                if (entry and not out.rstrip().endswith(_QUESTION_ENDINGS)
                        and not _is_dup(entry, [out, last_bot])):
                    out = out + entry   # 拉回节点主问句
                return out
            metrics.LLM_CALLS.labels(outcome="fallback").inc()
        except Exception:
            metrics.LLM_CALLS.labels(outcome="fallback").inc()
            logger.exception("freeform fallback failed")
        return None

    async def _fallback(self, snap, state: CallState, ctx: dict,
                        cls: ClsResult, user_text: str):
        """无路由命中：受限LLM自由应答 → '没听清'重试 → 超 max_retry 跳节点兜底。
        返回 (segments, listen, end, transfer, llm_used)。"""
        node = snap.nodes.get(state.current_node) or snap.nodes[TERMINAL_END]
        nid = node["node_id"]
        n = state.retries.get(nid, 0) + 1
        state.retries[nid] = n
        if n > (node.get("max_retry") or 1):
            fb = snap.nodes.get(node.get("fallback_node") or "") or snap.nodes[TERMINAL_END]
            if fb["node_id"] == TERMINAL_TRANSFER:
                return [self._entry(snap, fb, ctx, state)], fb, False, True, False
            if fb["node_id"] == TERMINAL_END:
                segs = [self._tpl(snap, "TPL_FALLBACK_001", ctx, state),
                        self._tpl(snap, "TPL_END_001", ctx, state)]
                return segs, fb, True, False, False
            segs = [self._entry(snap, fb, ctx, state)]
            listen, end, tr = self._advance(snap, segs, fb,
                                            fb.get("entry_template_id"), ctx, state)
            return segs, listen, end, tr, False

        # 先尝试受限LLM长尾应答（解决"只会说没听清"的死板问题）
        free = await self._freeform(snap, state, node, user_text, ctx, cls)
        if free:
            return [free], node, False, False, True

        retry = self._tpl(snap, "TPL_RETRY_001", ctx, state)
        entry = self._entry(snap, node, ctx, state)
        last_bot = self._last_bot(state)
        # 用户刚听过节点主问句：仅给出转折提示，避免连续两轮原样复读
        if entry and last_bot and _is_dup(entry, [last_bot]):
            return [retry], node, False, False, False
        return [(retry + entry) if entry else retry], node, False, False, False
