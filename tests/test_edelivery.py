"""电子送达通知场景测试。"""
from __future__ import annotations

from app.config import Settings
from app.engines.call_state import CallState
from app.engines.edelivery_orchestrator import (ED_ADDRESS, ED_ADDRESS_NEW,
                                                ED_EDELIVERY,
                                                ElectronicDeliveryOrchestrator)


CASE = {
    "case_id": "CASE_ED_001",
    "respondent_name": "刘某华",
    "respondent_dir": "贵阳市观山湖区林城东路205号405室",
    "plaintiff_name": "贵阳天某有限公司",
    "court_name": "某某区人民法院",
    "court_contact": "0851-376428",
    "lawsuit_type": "买卖合同纠纷",
    "claim_amount": "12500元",
}


def _orch():
    return ElectronicDeliveryOrchestrator(Settings(offline_mode=True))


async def test_edelivery_agree_main_flow_ends_call():
    orch = _orch()
    state = CallState(call_id="ED_T1", case=dict(CASE))

    opening = await orch.opening(state)
    assert "某某区人民法院立案庭" in opening.reply
    assert "刘某华" in opening.reply

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
