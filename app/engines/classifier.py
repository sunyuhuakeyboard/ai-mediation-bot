"""意图/抗性分类引擎。

分层策略（对应方案 §小模型意图分类）：
1. 若配置了小模型分类服务（classifier_url），先走 HTTP（预算 250ms，超时即降级）；
2. 关键词规则分类：全局优先标签（诈骗/投诉/辱骂/拒调/人工/非本人）先扫，
   其余业务标签按"最长关键词命中"取胜，避免短词误吞长词；
3. 极短肯定/否定（嗯/对/不是…）→ 按当前节点极性映射为业务标签（NODE_POLAR_MAP）；
4. 槽位抽取（金额/时间/期数/回访时间）；在方案语境下由槽位推断 PROVIDE_PLAN。
分类只产出标签与槽位，绝不产出话术 —— 话术由决策表与模板库决定。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.utils.text import (is_negated_plan, parse_amount, parse_callback_time,
                            parse_date_text, parse_installment)

logger = logging.getLogger(__name__)

# 方案语境节点：在这些节点收到金额/时间槽位可推断为 PROVIDE_PLAN
PLAN_CONTEXT = {"N007", "N009", "N017", "N018", "N019", "N021", "N022"}
INSTALLMENT_NODES = {"N019"}

_PUNCT_RE = re.compile(r"[\s，。、！？!?．.~…：:；;\"'）（()\[\]]+")

_AFFIRM_SET = {"嗯", "对", "是", "好", "行", "可以", "好的", "是的", "对的", "没问题",
               "嗯嗯", "对对", "好好", "可以的", "行的", "中", "好嘞", "ok", "okay"}
_DENY_SET = {"不是", "不", "没有", "没", "不行", "不用", "不好", "不可以", "不对", "没用", "不要",
             "我不是", "不是了", "不是吧", "不是的", "不是诶"}

# 关键词分类排除的伪标签（它们另有专门路径）
_EXCLUDE_KW_SCAN = {"AFFIRM", "DENY", "UNKNOWN", "PROVIDE_PLAN"}

_EMOTION_HOT = ("烦", "气死", "凭什么", "有病", "滚", "妈的", "神经", "吵", "闹")
_EMOTION_LOW = ("唉", "难受", "压力", "愁", "没办法", "实在")


@dataclass
class ClsResult:
    intent: str = "UNKNOWN"
    objection: str | None = None
    emotion: str = "平稳"
    risk: str = "低"
    confidence: float = 0.0
    slots: dict = field(default_factory=dict)
    source: str = "keyword"        # model / keyword / polar / slots


class IntentClassifier:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None,
                 llm=None) -> None:
        self.s = settings
        self.http = http
        self.llm = llm

    async def classify(self, snap, node_id: str, text: str, history: list | None = None) -> ClsResult:
        text = (text or "").strip()
        negated = is_negated_plan(text)
        slots = {} if negated else self._extract_slots(node_id, text)

        result: ClsResult | None = None
        if self.s.classifier_url and self.http is not None:
            result = await self._via_model(node_id, text, history)
        if result is None:
            result = self._via_keywords(snap, node_id, text)
        # 关键词命中弱 → 用 LLM 兜底意图分类，避免动辄进入 fallback
        if (result.intent == "UNKNOWN" and result.confidence < 0.5
                and self.llm is not None and self.s.llm_classifier_enabled and text):
            llm_result = await self._via_llm(snap, node_id, text, history)
            if llm_result is not None and llm_result.intent != "UNKNOWN":
                result = llm_result
        if negated:
            # 否定语境的金额/时间不是承诺：丢弃方案槽位，弱标签倾向"无力还款"
            for k in ("repayment_amount", "repayment_date",
                      "installment_count", "installment_amount"):
                result.slots.pop(k, None)
            if result.intent in ("UNKNOWN", "PROVIDE_PLAN", "AFFIRM"):
                result.intent, result.confidence, result.source = "NO_MONEY", 0.75, "negation"
        result.slots.update(slots)

        # 槽位推断 PROVIDE_PLAN（用户直接报方案，覆盖弱标签）
        if not negated:
            result = self._infer_plan(snap, node_id, result)

        label = snap.labels_by_id.get(result.intent)
        if label:
            result.risk = label.get("risk_level") or result.risk
            result.objection = label.get("objection_label") or result.objection
        result.emotion = self._emotion(text, result.intent)
        return result

    # ---------------- 小模型通道 ----------------
    async def _via_model(self, node_id: str, text: str, history) -> ClsResult | None:
        try:
            resp = await self.http.post(
                self.s.classifier_url,
                json={"current_node": node_id, "user_text": text,
                      "history": [h for h in (history or [])][-4:]},
                timeout=self.s.classifier_timeout_ms / 1000,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            intent = data.get("intent") or "UNKNOWN"
            return ClsResult(intent=intent, confidence=float(data.get("confidence") or 0.0),
                             slots=dict(data.get("slots") or {}), source="model")
        except Exception:
            logger.debug("classifier service degraded to keywords", exc_info=True)
            return None

    # ---------------- LLM 兜底通道（关键词命中失败时） ----------------
    _SYNTHETIC_INPUT_HINTS = (
        "asr content", "no speech detected", "content empty",
        "用户未回应", "识别异常", "按键错误",
    )

    async def _via_llm(self, snap, node_id: str, text: str, history) -> ClsResult | None:
        """让 LLM 在已知标签集合内挑一个意图。仅在关键词分类失败时启用。"""
        # 防御：占位符/非自然语言一律拒绝分类，避免对"ASR content always empty"幻觉打标
        lowered = text.lower()
        if any(h in lowered for h in self._SYNTHETIC_INPUT_HINTS):
            logger.info("llm classifier skipped synthetic_input text=%r", text[:40])
            return None
        # 过滤主体为非中文（4 字以上）的纯英文/数字串：典型为上游占位符
        cn_chars = sum(1 for ch in text if "一" <= ch <= "鿿")
        if len(text) >= 4 and cn_chars == 0:
            logger.info("llm classifier skipped non_chinese text=%r", text[:40])
            return None
        node = snap.nodes.get(node_id) or {}
        # 仅暴露与当前节点候选路由相关的标签；候选不足时退回全标签
        candidates = self._candidate_labels(snap, node_id)
        if len(candidates) < 4:
            candidates = [lbl for lbl in snap.labels
                          if lbl["label_id"] not in ("UNKNOWN", "AFFIRM", "DENY")]
        catalog = "\n".join(f"- {lbl['label_id']}: {lbl.get('label_name') or lbl['label_id']}"
                            for lbl in candidates)
        hist_lines = []
        for role, t in (history or [])[-4:]:
            hist_lines.append(("用户：" if role == "user" else "调解员：") + str(t))
        prompt = (
            "你是民商事电话调解员的意图分类器，只能输出 JSON。\n"
            f"当前节点：{node.get('node_name') or node_id}。"
            f"节点目标：{node.get('node_goal') or '推进对话'}。\n"
            f"最近对话：\n{chr(10).join(hist_lines) or '（首轮）'}\n"
            f"用户刚才说：{text}\n"
            "请在下列候选标签中选一个最贴近的：\n"
            f"{catalog}\n"
            "只输出一行 JSON，格式：{\"intent\":\"标签ID\",\"confidence\":0~1}"
        )
        try:
            raw = await self.llm.complete_short(
                [{"role": "user", "content": prompt}], max_tokens=40)
        except Exception:
            logger.debug("llm classifier call failed", exc_info=True)
            return None
        if not raw:
            return None
        parsed = self._parse_intent_json(raw)
        if not parsed:
            return None
        intent_id, conf = parsed
        # 严格校验：必须是已知标签
        if not any(lbl["label_id"] == intent_id for lbl in snap.labels):
            logger.info("llm classifier returned unknown label=%r", intent_id)
            return None
        return ClsResult(intent=intent_id, confidence=max(0.0, min(1.0, conf)),
                         source="llm")

    @staticmethod
    def _candidate_labels(snap, node_id: str) -> list[dict]:
        """收集当前节点路由表里出现过的 intent_label，作为候选范围。"""
        wanted: set[str] = set()
        for (n, label_id) in snap.routes_index.keys():
            if n == node_id and label_id:
                wanted.add(label_id)
        out = [lbl for lbl in snap.labels if lbl["label_id"] in wanted]
        existing = {lbl["label_id"] for lbl in out}
        # 风险类标签全局可触发，始终带上
        out.extend(lbl for lbl in snap.labels
                   if lbl.get("global_priority") and lbl["label_id"] not in existing)
        return out

    @staticmethod
    def _parse_intent_json(raw: str) -> tuple[str, float] | None:
        """从 LLM 文本中解析 {intent,confidence}；容忍前后多余字符。"""
        if not raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except Exception:
            return None
        intent = data.get("intent")
        if not isinstance(intent, str):
            return None
        try:
            conf = float(data.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        return intent, conf

    # ---------------- 关键词通道 ----------------
    def _via_keywords(self, snap, node_id: str, text: str) -> ClsResult:
        # 1) 业务标签最长命中（全局优先标签加权，保证风险类优先）
        matched: dict[str, tuple[dict, int]] = {}
        for label in snap.labels:
            lid = label["label_id"]
            if lid in _EXCLUDE_KW_SCAN:
                continue
            for kw in label.get("keywords") or []:
                if kw and kw in text:
                    score = len(kw) + (100 if label.get("global_priority") else 0)
                    if score > matched.get(lid, (None, 0))[1]:
                        matched[lid] = (label, score)
        if matched:
            # 身份确认/否认 与 "什么事/你是谁" 同句出现时（如"是我，什么事"），身份结论优先
            if "QUESTION_IDENTITY" in matched:
                for dominant in ("NOT_SELF", "CONFIRM_SELF"):
                    if dominant in matched:
                        lbl = matched[dominant][0]
                        return ClsResult(intent=dominant,
                                         confidence=0.9 if lbl.get("global_priority") else 0.85,
                                         source="keyword")
            best_label = max(matched.values(), key=lambda x: x[1])[0]
            conf = 0.9 if best_label.get("global_priority") else 0.85
            return ClsResult(intent=best_label["label_id"], confidence=conf, source="keyword")

        # 2) 极短肯定/否定 → 节点极性映射
        t = _PUNCT_RE.sub("", text).lower()
        if t and len(t) <= 6:
            polar = None
            if t in _AFFIRM_SET:
                polar = "AFFIRM"
            elif t in _DENY_SET or (t.startswith(("不", "没", "别")) and len(t) <= 5):
                polar = "DENY"
            elif t.startswith(("嗯", "对", "好", "行")) and len(t) <= 4:
                polar = "AFFIRM"
            if polar:
                mapped = (snap.polar_map.get(node_id) or {}).get(polar, polar)
                return ClsResult(intent=mapped, confidence=0.8, source="polar")

        return ClsResult(intent="UNKNOWN", confidence=0.3, source="keyword")

    # ---------------- 槽位 ----------------
    def _extract_slots(self, node_id: str, text: str) -> dict:
        slots: dict = {}
        cnt, per = parse_installment(text)
        if cnt:
            slots["installment_count"] = cnt
        if per:
            slots["installment_amount"] = per

        amt = parse_amount(text)
        if amt and "installment_amount" not in slots:
            if node_id in INSTALLMENT_NODES or "每期" in text or "一期" in text:
                slots["installment_amount"] = amt
            else:
                slots["repayment_amount"] = amt

        if node_id == "N022":
            cb = parse_callback_time(text)
            if cb:
                slots["callback_time"] = cb
        else:
            d = parse_date_text(text)
            if d:
                slots["repayment_date"] = d
        return slots

    def _infer_plan(self, snap, node_id: str, r: ClsResult) -> ClsResult:
        s = r.slots
        full_plan = (s.get("repayment_amount") and s.get("repayment_date")) or \
                    (s.get("installment_count") and s.get("installment_amount"))
        partial = any(s.get(k) for k in ("repayment_amount", "repayment_date",
                                         "installment_count", "installment_amount"))
        weak = r.intent in ("UNKNOWN", "AFFIRM", "NO_MONEY", "HESITATE")
        if node_id == "N022" and s.get("callback_time") and r.intent in ("UNKNOWN", "AFFIRM", "REQUEST_CALLBACK", "PROVIDE_PLAN"):
            r.intent, r.confidence, r.source = "PROVIDE_PLAN", max(r.confidence, 0.8), "slots"
            return r
        if full_plan and r.intent not in ("REFUSE_MEDIATION", "REQUEST_HUMAN",
                                          "COMPLAINT_THREAT", "ABUSIVE_LANGUAGE", "FRAUD_SUSPICION"):
            r.intent, r.confidence, r.source = "PROVIDE_PLAN", max(r.confidence, 0.8), "slots"
        elif partial and weak and node_id in {"N017", "N018", "N019", "N021"}:
            r.intent, r.confidence, r.source = "PROVIDE_PLAN", max(r.confidence, 0.7), "slots"
        return r

    @staticmethod
    def _emotion(text: str, intent: str) -> str:
        if intent in ("ABUSIVE_LANGUAGE", "COMPLAINT_THREAT") or any(k in text for k in _EMOTION_HOT):
            return "激动"
        if any(k in text for k in _EMOTION_LOW):
            return "低落"
        return "平稳"
