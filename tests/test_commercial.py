"""商用化增强功能测试。

覆盖：否定语境不误判方案 / 开场合规披露 / 变体轮换防复读 / ASR碎片合并 /
UNKNOWN长尾受限LLM应答 / 金额异常复核 / 外呼策略（勿扰/频次/DNC）/
"别再打"自动入DNC / 知识引用校验 / LLM提示词记忆注入。
"""
import os

os.environ.setdefault("OFFLINE_MODE", "1")
os.environ.setdefault("LLM_API_KEY", "")

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from app.cache.knowledge_cache import KnowledgeCache  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.engines.call_state import CallState, StateStore  # noqa: E402
from app.engines.classifier import IntentClassifier  # noqa: E402
from app.engines.compliance import ComplianceEngine  # noqa: E402
from app.engines.orchestrator import DialogOrchestrator  # noqa: E402
from app.engines.prompt_builder import build_messages  # noqa: E402
from app.knowledge import seed  # noqa: E402
from app.knowledge.seed import DEMO_CASE  # noqa: E402
from app.knowledge.validate import validate_refs  # noqa: E402
from app.services.call_service import CallBlocked, CallService  # noqa: E402
from app.utils.text import is_negated_plan  # noqa: E402

DAY = datetime(2026, 6, 10, 14, 30)     # 白天，非勿扰时段
NIGHT = datetime(2026, 6, 10, 22, 0)


class FakeLLM:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = 0
        self.last_prompt = ""

    async def short_reply(self, messages, max_chars=None):
        self.calls += 1
        self.last_prompt = messages[-1]["content"]
        return self.replies.pop(0) if self.replies else None


def make_orchestrator(llm=None):
    settings = get_settings()
    cache = KnowledgeCache()
    cache.load_from_seed()
    classifier = IntentClassifier(settings, http=None)
    return DialogOrchestrator(cache, classifier, llm, ComplianceEngine(), settings)


async def new_call(orch, call_id="C1"):
    state = CallState(call_id=call_id, case=dict(DEMO_CASE))
    opening = await orch.opening(state)
    return state, opening


async def to_plan_stage(orch, state):
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    await orch.handle_turn(state, "愿意")          # -> N010 -> chain N017 方案沟通


# ================= 否定语境 =================
def test_negation_detector():
    assert is_negated_plan("我不可能下个月还1000")
    assert is_negated_plan("一万六我哪有这么多钱")
    assert not is_negated_plan("下个月10号还1000")
    # 让步+承诺不得误杀
    assert not is_negated_plan("实在拿不出太多，下个月还1000吧")


async def test_negated_plan_not_confirmed():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    await to_plan_stage(orch, state)
    assert state.current_node == "N017"

    r = await orch.handle_turn(state, "我不可能下个月还1000的")
    assert r.intent == "NO_MONEY"                       # 否定语境→无力还款，而非方案
    assert "repayment_amount" not in state.slots        # 金额不得入档
    assert state.current_node == "N018"
    assert "对吗" not in r.reply                        # 绝不进入方案确认复述


# ================= 开场合规披露 =================
async def test_opening_disclosure():
    orch = make_orchestrator(FakeLLM())
    _, opening = await new_call(orch)
    assert "录音" in opening.reply and "智能" in opening.reply
    assert "橘子分期" not in opening.reply              # 披露语不得夹带案件信息


# ================= 变体轮换 =================
async def test_variant_rotation_no_parrot():
    orch = make_orchestrator(FakeLLM())                 # LLM恒None→走模板+变体
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    r1 = await orch.handle_turn(state, "我没钱")        # N009->N018 TPL_NO_MONEY 主文本
    r2 = await orch.handle_turn(state, "真的没钱")      # N018->N018 同模板 → 变体1
    assert r1.reply != r2.reply
    assert "难处" in r2.reply or "商量" in r2.reply


# ================= ASR碎片合并 =================
async def test_asr_fragment_merge():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")             # 监听 N009

    r1 = await orch.handle_turn(state, "我想分")        # 碎片1：无法识别
    assert r1.action_type == "FALLBACK"
    assert state.last_fragment == "我想分"

    r2 = await orch.handle_turn(state, "期还可以吗")     # 碎片2：拼接后= 我想分期还可以吗
    assert r2.intent == "REQUEST_INSTALLMENT"
    assert state.current_node == "N019"
    assert state.last_fragment is None


# ================= UNKNOWN长尾：受限LLM应答 =================
async def test_freeform_fallback_with_llm():
    llm = FakeLLM(replies=["这个我帮您记下来了。"])
    orch = make_orchestrator(llm)
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")             # 监听 N009

    r = await orch.handle_turn(state, "你们办公地点在什么区")
    assert r.action_type == "FALLBACK" and r.llm_used is True
    assert "记下来" in r.reply
    assert "愿意" in r.reply                            # 自动拉回节点主问句
    assert state.current_node == "N009"
    assert llm.calls == 1
    # 提示词应含长尾策略与记忆组件
    assert "不在标准流程内" in llm.last_prompt
    assert "最近对话" in llm.last_prompt


