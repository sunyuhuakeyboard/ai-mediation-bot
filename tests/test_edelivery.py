"""电子送达通知场景测试。"""
from __future__ import annotations

from app.config import Settings
from app.engines.call_state import CallState
from app.engines.edelivery_orchestrator import (ED_ADDRESS, ED_ADDRESS_NEW,
                                                ED_EDELIVERY, ED_NON_SELF,
                                                ElectronicDeliveryOrchestrator)


CASE = {
    "case_id": "CASE_ED_001",
    "respondent_name": "刘立华",
    "respondent_dir": "贵阳市观山湖区林城东路205号405室",
    "plaintiff_name": "贵阳天某有限公司",
    "court_name": "杭州市拱墅区人民法院",
    "court_contact": "0851-376428",
    "lawsuit_type": "买卖合同纠纷",
    "claim_amount": "12500元",
}


def _orch():
    return ElectronicDeliveryOrchestrator(Settings(offline_mode=True))


class FakeLLM:
    def __init__(self, reply: str | None):
        self.reply = reply
        self.calls = []

    async def short_reply(self, messages, max_chars=None):
        self.calls.append({"messages": messages, "max_chars": max_chars})
        return self.reply


async def test_edelivery_agree_main_flow_ends_call():
    orch = _orch()
    state = CallState(call_id="ED_T1", case=dict(CASE))

    opening = await orch.opening(state)
    assert "杭州市拱墅区人民法院立案庭" in opening.reply
    assert "刘立华" in opening.reply

    r1 = await orch.handle_turn(state, "是我，什么事")
    assert r1.node_after == ED_EDELIVERY
    assert "原告贵阳天某有限公司" in r1.reply
    assert "是否同意本案采用电子送达方式" in r1.reply

    r2 = await orch.handle_turn(state, "同意电子送达")
    assert r2.end_call is True
    assert r2.call_result == "同意电子送达"
    assert "同意接受电子送达" in r2.reply
    assert "再见" in r2.reply


