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
_LOG_TEXT_LIMIT = 120


def _norm(text: str) -> str:
    """归一化用于复读比对：去标点/空白，保留语义字符。"""
    return _NORM_RE.sub("", str(text or ""))


def _preview(text, limit: int = _LOG_TEXT_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _segments_preview(segments: list[str]) -> list[dict]:
    return [{"len": len(s or ""), "text": _preview(s)} for s in (segments or [])]


def _slot_changes(before: dict, after: dict) -> dict:
    keys = sorted(set(before) | set(after))
    changes = {}
    for key in keys:
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    return changes


def _message_overview(messages: list[dict]) -> list[dict]:
    overview = []
    for msg in messages or []:
        content = msg.get("content") or ""
        overview.append({
            "role": msg.get("role"),
            "chars": len(str(content)),
            "preview": _preview(content, 180),
        })
    return overview


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
        logger.info(
            "dialog opening start call=%s node=%s case_id=%s ctx_keys=%s",
            state.call_id, state.current_node, (state.case or {}).get("case_id"),
            sorted(ctx.keys()),
        )
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
        logger.info(
            "dialog opening done call=%s node_after=%s reply_len=%s reply=%r segments=%s elapsed_ms=%s",
            state.call_id, state.current_node, len(reply), _preview(reply),
            _segments_preview(segments), int(_now_ms() - t0),
        )
        return TurnResult(call_id=state.call_id, reply=reply, segments=segments,
                          action_type="OPENING", node_before="N001",
                          node_after=state.current_node, slots=dict(state.slots),
                          latency_ms={"total": int(_now_ms() - t0)})

    # ================= 回合 =================
    async def handle_turn(self, state: CallState, user_text: str) -> TurnResult:
        t0 = _now_ms()
        snap = self.cache.snap()

        if state.ended:
            logger.info("dialog turn ignored ended call=%s node=%s user=%r",
                        state.call_id, state.current_node, _preview(user_text))
            return TurnResult(call_id=state.call_id, reply="本次通话已结束，感谢您的配合。",
                              segments=["本次通话已结束，感谢您的配合。"], action_type="ENDED",
                              node_before=state.current_node, node_after=state.current_node,
                              end_call=True, latency_ms={"total": 0})

        node_before = state.current_node
        slots_before = dict(state.slots)
        last_bot_before = self._last_bot(state)
        logger.info(
            "dialog turn start call=%s turn=%s node=%s user_len=%s user=%r last_bot_len=%s "
            "last_bot=%r slots=%s retries=%s history=%s",
            state.call_id, state.turn_index + 1, node_before, len(user_text or ""),
            _preview(user_text), len(last_bot_before), _preview(last_bot_before),
            dict(state.slots), dict(state.retries or {}), len(state.history or []),
        )

        # 用户无有效输入（ASR 无内容 / 占位符 / 静音）：仅温和回探，绝不进入分类与路由
        if not (user_text or "").strip():
            return self._silence_prompt(state, snap, node_before, t0)

        state.silence_count = 0   # 真实输入：复位静音计数
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
                logger.info(
                    "asr fragments merged call=%s node=%s previous=%r current=%r merged=%r "
                    "intent=%s route=%s",
                    state.call_id, node_before, _preview(state.last_fragment),
                    _preview(user_text), _preview(merged_text), cls_m.intent,
                    route_m.get("route_id"),
                )
                cls, route, user_text = cls_m, route_m, merged_text
                state.slots = tmp
        state.last_fragment = user_text if route is None else None

        for k, v in (snap.label_effects.get(cls.intent) or {}).items():
            state.slots[k] = v
        t2 = _now_ms()

        logger.info(
            "dialog classified call=%s node=%s intent=%s objection=%s source=%s conf=%.3f "
            "emotion=%s risk=%s extracted_slots=%s slot_changes=%s last_fragment=%r",
            state.call_id, node_before, cls.intent, cls.objection, cls.source,
            cls.confidence, cls.emotion, cls.risk, dict(cls.slots),
            _slot_changes(slots_before, state.slots), _preview(state.last_fragment),
        )
        if route is None:
            logger.info(
                "dialog route miss call=%s node=%s intent=%s objection=%s slots=%s",
                state.call_id, node_before, cls.intent, cls.objection, dict(state.slots),
            )
        else:
            logger.info(
                "dialog route hit call=%s node=%s route=%s action=%s next=%s template=%s "
                "strategy=%s priority=%s condition=%s set_slots=%s",
                state.call_id, node_before, route.get("route_id"),
                route.get("action_type"), route.get("next_node"),
                route.get("template_id"), route.get("strategy_id"),
                route.get("priority"), route.get("slot_condition"),
                route.get("set_slots"),
            )

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
        latency = {"classify": int(t1 - t0), "route": int(t2 - t1),
                   "reply": int(t3 - t2), "compliance": int(t4 - t3),
                   "total": total}
        logger.info(
            "dialog turn done call=%s turn=%s action=%s route=%s node=%s->%s intent=%s "
            "llm_used=%s end=%s transfer=%s reply_len=%s reply=%r segments=%s compliance=%s "
            "latency_ms=%s slots=%s retries=%s",
            state.call_id, state.turn_index, action, route_id, node_before,
            state.current_node, cls.intent, llm_used, end_call, transfer, len(reply),
            _preview(reply), _segments_preview(segments),
            {"passed": comp.passed, "violations": comp.violations}, latency,
            dict(state.slots), dict(state.retries or {}),
        )

        return TurnResult(
            call_id=state.call_id, reply=reply, segments=segments,
            intent=cls.intent, objection=cls.objection, emotion=cls.emotion,
            risk=cls.risk, confidence=round(cls.confidence, 3),
            action_type=action, route_id=route_id,
            node_before=node_before, node_after=state.current_node,
            slots=dict(state.slots), end_call=end_call, transfer_human=transfer,
            llm_used=llm_used, call_result=state.call_result,
            compliance={"passed": comp.passed, "violations": comp.violations},
            latency_ms=latency,
        )

    # ---- 静音兜底：用户无有效输入时温和回探（调解中立口吻） ----
    _SILENCE_PROMPTS = (
        "您好，您慢慢说，听到您说话就行。",
        "嗯，您方便的话就说说目前的情况。",
        "您那边还在听吗？我这边等您一下。",
    )

    def _silence_prompt(self, state: CallState, snap, node_before: str,
                        t0: float) -> "TurnResult":
        """ASR 无有效内容时温和回探。

        重要：每个 usrtype=9 都触发我们说话，会被 IVR 自播一次，听感是"每句话说两遍"。
        所以连续静音超过阈值后直接挂断，避免无限循环骚扰用户。
        """
        state.silence_count += 1
        max_silence = max(1, int(self.s.okcti_silence_max_turns or 1))

        # 超阈值：温和挂断，避免被 IVR 反复自播
        if state.silence_count > max_silence:
            reply = sanitize_tts(
                "看您现在可能不太方便，那今天就先不打扰您了，后续我们再联系。再见。")
            state.remember("bot", reply)
            state.ended = True
            state.call_result = state.call_result or "用户未回应"
            logger.info(
                "dialog turn silence_end call=%s node=%s silence_count=%s reply=%r",
                state.call_id, node_before, state.silence_count, _preview(reply),
            )
            metrics.TURNS_TOTAL.labels(action="SILENCE_END").inc()
            return TurnResult(
                call_id=state.call_id, reply=reply, segments=[reply],
                action_type="SILENCE_END", node_before=node_before,
                node_after=node_before, slots=dict(state.slots),
                end_call=True, call_result=state.call_result,
                latency_ms={"total": int(_now_ms() - t0)},
            )

        recent = self._recent_bots(state, n=3)
        preface = next((p for p in self._SILENCE_PROMPTS if not _is_dup(p, recent)),
                       self._SILENCE_PROMPTS[0])
        reply = sanitize_tts(preface)
        state.remember("bot", reply)
        logger.info(
            "dialog turn silence call=%s node=%s silence_count=%s preface=%r reply=%r",
            state.call_id, node_before, state.silence_count,
            _preview(preface), _preview(reply),
        )
        metrics.TURNS_TOTAL.labels(action="SILENCE_PROMPT").inc()
        return TurnResult(
            call_id=state.call_id, reply=reply, segments=[reply],
            action_type="SILENCE_PROMPT", node_before=node_before,
            node_after=node_before, slots=dict(state.slots),
            latency_ms={"total": int(_now_ms() - t0)},
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
             state: CallState | None = None,
             avoid_refs: list[str] | None = None) -> str:
        """渲染模板。多变体场景下，优先返回与 avoid_refs 都不复读的候选。"""
        t = snap.templates.get(template_id or "")
        if not t:
            if state is not None:
                logger.warning("template missing call=%s template=%s", state.call_id, template_id)
            return ""
        texts = [t["template_text"], *(t.get("variants") or [])]
        if not texts:
            if state is not None:
                logger.warning("template empty call=%s template=%s", state.call_id, template_id)
            return ""
        selected_index = 0
        rotated = False
        avoided_duplicate = False
        if state is not None and self.s.variant_rotation and len(texts) > 1:
            n = len(texts)
            cur = state.variant_cursor.get(t["template_id"], 0)
            chosen, idx = texts[cur % n], cur
            if avoid_refs:
                refs = [r for r in avoid_refs if r]
                for offset in range(n):
                    cand = texts[(cur + offset) % n]
                    if not _is_dup(cand, refs):
                        chosen, idx = cand, cur + offset
                        avoided_duplicate = offset > 0
                        break
            state.variant_cursor[t["template_id"]] = idx + 1
            selected_index = idx % n
            rotated = True
            text = chosen
        else:
            text = texts[0]
        rendered = strip_unfilled(render(text, ctx))
        if state is not None:
            logger.info(
                "template rendered call=%s template=%s variant=%s/%s rotated=%s "
                "avoid_refs=%s avoided_duplicate=%s ctx_keys=%s text_len=%s text=%r",
                state.call_id, t.get("template_id"), selected_index + 1, len(texts),
                rotated, bool(avoid_refs), avoided_duplicate, sorted(ctx.keys()),
                len(rendered), _preview(rendered),
            )
        return rendered

    def _entry(self, snap, node: dict, ctx: dict, state: CallState | None = None) -> str:
        return self._tpl(snap, node.get("entry_template_id"), ctx, state)

    @staticmethod
    def _last_bot(state: CallState) -> str:
        for role, text in reversed(state.history or []):
            if role == "bot" and text:
                return str(text)
        return ""

    @staticmethod
    def _recent_bots(state: CallState, n: int = 3) -> list[str]:
        """返回最近 n 条 bot 回复（按时间倒序），用于跨多轮复读判定。"""
        out: list[str] = []
        for role, text in reversed(state.history or []):
            if role == "bot" and text:
                out.append(str(text))
                if len(out) >= n:
                    break
        return out

    @staticmethod
    def _collapse_self_repeat(text: str) -> str:
        """合并一句话内相邻重复子句：'我这边再确认一下。我这边再确认一下' → 单句。"""
        if not text:
            return text
        parts = re.split(r"(?<=[。！？!?])", text)
        out, seen = [], set()
        for p in parts:
            key = _norm(p)
            if not key:
                if p:
                    out.append(p)
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return "".join(out)

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
            logger.info(
                "avoid_repeat stripped_overlap call=%s last_len=%s reply_len=%s tail_len=%s "
                "last=%r reply=%r tail=%r",
                state.call_id, len(last), len(reply), len(text), _preview(last),
                _preview(reply), _preview(text),
            )
            return text, [text]
        logger.info(
            "avoid_repeat compressed call=%s last_len=%s reply_len=%s last=%r reply=%r",
            state.call_id, len(last), len(reply), _preview(last), _preview(reply),
        )
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
        logger.info(
            "dialog primary start call=%s route=%s action=%s target=%s template=%s "
            "strategy=%s allow_llm=%s user=%r ctx_keys=%s",
            state.call_id, route.get("route_id"), action, target.get("node_id"),
            route.get("template_id"), route.get("strategy_id"),
            target.get("allow_llm", True), _preview(user_text), sorted(ctx.keys()),
        )

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
                logger.info(
                    "dialog primary ask_missing_slot call=%s route=%s missing=%s stay=%s ask=%r",
                    state.call_id, route.get("route_id"), missing, stay,
                    _preview(ask),
                )
                return (ask or self._entry(snap, snap.nodes[stay], ctx, state),
                        snap.nodes[stay], False)
            logger.info(
                "dialog primary plan_confirm call=%s route=%s reply=%r",
                state.call_id, route.get("route_id"), _preview(text),
            )
            return text, target, False

        if action in ("DIRECT_TEMPLATE", "RECORD_ONLY"):
            txt = self._tpl(snap, route.get("template_id"), ctx, state) \
                or self._entry(snap, target, ctx, state)
            logger.info(
                "dialog primary template call=%s route=%s action=%s reply=%r",
                state.call_id, route.get("route_id"), action, _preview(txt),
            )
            return txt, target, False

        if action == "ASK_SLOT":
            for s in target.get("required_slots") or []:
                if not state.slots.get(s) and s in snap.slot_questions:
                    txt = self._tpl(snap, snap.slot_questions[s], ctx, state)
                    logger.info(
                        "dialog primary ask_slot call=%s route=%s slot=%s reply=%r",
                        state.call_id, route.get("route_id"), s, _preview(txt),
                    )
                    return txt, target, False
            txt = self._entry(snap, target, ctx, state)
            logger.info(
                "dialog primary ask_slot_entry call=%s route=%s reply=%r",
                state.call_id, route.get("route_id"), _preview(txt),
            )
            return txt, target, False

        if action in ("LLM_SHORT_REPLY", "TEMPLATE_REWRITE"):
            if self.llm is None:
                logger.info("dialog llm skipped call=%s route=%s reason=no_client",
                            state.call_id, route.get("route_id"))
            elif not target.get("allow_llm", True):
                logger.info("dialog llm skipped call=%s route=%s reason=target_disallow target=%s",
                            state.call_id, route.get("route_id"), target.get("node_id"))
            if self.llm is not None and target.get("allow_llm", True):
                try:
                    msgs = build_messages(snap, route, target, strategy, template,
                                          cls, user_text, ctx, state.slots,
                                          history=state.history)
                    logger.info(
                        "dialog llm request call=%s route=%s model=%s messages=%s",
                        state.call_id, route.get("route_id"), self.s.llm_model,
                        _message_overview(msgs),
                    )
                    out = await self.llm.short_reply(msgs)
                    if out and len(out) >= 4:
                        metrics.LLM_CALLS.labels(outcome="ok").inc()
                        logger.info(
                            "dialog llm ok call=%s route=%s out_len=%s out=%r",
                            state.call_id, route.get("route_id"), len(out), _preview(out),
                        )
                        return out, target, True
                    metrics.LLM_CALLS.labels(outcome="fallback").inc()
                    logger.warning(
                        "dialog llm fallback call=%s route=%s reason=empty_or_short out_len=%s out=%r",
                        state.call_id, route.get("route_id"), len(out or ""), _preview(out),
                    )
                except Exception:
                    metrics.LLM_CALLS.labels(outcome="fallback").inc()
                    logger.exception("llm path failed, fallback to template call=%s route=%s",
                                     state.call_id, route.get("route_id"))
            # 超时/失败 → 参考话术模板（含变体轮换）兜底
            txt = self._tpl(snap, route.get("template_id"), ctx, state) \
                or self._entry(snap, target, ctx, state)
            logger.info(
                "dialog llm fallback_template call=%s route=%s template=%s reply=%r",
                state.call_id, route.get("route_id"), route.get("template_id"),
                _preview(txt),
            )
            return txt, target, False

        txt = self._entry(snap, target, ctx, state)
        logger.info(
            "dialog primary default_entry call=%s route=%s action=%s reply=%r",
            state.call_id, route.get("route_id"), action, _preview(txt),
        )
        return txt, target, False

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
                        if state is not None:
                            logger.info(
                                "dialog advance append_end call=%s node=%s template=TPL_END_001 text=%r",
                                state.call_id, nid, _preview(end_txt),
                            )
                    elif state is not None and end_txt:
                        logger.info(
                            "dialog advance skip_dup_end call=%s node=%s template=TPL_END_001 text=%r",
                            state.call_id, nid, _preview(end_txt),
                        )
                end = True
                break
            if nid == TERMINAL_TRANSFER:
                if not segments or not segments[-1]:
                    entry = self._entry(snap, node, ctx, state)
                    segments.append(entry)
                    if state is not None:
                        logger.info(
                            "dialog advance append_transfer call=%s node=%s text=%r",
                            state.call_id, nid, _preview(entry),
                        )
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
                if state is not None:
                    logger.info(
                        "dialog advance append_chain call=%s from_node=%s next_node=%s "
                        "template=%s text=%r",
                        state.call_id, nid, nxt.get("node_id"),
                        nxt.get("entry_template_id"), _preview(entry),
                    )
            elif state is not None and entry:
                logger.info(
                    "dialog advance skip_dup_chain call=%s from_node=%s next_node=%s "
                    "template=%s text=%r",
                    state.call_id, nid, nxt.get("node_id"),
                    nxt.get("entry_template_id"), _preview(entry),
                )
            node = nxt
        return node, end, transfer

    # ---------- 长尾兜底 ----------
    async def _freeform(self, snap, state: CallState, node: dict,
                        user_text: str, ctx: dict, cls: ClsResult) -> str | None:
        """UNKNOWN长尾：受限LLM简短回应并拉回当前节点问题（仍过合规引擎）。"""
        if self.llm is None or not self.s.freeform_fallback_llm:
            logger.info(
                "dialog freeform skipped call=%s node=%s reason=%s",
                state.call_id, node.get("node_id"),
                "no_client" if self.llm is None else "disabled",
            )
            return None
        try:
            pseudo_route = {"prompt_component_ids": FREEFORM_COMPONENTS,
                            "action_type": "LLM_SHORT_REPLY"}
            # 把当前节点主问句作为 SCRIPT 喂给模型，让 LLM 看到目标话术再换种说法；
            # 同一次 _entry 调用复用：避免变体游标二次推进导致拉回时撞上上一轮原句
            entry_text = self._entry(snap, node, ctx, state)
            pseudo_template = {"template_text": entry_text} if entry_text else None
            msgs = build_messages(snap, pseudo_route, node, FREEFORM_STRATEGY,
                                  pseudo_template, cls, user_text, ctx, state.slots,
                                  history=state.history)
            logger.info(
                "dialog freeform request call=%s node=%s user=%r entry=%r messages=%s",
                state.call_id, node.get("node_id"), _preview(user_text),
                _preview(entry_text), _message_overview(msgs),
            )
            out = await self.llm.short_reply(msgs)
            if out and len(out) >= 4:
                metrics.LLM_CALLS.labels(outcome="ok").inc()
                # 跨最近3轮 bot 比对：避免几轮前刚问过的节点主问句再被追加
                recent_bots = self._recent_bots(state, n=3)
                if (entry_text and not out.rstrip().endswith(_QUESTION_ENDINGS)
                        and not _is_dup(entry_text, [out, *recent_bots])):
                    out = out + entry_text   # 拉回节点主问句
                    logger.info(
                        "dialog freeform appended_entry call=%s node=%s out=%r entry=%r",
                        state.call_id, node.get("node_id"), _preview(out),
                        _preview(entry_text),
                    )
                logger.info(
                    "dialog freeform ok call=%s node=%s out_len=%s out=%r",
                    state.call_id, node.get("node_id"), len(out), _preview(out),
                )
                return out
            metrics.LLM_CALLS.labels(outcome="fallback").inc()
            logger.warning(
                "dialog freeform fallback call=%s node=%s reason=empty_or_short out_len=%s out=%r",
                state.call_id, node.get("node_id"), len(out or ""), _preview(out),
            )
        except Exception:
            metrics.LLM_CALLS.labels(outcome="fallback").inc()
            logger.exception("freeform fallback failed call=%s node=%s",
                             state.call_id, node.get("node_id"))
        return None

    async def _fallback(self, snap, state: CallState, ctx: dict,
                        cls: ClsResult, user_text: str):
        """无路由命中：受限LLM自由应答 → '没听清'重试 → 超 max_retry 跳节点兜底。
        返回 (segments, listen, end, transfer, llm_used)。"""
        node = snap.nodes.get(state.current_node) or snap.nodes[TERMINAL_END]
        nid = node["node_id"]
        n = state.retries.get(nid, 0) + 1
        state.retries[nid] = n
        logger.info(
            "dialog fallback start call=%s node=%s retry=%s max_retry=%s intent=%s user=%r",
            state.call_id, nid, n, node.get("max_retry") or 1, cls.intent,
            _preview(user_text),
        )
        if n > (node.get("max_retry") or 1):
            fb = snap.nodes.get(node.get("fallback_node") or "") or snap.nodes[TERMINAL_END]
            if fb["node_id"] == TERMINAL_TRANSFER:
                logger.info("dialog fallback jump call=%s from_node=%s to_node=%s transfer=true",
                            state.call_id, nid, fb.get("node_id"))
                return [self._entry(snap, fb, ctx, state)], fb, False, True, False
            if fb["node_id"] == TERMINAL_END:
                segs = [self._tpl(snap, "TPL_FALLBACK_001", ctx, state),
                        self._tpl(snap, "TPL_END_001", ctx, state)]
                logger.info("dialog fallback jump call=%s from_node=%s to_node=%s end=true segments=%s",
                            state.call_id, nid, fb.get("node_id"), _segments_preview(segs))
                return segs, fb, True, False, False
            segs = [self._entry(snap, fb, ctx, state)]
            listen, end, tr = self._advance(snap, segs, fb,
                                            fb.get("entry_template_id"), ctx, state)
            logger.info(
                "dialog fallback jump call=%s from_node=%s to_node=%s listen=%s end=%s transfer=%s segments=%s",
                state.call_id, nid, fb.get("node_id"), listen.get("node_id"),
                end, tr, _segments_preview(segs),
            )
            return segs, listen, end, tr, False

        # 先尝试受限LLM长尾应答（解决"只会说没听清"的死板问题）
        free = await self._freeform(snap, state, node, user_text, ctx, cls)
        if free:
            logger.info("dialog fallback freeform_used call=%s node=%s reply=%r",
                        state.call_id, nid, _preview(free))
            return [free], node, False, False, True

        recent_bots = self._recent_bots(state, n=3)
        last_bot = recent_bots[0] if recent_bots else ""
        retry = self._tpl(snap, "TPL_RETRY_001", ctx, state,
                          avoid_refs=recent_bots)
        entry = self._entry(snap, node, ctx, state)
        # 用户最近 N 轮已听过节点主问句：仅给出转折提示，避免再次原样复读
        if entry and recent_bots and _is_dup(entry, recent_bots):
            logger.info(
                "dialog fallback skip_entry_dup call=%s node=%s retry=%r entry=%r last_bot=%r",
                state.call_id, nid, _preview(retry), _preview(entry), _preview(last_bot),
            )
            return [retry], node, False, False, False
        merged = (retry + entry) if entry else retry
        collapsed = self._collapse_self_repeat(merged)
        logger.info(
            "dialog fallback retry_template call=%s node=%s retry=%r entry=%r collapsed=%r",
            state.call_id, nid, _preview(retry), _preview(entry), _preview(collapsed),
        )
        return [collapsed], node, False, False, False