async def test_freeform_llm_output_still_compliance_checked():
    llm = FakeLLM(replies=["你再不处理我们就起诉你，列入失信名单。"])
    orch = make_orchestrator(llm)
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    r = await orch.handle_turn(state, "嗯嗯嗯啊啊啊")    # UNKNOWN → freeform输出违规
    assert r.compliance["passed"] is False
    assert "起诉" not in r.reply and "失信" not in r.reply


# ================= 金额合理性复核 =================
async def test_amount_anomaly_recheck():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    await to_plan_stage(orch, state)

    r = await orch.handle_turn(state, "我下个月10号一次还十万")   # 16000欠款 还10万→异常
    assert "再说一遍" in r.reply
    assert "repayment_amount" not in state.slots         # 异常金额已清除
    assert "AMOUNT_ANOMALY" in state.risk_flags
    assert state.current_node == "N017"
    assert "对吗" not in r.reply

    r = await orch.handle_turn(state, "还一千")           # 复述正常金额 → 进确认
    assert state.current_node == "N021"
    assert "1000" in r.reply and "下个月10号" in r.reply


# ================= 外呼策略 =================
async def test_outbound_dnd_window():
    svc = CallService(get_settings(), StateStore())
    with pytest.raises(CallBlocked) as e:
        await svc.start_call(dict(DEMO_CASE), now=NIGHT)
    assert e.value.kind == "dnd"
    # force=True（人工强呼/呼入）可跳过
    state = await svc.start_call(dict(DEMO_CASE), now=NIGHT, force=True)
    assert state.call_id


async def test_outbound_daily_limit():
    svc = CallService(get_settings(), StateStore())
    case = dict(DEMO_CASE, debtor_phone="139****0001")
    await svc.start_call(case, now=DAY)
    await svc.start_call(case, now=DAY)
    with pytest.raises(CallBlocked) as e:
        await svc.start_call(case, now=DAY)               # 默认上限2次/日
    assert e.value.kind == "freq"


async def test_dnc_blocks_and_admin_remove():
    svc = CallService(get_settings(), StateStore())
    case = dict(DEMO_CASE, debtor_phone="139****0002")
    await svc.add_dnc(case["debtor_phone"], "测试登记")
    with pytest.raises(CallBlocked) as e:
        await svc.start_call(case, now=DAY)
    assert e.value.kind == "dnc"
    await svc.remove_dnc(case["debtor_phone"])
    state = await svc.start_call(case, now=DAY)
    assert state.call_id


async def test_user_stop_contact_enrolls_dnc():
    svc = CallService(get_settings(), StateStore())
    orch = make_orchestrator(FakeLLM())
    case = dict(DEMO_CASE, debtor_phone="139****0003")
    state = await svc.start_call(case, now=DAY)
    await orch.opening(state)
    r = await orch.handle_turn(state, "不要再打了，别再打给我")
    assert r.end_call and state.slots.get("dnc_request") is True
    svc.persist_turn(state, r, "不要再打了，别再打给我")
    await svc.drain()
    assert await svc.is_dnc(case["debtor_phone"]) is True
    with pytest.raises(CallBlocked):                       # 后续外呼被强制拦截
        await svc.start_call(case, now=DAY)


# ================= 知识引用校验 =================
def test_validate_refs_seed_clean_and_detects_bad():
    assert validate_refs(seed.NODES, seed.ROUTES, seed.TEMPLATES,
                         seed.STRATEGIES, seed.COMPONENTS) == []
    bad = [dict(route_id="RBAD", current_node="N999", intent_label="X",
                next_node="N888", template_id="TPL_NOPE",
                strategy_id="STR_NOPE", prompt_component_ids=["GHOST"],
                slot_condition={})]
    issues = validate_refs(seed.NODES, bad, seed.TEMPLATES,
                           seed.STRATEGIES, seed.COMPONENTS)
    assert len(issues) == 5


# ================= LLM提示词：记忆/已知信息/防重复 =================
def test_prompt_memory_injection():
    cache = KnowledgeCache()
    snap = cache.load_from_seed()
    route = {"prompt_component_ids": [], "action_type": "LLM_SHORT_REPLY"}
    node = snap.nodes["N018"]
    strategy = snap.strategies["STR_NO_MONEY"]
    template = snap.templates["TPL_NO_MONEY_001"]

    class C:
        intent, objection, risk = "NO_MONEY", "NO_PAYMENT_ABILITY", "中"

    history = [["user", "我没钱"], ["bot", "理解您的情况，您可以先说下目前能接受的时间或金额。"]]
    slots = {"identity_confirmed": True, "mediation_willingness": "愿意", "no_money": True}
    msgs = build_messages(snap, route, node, strategy, template, C(),
                          "真的没钱", {"name": "张三"}, slots, history=history)
    prompt = msgs[0]["content"]
    assert "最近对话" in prompt and "调解员：理解您的情况" in prompt
    assert "已确认本人" in prompt and "调解意愿：愿意" in prompt
    assert "请换不同的措辞" in prompt
    assert "不超过30字" in prompt


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