async def test_edelivery_refuse_collects_paper_delivery_address():
    orch = _orch()
    state = CallState(call_id="ED_T2", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是本人")

    refused = await orch.handle_turn(state, "不同意，我要纸质邮寄")
    assert refused.node_after == ED_ADDRESS
    assert "不同意电子送达" in refused.reply
    assert "是否为您的现住地址" in refused.reply

    wrong = await orch.handle_turn(state, "地址不对，我搬家了")
    assert wrong.node_after == ED_ADDRESS_NEW
    assert "当前可以接收快递的收件地址" in wrong.reply

    done = await orch.handle_turn(state, "贵阳市云岩区中华北路100号")
    assert done.end_call is True
    assert done.call_result == "地址确认"
    assert state.slots["delivery_address"] == "贵阳市云岩区中华北路100号"


async def test_edelivery_faq_answers_then_returns_to_delivery_confirmation():
    orch = _orch()
    state = CallState(call_id="ED_T3", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "是我")

    faq = await orch.handle_turn(state, "电子送达和纸质送达法律效力一样吗")
    assert faq.node_after == ED_EDELIVERY
    assert "同等法律效力" in faq.reply
    assert "是否同意" in faq.reply
    assert state.ended is False


async def test_edelivery_explains_delivery_method_before_confirmation():
    orch = _orch()
    state = CallState(call_id="ED_T3_METHOD", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是")

    answer = await orch.handle_turn(state, "怎么送的？")

    assert answer.node_after == ED_EDELIVERY
    assert answer.intent == "FAQ"
    assert "线上向您发送诉讼文书" in answer.reply
    assert "是否同意本案采用电子送达" in answer.reply
    assert state.ended is False


async def test_edelivery_topic_keyword_explains_instead_of_repeating_prompt():
    orch = _orch()
    state = CallState(call_id="ED_T3_TOPIC", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "是我")

    answer = await orch.handle_turn(state, "电子送达。")

    assert answer.node_after == ED_EDELIVERY
    assert answer.intent == "FAQ"
    assert "微法院" in answer.reply
    assert answer.reply != "现向你确认，是否同意本案采用电子送达方式接收诉讼文书？电子送达与纸质送达具有同等法律效力。"
    assert state.ended is False


async def test_edelivery_colloquial_agreement_after_explanation_ends_call():
    orch = _orch()
    state = CallState(call_id="ED_T3_AGREE", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "怎么送的？")

    agreed = await orch.handle_turn(state, "可以啊。")

    assert agreed.end_call is True
    assert agreed.intent == "AGREE_EDELIVERY"
    assert agreed.call_result == "同意电子送达"


async def test_edelivery_asr_followup_about_method_gets_answer():
    orch = _orch()
    state = CallState(call_id="ED_T3_ASR", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是")

    answer = await orch.handle_turn(state, "这样子送的。")

    assert answer.node_after == ED_EDELIVERY
    assert answer.intent == "FAQ"
    assert "线上向您发送诉讼文书" in answer.reply
    assert state.ended is False


async def test_edelivery_identity_denial_does_not_advance_to_notice():
    orch = _orch()
    state = CallState(call_id="ED_T4", case=dict(CASE))
    await orch.opening(state)

    denied = await orch.handle_turn(state, "不是。")

    assert denied.node_after == ED_NON_SELF
    assert denied.intent == "NOT_SELF"
    assert "认识刘立华" in denied.reply
    assert state.slots["identity_confirmed"] is False
    assert state.slots["not_self"] is True
    assert state.ended is False


async def test_edelivery_identity_denial_after_notice_recovers_to_non_self():
    orch = _orch()
    state = CallState(call_id="ED_T5", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "是我")

    denied = await orch.handle_turn(state, "我说我不是。")

    assert denied.node_after == ED_NON_SELF
    assert denied.intent == "NOT_SELF"
    assert state.slots["identity_confirmed"] is False
    assert state.slots["not_self"] is True
    assert state.slots.get("electronic_delivery_agreed") is not True
    assert state.ended is False


async def test_edelivery_short_unrelated_text_does_not_count_as_agreement():
    orch = _orch()
    state = CallState(call_id="ED_T6", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "是我")

    unclear = await orch.handle_turn(state, "行业。")

    assert unclear.node_after == ED_EDELIVERY
    assert unclear.intent == "UNKNOWN"
    assert "是否同意本案采用电子送达方式" in unclear.reply
    assert state.slots.get("electronic_delivery_agreed") is not True
    assert state.ended is False


async def test_edelivery_non_self_not_know_routes_to_wrong_number():
    orch = _orch()
    state = CallState(call_id="ED_T7", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "不是")

    result = await orch.handle_turn(state, "不认识")

    assert result.end_call is True
    assert result.call_result == "号码错误"
    assert "号码搞错" in result.reply


async def test_edelivery_unknown_turn_uses_llm_fallback_and_stays_on_task():
    llm = FakeLLM("我理解您的顾虑，电子送达只是接收法院文书的一种线上方式。")
    orch = ElectronicDeliveryOrchestrator(Settings(offline_mode=True), llm=llm)
    state = CallState(call_id="ED_T8", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是")

    result = await orch.handle_turn(state, "我还是有点搞不清楚这个东西。")

    assert result.node_after == ED_EDELIVERY
    assert result.action_type == "LLM_FALLBACK"
    assert result.intent == "FREEFORM"
    assert result.llm_used is True
    assert result.end_call is False
    assert "我理解您的顾虑" in result.reply
    assert "是否同意本案采用电子送达" in result.reply
    assert llm.calls


async def test_edelivery_llm_fallback_prompt_has_commercial_guardrails():
    llm = FakeLLM("可以先确认身份，我这边不会在未确认前披露案情。")
    orch = ElectronicDeliveryOrchestrator(Settings(offline_mode=True), llm=llm)
    state = CallState(call_id="ED_T9", case=dict(CASE))
    await orch.opening(state)

    result = await orch.handle_turn(state, "你们先说清楚。")

    prompt_text = "\n".join(m["content"] for m in llm.calls[0]["messages"])
    assert result.llm_used is True
    assert "未确认前不要披露具体案情" in prompt_text
    assert "不得替用户确认同意、不同意、本人或非本人" in prompt_text
    assert "不编造案号、金额、开庭时间、法院地址或处理结果" in prompt_text
    assert CASE["plaintiff_name"] not in prompt_text
    assert CASE["lawsuit_type"] not in prompt_text
    assert CASE["claim_amount"] not in prompt_text


async def test_edelivery_bad_llm_fallback_is_filtered_to_rule_prompt():
    llm = FakeLLM("对不起，我们暂时无法提供该服务。")
    orch = ElectronicDeliveryOrchestrator(Settings(offline_mode=True), llm=llm)
    state = CallState(call_id="ED_T10", case=dict(CASE))
    await orch.opening(state)
    await orch.handle_turn(state, "我是")

    result = await orch.handle_turn(state, "我还是有点搞不清楚这个东西。")

    assert result.llm_used is False
    assert result.action_type == "ASK_EDELIVERY"
    assert "暂时无法提供" not in result.reply
    assert "是否同意本案采用电子送达方式" in result.reply
