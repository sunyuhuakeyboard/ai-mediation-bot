"""法院电子送达通知场景编排器。

按"身份告知 -> 当事人确认 -> 案件告知 -> 电子送达确认 -> 地址确认"
的简单状态机推进，避免把送达通知场景套进复杂调解策略。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.config import Settings
from app.engines.call_state import CallState
from app.engines.orchestrator import TurnResult
from app.utils.text import sanitize_tts

logger = logging.getLogger(__name__)

ED_IDENTITY = "ED_ID"
ED_NON_SELF = "ED_NS"
ED_PROXY_PHONE = "ED_PROXY"
ED_PROXY_CONFIRM = "ED_PROXY_OK"
ED_EDELIVERY = "ED_ES"
ED_ADDRESS = "ED_ADDR"
ED_ADDRESS_NEW = "ED_ADDR_NEW"
ED_CALLBACK = "ED_CB"

_PHONE_RE = re.compile(r"(?:\+?86[- ]?)?(1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8})")
_AMOUNT_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")

_AFFIRM_EXACT = {
    "是", "是的", "对", "对的", "嗯", "嗯嗯", "本人", "我本人", "我是本人",
    "是本人", "是我", "我就是", "没错", "没错的", "可以", "同意", "接受",
    "行", "好的", "好", "没问题", "可以啊", "可以的", "行啊", "行的",
    "好啊", "好呀", "同意啊", "同意的", "接受啊", "接受的",
}
_AFFIRM_PHRASES = (
    "我是", "是我", "我就是", "我是本人", "是我本人", "对我是", "是的是我", "没错我是",
)
_DENY_IDENTITY = ("不是", "不是本人", "不是我", "我不是", "非本人", "打错", "找错", "号码错", "不认识")
_WRONG_NUMBER = ("打错", "找错", "号码错", "新号码", "不认识", "不认识他", "不认识她")
_KNOWS_PERSON = ("认识", "家人", "亲属", "朋友", "同事", "我老公", "我老婆", "我儿子", "我女儿")
_CALLBACK = ("不方便", "稍后", "晚点", "等会", "改天", "在忙", "开会", "回电", "再打")
_AGITATED = ("骗子", "诈骗", "骗人", "滚", "有病", "投诉", "举报", "别打")
_PROXY = ("律师", "代理人", "委托", "授权")
_REFUSE_ES = ("不同意", "拒绝", "不同意电子", "纸质", "邮寄", "不要电子", "不接受电子", "不想电子")
_AGREE_ES_EXACT = {
    "同意", "接受", "可以", "行", "好的", "好", "没问题", "可以啊", "可以的",
    "行啊", "行的", "好啊", "好呀", "同意啊", "同意的", "接受啊", "接受的",
}
_AGREE_ES_PHRASES = ("同意电子", "接受电子", "可以电子", "电子送达可以", "电子送达同意")
_ADDR_WRONG = ("不是", "不对", "错", "错误", "不是这个地址", "搬家", "不在那")
_REFUSE_ADDR = ("不提供", "不告诉", "不知道", "没有地址", "不方便说")
_SERVICE_REFUSAL_HINTS = (
    "无法提供该服务", "暂时无法提供", "不能提供该服务", "无法提供服务",
    "无法处理该请求", "不能处理该请求", "无法协助", "不能协助",
)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？!?；;,.、]", "", text or "")


def _is_identity_denial(text: str) -> bool:
    t = _norm(text)
    if not t or "是不是" in t or "是否" in t:
        return False
    return any(w in t for w in _DENY_IDENTITY)


def _is_affirm(text: str) -> bool:
    t = _norm(text)
    if (
        not t
        or _is_identity_denial(t)
        or any(w in t for w in ("不同意", "不接受", "不可以", "不行", "拒绝"))
        or any(w in t for w in ("是不是", "是否", "可以吗", "行吗"))
    ):
        return False
    if t in _AFFIRM_EXACT:
        return True
    return any(w in t for w in _AFFIRM_PHRASES)


def _is_edelivery_agree(text: str) -> bool:
    t = _norm(text)
    if not t or _has_any(t, _REFUSE_ES) or _is_identity_denial(t):
        return False
    return t in _AGREE_ES_EXACT or any(w in t for w in _AGREE_ES_PHRASES)


def _fmt_amount(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    if "元" in text:
        return text
    match = _AMOUNT_RE.search(text)
    return f"{match.group(0)} 元" if match else text


def _flatten_case(case: dict) -> dict:
    out = dict(case or {})
    extra = out.get("extra")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in out and k != "okcti":
                out[k] = v
    return out


class ElectronicDeliveryOrchestrator:
    def __init__(self, settings: Settings, llm=None) -> None:
        self.s = settings
        self.llm = llm

    async def opening(self, state: CallState) -> TurnResult:
        state.current_node = ED_IDENTITY
        state.visit(ED_IDENTITY)
        ctx = self._ctx(state)
        reply = self._clean(
            f"您好，我这边是{ctx['court_name']}立案庭，我是立案庭法官助理，请问是{ctx['respondent_name']}吗？"
        )
        state.remember("bot", reply)
        return self._result(state, reply, "OPENING", "ED_OPEN", "OPENING", ED_IDENTITY, ED_IDENTITY)

    async def handle_turn(self, state: CallState, user_text: str) -> TurnResult:
        started = time.perf_counter()
        if state.ended:
            return self._result(state, "本次通话已结束，感谢您的配合。", "ENDED",
                                None, "ENDED", "ENDED", state.current_node, state.current_node,
                                end_call=True)

        node_before = state.current_node or ED_IDENTITY
        text = (user_text or "").strip()
        if not text:
            return self._silence(state, node_before, started)

        state.silence_count = 0
        state.remember("user", text)
        ctx = self._ctx(state)

        if not _CHINESE_RE.search(text):
            reply = self._clean(f"请您用中文沟通。我这边继续为您做法院电子送达通知。{self._prompt_for(state.current_node, ctx)}")
            return self._finish(state, reply, "LANGUAGE", "ED_LANG", "LANGUAGE", node_before, state.current_node, started)

        answer = self._faq_answer(text, state.current_node, ctx)
        if answer:
            reply = self._clean(answer + self._followup_for(state.current_node, ctx))
            return self._finish(state, reply, "FAQ", "ED_FAQ", "FAQ", node_before, state.current_node, started)

        node = state.current_node
        if node == ED_IDENTITY:
            return await self._handle_identity(state, text, ctx, node_before, started)
        if node == ED_NON_SELF:
            return await self._handle_non_self(state, text, ctx, node_before, started)
        if node == ED_PROXY_PHONE:
            return await self._handle_proxy_phone(state, text, ctx, node_before, started)
        if node == ED_PROXY_CONFIRM:
            return await self._handle_proxy_confirm(state, text, ctx, node_before, started)
        if node == ED_EDELIVERY:
            return await self._handle_edelivery(state, text, ctx, node_before, started)
        if node == ED_ADDRESS:
            return await self._handle_address(state, text, ctx, node_before, started)
        if node == ED_ADDRESS_NEW:
            return await self._handle_address_new(state, text, ctx, node_before, started)
        if node == ED_CALLBACK:
            return await self._handle_callback(state, text, ctx, node_before, started)

        state.current_node = ED_IDENTITY
        return await self._llm_or_prompt(state, text, ctx, node_before, started,
                                         fallback=self._prompt_for(ED_IDENTITY, ctx),
                                         node_after=ED_IDENTITY)

    async def _handle_identity(self, state, text, ctx, node_before, started):
        if _has_any(text, _CALLBACK):
            state.current_node = ED_CALLBACK
            reply = "好的，请问您什么时候方便接听法院送达通知？"
            return self._finish(state, reply, "ASK_CALLBACK", "ED_R_CALLBACK", "REQUEST_CALLBACK",
                                node_before, ED_CALLBACK, started)
        if _has_any(text, _PROXY):
            state.current_node = ED_PROXY_PHONE
            reply = "您确认已授权代理人处理本案吗？如确认，请告知代理人联系电话，我记录后由法院联系。"
            return self._finish(state, reply, "ASK_PROXY", "ED_R_PROXY", "PROXY",
                                node_before, ED_PROXY_PHONE, started)
        if _is_identity_denial(text):
            state.slots["not_self"] = True
            state.slots["identity_confirmed"] = False
            state.current_node = ED_NON_SELF
            reply = f"您认识{ctx['respondent_name']}吗？他名下有一件诉讼案件需要本人处理。"
            return self._finish(state, reply, "ASK_RELATION", "ED_R_NOT_SELF", "NOT_SELF",
                                node_before, ED_NON_SELF, started)
        if _has_any(text, _AGITATED) or "你是谁" in text or "什么机构" in text or "哪里" in text:
            reply = f"这是{ctx['court_name']}立案庭，您可拨打{ctx['court_contact']}核实。请问是{ctx['respondent_name']}本人吗？"
            return self._finish(state, reply, "IDENTITY_EXPLAIN", "ED_R_ID_Q", "QUESTION_IDENTITY",
                                node_before, ED_IDENTITY, started)
        if _is_affirm(text):
            state.slots["identity_confirmed"] = True
            state.slots["not_self"] = False
            state.current_node = ED_EDELIVERY
            reply = self._case_notice(ctx) + self._edelivery_question(ctx)
            return self._finish(state, reply, "NOTICE_AND_CONFIRM", "ED_R_SELF", "CONFIRM_SELF",
                                node_before, ED_EDELIVERY, started)
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback=f"我需要先确认您的身份，才能进行下一步通知，请问您是{ctx['respondent_name']}本人吗？",
            node_after=ED_IDENTITY,
        )

    async def _handle_non_self(self, state, text, ctx, node_before, started):
        if _is_affirm(text) and ("本人" in text or "我就是" in text):
            state.slots["identity_confirmed"] = True
            state.slots["not_self"] = False
            state.current_node = ED_EDELIVERY
            reply = self._case_notice(ctx) + self._edelivery_question(ctx)
            return self._finish(state, reply, "NOTICE_AND_CONFIRM", "ED_R_RECOVER", "CONFIRM_SELF",
                                node_before, ED_EDELIVERY, started)
        if _has_any(text, _WRONG_NUMBER):
            return self._end(state, "不好意思，可能号码搞错了，再见。", "WRONG_NUMBER",
                             "ED_R_WRONG", "NOT_KNOW", node_before, started, "号码错误")
        if _has_any(text, _KNOWS_PERSON):
            reply = f"麻烦您转告{ctx['respondent_name']}，我院有涉及他本人的诉讼事项需要核实办理，请尽快拨打{ctx['court_contact']}联系{ctx['court_name']}处理。再见。"
            return self._end(state, reply, "NON_SELF_RELAY", "ED_R_RELAY", "KNOWS_PERSON",
                             node_before, started, "非本人转告")
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback=f"为避免打扰，我再确认一下，您是否认识{ctx['respondent_name']}？",
            node_after=ED_NON_SELF,
        )

    async def _handle_proxy_phone(self, state, text, ctx, node_before, started):
        match = _PHONE_RE.search(text)
        if match:
            phone = match.group(1).replace(" ", "")
            state.slots["respondent_phone_proxy"] = phone
            state.current_node = ED_PROXY_CONFIRM
            reply = f"我复述一下代理人联系电话：{phone}，请问正确吗？"
            return self._finish(state, reply, "CONFIRM_PROXY_PHONE", "ED_R_PROXY_PHONE",
                                "PROXY_PHONE", node_before, ED_PROXY_CONFIRM, started)
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback="请您告知代理人的联系电话，我记录后由法院联系。",
            node_after=ED_PROXY_PHONE,
        )

    async def _handle_proxy_confirm(self, state, text, ctx, node_before, started):
        if _is_affirm(text):
            phone = state.slots.get("respondent_phone_proxy") or ""
            return self._end(state, f"好的，代理人联系电话{phone}我已记录，法院会联系您的代理人。谢谢，再见。",
                             "PROXY_RECORDED", "ED_R_PROXY_OK", "PROXY_CONFIRMED",
                             node_before, started, "代理人处理")
        state.current_node = ED_PROXY_PHONE
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback="好的，请您重新说一下代理人的联系电话。",
            node_after=ED_PROXY_PHONE,
        )

    async def _handle_callback(self, state, text, ctx, node_before, started):
        state.slots["callback_time"] = text
        return self._end(state, f"好的，我记录您方便的时间是{text}，后续法院会再联系您。再见。",
                         "CALLBACK_RECORDED", "ED_R_CALLBACK_OK", "CALLBACK_TIME",
                         node_before, started, "预约回访")

    async def _handle_edelivery(self, state, text, ctx, node_before, started):
        if _is_identity_denial(text):
            state.slots["identity_confirmed"] = False
            state.slots["not_self"] = True
            state.current_node = ED_NON_SELF
            reply = f"好的，那我先更正一下。请问您认识{ctx['respondent_name']}吗？他名下有一件诉讼案件需要本人处理。"
            return self._finish(state, reply, "ASK_RELATION", "ED_R_ES_NOT_SELF", "NOT_SELF",
                                node_before, ED_NON_SELF, started)
        if _has_any(text, _REFUSE_ES):
            state.slots["electronic_delivery_agreed"] = False
            state.current_node = ED_ADDRESS
            reply = f"已如实记录您不同意电子送达，后续我院将依法采取邮寄或直接送达方式。需要确认一下送达地址，{ctx['respondent_dir']}是否为您的现住地址？"
            return self._finish(state, reply, "ASK_ADDRESS", "ED_R_ES_REFUSE", "REFUSE_EDELIVERY",
                                node_before, ED_ADDRESS, started)
        if _is_edelivery_agree(text) or _is_affirm(text):
            state.slots["electronic_delivery_agreed"] = True
            reply = "好的，我记录您同意接受电子送达。请尽快完成微法院实名认证，及时查看案件材料和后续诉讼文书，并保持电话畅通。再见。"
            return self._end(state, reply, "EDELIVERY_AGREED", "ED_R_ES_AGREE", "AGREE_EDELIVERY",
                             node_before, started, "同意电子送达")
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback=self._edelivery_question(ctx),
            node_after=ED_EDELIVERY,
        )

    async def _handle_address(self, state, text, ctx, node_before, started):
        if _has_any(text, _REFUSE_ADDR):
            reply = "您可以不提供地址，法院将按身份证登记地址依法送达。特此告知，再见。"
            return self._end(state, reply, "ADDRESS_REFUSED", "ED_R_ADDR_REFUSE", "REFUSE_ADDRESS",
                             node_before, started, "拒绝提供地址")
        if _has_any(text, _ADDR_WRONG):
            state.current_node = ED_ADDRESS_NEW
            reply = "那请您提供当前可以接收快递的收件地址；如不能提供，我们将按身份证登记地址进行送达。"
            return self._finish(state, reply, "ASK_NEW_ADDRESS", "ED_R_ADDR_WRONG", "ADDRESS_WRONG",
                                node_before, ED_ADDRESS_NEW, started)
        if _is_affirm(text):
            state.slots["delivery_address"] = ctx["respondent_dir"]
            reply = "好的，已记录。请保持通讯地址可正常收件、电话畅通，也可自行登录微法院查询案件信息。再见。"
            return self._end(state, reply, "ADDRESS_CONFIRMED", "ED_R_ADDR_OK", "ADDRESS_CONFIRMED",
                             node_before, started, "地址确认")
        return await self._llm_or_prompt(
            state, text, ctx, node_before, started,
            fallback=f"我再确认一下，{ctx['respondent_dir']}是否为您的现住地址？",
            node_after=ED_ADDRESS,
        )

    async def _handle_address_new(self, state, text, ctx, node_before, started):
        if _has_any(text, _REFUSE_ADDR):
            reply = "您可以不提供地址，法院将按身份证登记地址依法送达。特此告知，再见。"
            return self._end(state, reply, "ADDRESS_REFUSED", "ED_R_ADDR_NO_NEW", "REFUSE_ADDRESS",
                             node_before, started, "拒绝提供地址")
        state.slots["delivery_address"] = text
        reply = "好的，已记录新地址。请保持通讯地址可正常收件、电话畅通，及时查看送达文书。再见。"
        return self._end(state, reply, "NEW_ADDRESS_RECORDED", "ED_R_ADDR_NEW", "NEW_ADDRESS",
                         node_before, started, "地址确认")

    def _faq_answer(self, text: str, node: str, ctx: dict) -> str:
        t = _norm(text)
        if self._is_edelivery_method_question(t):
            return "电子送达是法院通过人民法院在线服务、微法院或预留手机渠道，线上向您发送诉讼文书。"
        if self._is_edelivery_channel_question(t):
            return "同意后，您可在微信搜索微法院，实名认证登录后查看和下载案件材料、传票等诉讼文书。"
        if self._is_edelivery_must_question(t):
            return "电子送达需要您自愿确认；如不同意，法院会依法采用邮寄或直接送达等方式。"
        if self._is_edelivery_help_question(t):
            return "如果您不会操作微法院，可以先选择纸质送达，或拨打法院电话请工作人员协助。"
        if self._is_edelivery_timing_question(t):
            return "电子送达以系统发送和记录的时间为准，您登录后能查看具体文书和送达记录。"
        if self._is_edelivery_safety_question(t):
            return f"这是{ctx['court_name']}的诉讼文书送达通知，您也可拨打{ctx['court_contact']}核实。"
        if "机器人" in text or "真人" in text:
            return f"我是法院的智能法官助理，负责诉讼文书电子送达通知；如需人工服务，可拨打{ctx['court_contact']}。"
        if any(w in text for w in ("怎么知道", "核实", "诈骗", "真假", "法院电话", "官方")):
            return f"您的谨慎是对的。您可拨打{ctx['court_name']}电话{ctx['court_contact']}核实。"
        if any(w in text for w in ("谁告", "被谁告", "告的什么", "什么案", "案由")):
            return f"原告是{ctx['plaintiff_name']}，案由是{ctx['lawsuit_type']}，诉讼请求金额{ctx['claim_amount']}，案件编号{ctx['case_id']}。"
        if any(w in text for w in ("还钱", "还了", "还过", "争议", "为什么起诉")):
            return f"您可在答辩期内向{ctx['court_name']}提交证明材料，法院会依法审查。我这边只负责送达通知。"
        if any(w in text for w in ("材料没收到", "起诉材料", "没收到材料", "文书没收到", "没收到短信", "收不到短信")):
            return f"同意电子送达后，文书会发送至微法院；也可拨打{ctx['court_contact']}联系法院领取纸质版。"
        if any(w in text for w in ("法院在哪", "怎么去法院", "法院地址")):
            return f"具体地址和开庭时间会在传票中注明，也可拨打{ctx['court_contact']}咨询法院工作人员。"
        if "律师" in text:
            return "是否请律师由您自行决定，也可委托符合法律规定的近亲属作为诉讼代理人。"
        if "答辩" in text:
            return f"收到起诉状副本后，您可在15日内向{ctx['court_name']}提交书面答辩状。"
        if any(w in text for w in ("必须去", "不去", "不开庭", "缺席")):
            return "作为被告，不出庭可能导致缺席判决，建议按时出庭或委托代理人。"
        if "调解" in text:
            return "调解是自愿的，您有权拒绝，法院会依法进行后续庭审程序。"
        if "延期" in text:
            return f"如有正当理由，可在开庭前向{ctx['court_name']}书面申请延期，由法院决定是否准许。"
        if any(w in text for w in ("输了", "判我输", "强制执行")):
            return "判决生效后需在确定期限内履行义务；不履行的，原告可依法申请强制执行。"
        if "失信" in text or "老赖" in text:
            return "失信被执行人会受到出行、消费、融资等限制，建议判决生效后依法履行。"
        if "上诉" in text or "不服判决" in text:
            return "如不服判决，您可在判决书送达后15日内向上级法院提起上诉。"
        if "诉讼费" in text:
            return "诉讼费通常先由原告预交，最终由败诉方承担，具体以法院判决为准。"
        if "法律效力" in text or "纸质送达一样" in text:
            return "一样的。电子送达与纸质送达具有同等法律效力，送达时间以系统记录为准。"
        if any(w in text for w in ("后悔", "改回纸质", "变更送达")):
            return f"您可拨打{ctx['court_contact']}联系{ctx['court_name']}工作人员申请变更送达方式。"
        return ""

    def _is_edelivery_method_question(self, text: str) -> bool:
        if not text:
            return False
        if text in ("电子送达", "线上送达", "网上送达"):
            return True
        return (
            ("送" in text or "电子送达" in text)
            and any(w in text for w in ("怎么", "如何", "咋", "什么", "怎样", "这样子", "这种", "方式"))
        )

    def _is_edelivery_channel_question(self, text: str) -> bool:
        return any(w in text for w in (
            "发到哪里", "发哪里", "送到哪里", "在哪里看", "哪里看", "在哪看",
            "文书哪里看", "怎么查看", "怎么查", "怎么收", "在哪里收", "微法院",
            "微信", "短信", "手机", "链接", "平台", "app",
        ))

    def _is_edelivery_must_question(self, text: str) -> bool:
        return any(w in text for w in (
            "必须同意", "一定要同意", "不同意会怎样", "不同意可以吗", "能不能不同意",
            "可以不同意", "必须电子", "一定要电子",
        ))

    def _is_edelivery_help_question(self, text: str) -> bool:
        return any(w in text for w in (
            "不会操作", "不会弄", "不会用", "没有微信", "没有手机", "手机不会",
            "老人", "弄不了", "打不开", "登不上", "收不到",
        ))

    def _is_edelivery_timing_question(self, text: str) -> bool:
        return any(w in text for w in (
            "什么时候算送达", "何时送达", "送达时间", "多久送达", "几天送达",
            "什么时候收到", "多久收到",
        ))

    def _is_edelivery_safety_question(self, text: str) -> bool:
        return any(w in text for w in (
            "安全吗", "安全不", "可靠吗", "风险", "隐私", "泄露", "会不会被骗",
        ))

    async def _llm_or_prompt(self, state: CallState, user_text: str, ctx: dict,
                             node_before: str, started: float, *,
                             fallback: str, node_after: str) -> TurnResult:
        """策略未命中时的商用兜底：模型只生成话术，不直接推进业务状态。"""
        if self.llm is not None and self.s.freeform_fallback_llm:
            try:
                messages = self._llm_messages(state, user_text, ctx, node_after)
                out = await self.llm.short_reply(messages, max_chars=110)
                out = self._sanitize_llm_reply(out)
                if out:
                    reply = self._ensure_followup(out, node_after, ctx)
                    logger.info(
                        "edelivery llm fallback ok call=%s node=%s user=%r reply=%r",
                        state.call_id, node_after, self._preview(user_text),
                        self._preview(reply),
                    )
                    return self._finish(state, reply, "LLM_FALLBACK", "ED_R_LLM",
                                        "FREEFORM", node_before, node_after, started,
                                        llm_used=True)
                logger.warning(
                    "edelivery llm fallback empty call=%s node=%s user=%r",
                    state.call_id, node_after, self._preview(user_text),
                )
            except Exception:
                logger.exception(
                    "edelivery llm fallback failed call=%s node=%s user=%r",
                    state.call_id, node_after, self._preview(user_text),
                )
        reply = self._clean(fallback)
        return self._finish(state, reply, self._fallback_action(node_after),
                            "ED_R_RULE_FALLBACK", "UNKNOWN",
                            node_before, node_after, started)

    def _llm_messages(self, state: CallState, user_text: str, ctx: dict,
                      node: str) -> list[dict]:
        history = []
        for role, text in (state.history or [])[-6:]:
            if text:
                history.append(f"{role}:{text}")
        facts = {
            "法院": ctx["court_name"],
            "法院电话": ctx["court_contact"],
            "当事人": ctx["respondent_name"],
        }
        if node not in (ED_IDENTITY, ED_NON_SELF):
            facts.update({
                "原告": ctx["plaintiff_name"],
                "案由": ctx["lawsuit_type"],
                "诉讼请求金额": ctx["claim_amount"],
                "案件编号": ctx["case_id"],
                "登记地址": ctx["respondent_dir"],
            })
        system = (
            "你是法院立案庭的智能法官助理，正在做诉讼文书电子送达电话通知。"
            "请只根据给定事实回答用户，不编造案号、金额、开庭时间、法院地址或处理结果。"
            "不得提供法律结论或诉讼策略，不承诺胜诉败诉，不评价案件实体。"
            "不得说无法提供服务、无法协助、请咨询律师这类拒绝句。"
            "不得替用户确认同意、不同意、本人或非本人。"
            "输出中文电话口语，一句话，80字以内，最后自然回到当前任务。"
        )
        user = (
            f"当前任务：{self._llm_task(node, ctx)}\n"
            f"案件事实：{facts}\n"
            f"最近对话：{' | '.join(history) if history else '无'}\n"
            f"用户刚说：{user_text}\n"
            "请生成客服下一句。"
        )
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _llm_task(self, node: str, ctx: dict) -> str:
        tasks = {
            ED_IDENTITY: f"先保护隐私并确认是否{ctx['respondent_name']}本人，未确认前不要披露具体案情。",
            ED_NON_SELF: f"确认对方是否认识{ctx['respondent_name']}，如认识请转告本人联系法院。",
            ED_PROXY_PHONE: "围绕代理人或授权问题回应，并请对方提供代理人联系电话。",
            ED_PROXY_CONFIRM: "确认代理人联系电话是否正确，如不正确请重新提供。",
            ED_EDELIVERY: "回答用户关于电子送达、材料查看、效力、风险、操作的问题，最后询问是否同意电子送达。",
            ED_ADDRESS: "回答纸质送达或地址相关问题，最后确认登记地址是否为现住地址。",
            ED_ADDRESS_NEW: "请用户提供当前可以接收快递的收件地址。",
            ED_CALLBACK: "确认用户方便接听法院通知的回访时间。",
        }
        return tasks.get(node, tasks[ED_IDENTITY])

    def _sanitize_llm_reply(self, text: str | None) -> str:
        text = self._clean(text or "")
        if not text or len(text) < 4:
            return ""
        if any(h in text for h in _SERVICE_REFUSAL_HINTS):
            return ""
        if any(h in text for h in ("作为AI", "作为人工智能", "我不能", "我无法")):
            return ""
        return text[:120]

    def _ensure_followup(self, reply: str, node: str, ctx: dict) -> str:
        reply = reply.rstrip("。；;")
        followup = self._followup_for(node, ctx)
        norm_reply = _norm(reply)
        norm_followup = _norm(followup)
        if norm_followup and norm_followup not in norm_reply:
            reply = f"{reply}，{followup}"
        return self._clean(reply)

    def _fallback_action(self, node: str) -> str:
        return {
            ED_IDENTITY: "ASK_IDENTITY",
            ED_NON_SELF: "ASK_RELATION",
            ED_PROXY_PHONE: "ASK_PROXY_PHONE",
            ED_PROXY_CONFIRM: "CONFIRM_PROXY_PHONE",
            ED_EDELIVERY: "ASK_EDELIVERY",
            ED_ADDRESS: "ASK_ADDRESS",
            ED_ADDRESS_NEW: "ASK_NEW_ADDRESS",
            ED_CALLBACK: "ASK_CALLBACK",
        }.get(node, "FALLBACK")

    def _case_notice(self, ctx: dict) -> str:
        return f"好的，{ctx['respondent_name']}，现原告{ctx['plaintiff_name']}已就{ctx['lawsuit_type']}一案，向我院对你提起立案起诉。"

    def _edelivery_question(self, ctx: dict) -> str:
        return "现向你确认，是否同意本案采用电子送达方式接收诉讼文书？电子送达与纸质送达具有同等法律效力。"

    def _prompt_for(self, node: str, ctx: dict) -> str:
        prompts = {
            ED_IDENTITY: f"请问您是{ctx['respondent_name']}本人吗？",
            ED_NON_SELF: f"请问您认识{ctx['respondent_name']}吗？",
            ED_PROXY_PHONE: "请告知代理人联系电话。",
            ED_PROXY_CONFIRM: "请问联系电话是否正确？",
            ED_EDELIVERY: self._edelivery_question(ctx),
            ED_ADDRESS: f"请问{ctx['respondent_dir']}是否为您的现住地址？",
            ED_ADDRESS_NEW: "请提供当前可以接收快递的收件地址。",
            ED_CALLBACK: "请问您什么时候方便接听？",
        }
        return prompts.get(node, prompts[ED_IDENTITY])

    def _followup_for(self, node: str, ctx: dict) -> str:
        if node == ED_EDELIVERY:
            return "了解后，请问您是否同意本案采用电子送达？"
        if node == ED_ADDRESS:
            return f"请问{ctx['respondent_dir']}是否为您的现住地址？"
        if node == ED_IDENTITY:
            return f"请问您是{ctx['respondent_name']}本人吗？"
        return self._prompt_for(node, ctx)

    def _ctx(self, state: CallState) -> dict:
        case = _flatten_case(state.case or {})
        court_name = case.get("court_name") or case.get("mediation_org") or self.s.edelivery_default_court_name
        respondent = case.get("respondent_name") or case.get("debtor_name") or self.s.edelivery_default_respondent_name
        amount = _fmt_amount(case.get("claim_amount") or case.get("total_amount") or self.s.edelivery_default_claim_amount)
        return {
            "case_id": case.get("case_id") or state.call_id,
            "plaintiff_name": case.get("plaintiff_name") or case.get("creditor_name") or self.s.edelivery_default_plaintiff_name,
            "respondent_name": respondent,
            "respondent_dir": case.get("respondent_dir") or case.get("address") or self.s.edelivery_default_respondent_dir,
            "claim_amount": amount,
            "court_name": court_name,
            "lawsuit_type": case.get("lawsuit_type") or self.s.edelivery_default_lawsuit_type,
            "court_contact": case.get("court_contact") or case.get("official_verify_channel") or self.s.edelivery_default_court_contact,
        }

    def _silence(self, state: CallState, node_before: str, started: float) -> TurnResult:
        state.silence_count += 1
        ctx = self._ctx(state)
        if state.silence_count > self.s.okcti_silence_max_turns:
            return self._end(state, "暂时没有听到您的回应，稍后法院会再联系您，再见。", "SILENCE_END",
                             "ED_R_SILENCE_END", "SILENCE", node_before, started, "用户未回应")
        reply = self._clean(f"您好，您还在吗？{self._prompt_for(state.current_node, ctx)}")
        return self._finish(state, reply, "SILENCE_PROMPT", "ED_R_SILENCE", "SILENCE",
                            node_before, state.current_node, started)

    def _end(self, state: CallState, reply: str, action: str, route: str, intent: str,
             node_before: str, started: float, call_result: str) -> TurnResult:
        state.ended = True
        state.call_result = call_result
        return self._finish(state, reply, action, route, intent, node_before,
                            state.current_node, started, end_call=True)

    def _finish(self, state: CallState, reply: str, action: str, route: str | None,
                intent: str, node_before: str, node_after: str, started: float,
                *, end_call: bool = False, llm_used: bool = False) -> TurnResult:
        reply = self._clean(reply)
        state.current_node = node_after
        state.visit(node_after)
        state.turn_index += 1
        state.remember("bot", reply)
        return self._result(
            state, reply, action, route, intent, node_before, node_after,
            end_call=end_call, llm_used=llm_used,
            latency_ms={"total": int((time.perf_counter() - started) * 1000)},
        )

    def _result(self, state: CallState, reply: str, action: str, route: str | None,
                intent: str, node_before: str, node_after: str, *,
                end_call: bool = False, llm_used: bool = False,
                latency_ms: dict | None = None) -> TurnResult:
        return TurnResult(
            call_id=state.call_id,
            reply=reply,
            segments=[reply],
            intent=intent,
            confidence=0.9 if intent not in ("UNKNOWN", "SILENCE") else 0.3,
            action_type=action,
            route_id=route,
            node_before=node_before,
            node_after=node_after,
            slots=dict(state.slots),
            end_call=end_call,
            llm_used=llm_used,
            call_result=state.call_result,
            latency_ms=latency_ms or {},
        )

    @staticmethod
    def _clean(text: str) -> str:
        text = sanitize_tts(text)
        text = text.replace("#挂机#", "")
        text = re.sub(r"\s+", "", text)
        return text

    @staticmethod
    def _preview(text: str, limit: int = 120) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."
