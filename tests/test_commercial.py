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


# ================= 调解口吻：角色提示词 / TONE 组件 =================
async def test_prompt_contains_mediation_neutral_third_party_framing():
    """LLM 提示词需明确"独立第三方/不催收/不施压"，避免被理解为催收。"""

    class CaptureLLM:
        def __init__(self): self.last = ""
        async def short_reply(self, messages, max_chars=None):
            self.last = messages[-1]["content"]
            return None
        async def complete_short(self, messages, **kw):
            return None

    llm = CaptureLLM()
    orch = make_orchestrator(llm)
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    await orch.handle_turn(state, "现在确实没钱")    # 触发 N018 LLM_SHORT_REPLY
    assert "独立第三方" in llm.last or "不催收" in llm.last
    assert "不施压" in llm.last


async def test_tone_component_injected_when_emotion_not_calm():
    """情绪激动时 TONE 组件应进入提示词，'平稳'时不应进入（节省 prompt 长度）。"""

    class CaptureLLM:
        def __init__(self): self.last = ""
        async def short_reply(self, messages, max_chars=None):
            self.last = messages[-1]["content"]
            return None
        async def complete_short(self, messages, **kw):
            return None

    llm = CaptureLLM()
    orch = make_orchestrator(llm)
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    # 激动用户：含愤怒关键词，将被分类为"激动"情绪 → TONE 必现
    await orch.handle_turn(state, "烦死了，凭什么我得还")
    assert "用户当前情绪" in llm.last
    assert "承接情绪" in llm.last or "放慢节奏" in llm.last


def test_closing_template_carries_mediator_followup_commitment():
    """调解结束语应承诺如实反馈、礼貌结束，符合中立角色。"""
    cache = KnowledgeCache(); cache.load_from_seed()
    snap = cache.snap()
    end_tpl = snap.templates["TPL_END_001"]
    assert "如实反馈" in end_tpl["template_text"]
    assert "感谢" in end_tpl["template_text"]
    assert "再见" in end_tpl["template_text"]


# ================= UNKNOWN长尾：受限LLM应答 =================
# ================= LLM 兜底意图分类 =================
async def test_llm_classifier_promotes_unknown_to_known_intent():
    """关键词分类返回 UNKNOWN 时，由 LLM 兜底打标签，路由命中而非进入 fallback。"""

    class StubLLM:
        def __init__(self):
            self.last_prompt = ""

        async def complete_short(self, messages, max_tokens=40, timeout_ms=None):
            self.last_prompt = messages[-1]["content"]
            return '{"intent":"ALREADY_PAID","confidence":0.9}'

        async def short_reply(self, messages, max_chars=None):
            return None

    settings = get_settings()
    cache = KnowledgeCache(); cache.load_from_seed()
    llm = StubLLM()
    classifier = IntentClassifier(settings, http=None, llm=llm)
    orch = DialogOrchestrator(cache, classifier, llm, ComplianceEngine(), settings)
    state, _ = await new_call(orch, call_id="C_LLMCLS")
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")  # 进 N009

    # 这种偏口语化的表达，关键词规则容易漏识；让 LLM 兜底打成 ALREADY_PAID
    r = await orch.handle_turn(state, "之前那笔钱我前阵子已经处理过了")
    assert r.intent == "ALREADY_PAID"
    assert r.action_type != "FALLBACK"
    assert "您直说" not in r.reply  # 不再走 fallback 模板
    assert "凭证" in r.reply or "核实" in r.reply  # 命中 TPL_PAID_001
    # 提示词应包含候选标签与当前节点上下文
    assert "ALREADY_PAID" in llm.last_prompt
    assert "意图分类器" in llm.last_prompt


async def test_llm_classifier_returns_invalid_intent_falls_through_to_unknown():
    """LLM 返回不在标签集内的字符串时，必须降级为 UNKNOWN 不污染路由。"""

    class StubLLM:
        async def complete_short(self, messages, max_tokens=40, timeout_ms=None):
            return '{"intent":"NOT_A_REAL_LABEL","confidence":0.99}'

        async def short_reply(self, messages, max_chars=None):
            return None

    settings = get_settings()
    cache = KnowledgeCache(); cache.load_from_seed()
    classifier = IntentClassifier(settings, http=None, llm=StubLLM())
    orch = DialogOrchestrator(cache, classifier, StubLLM(), ComplianceEngine(), settings)
    state, _ = await new_call(orch, call_id="C_LLMCLS_BAD")
    r = await orch.handle_turn(state, "啦啦啦啦")
    # 标签无效 → 应仍走 fallback，不会把无效 intent 写进结果
    assert r.action_type == "FALLBACK"
    assert r.intent in ("UNKNOWN", "AFFIRM", "DENY")


# ================= CR005 不得误伤已确认本人金额复述 =================
async def test_compliance_does_not_redact_amount_after_identity_confirmed():
    """生产事故复现：状态里同时有 identity_confirmed=True 和 not_self=True 时，
    方案确认句"我跟您确认一下，您是希望分3期、每期3000元处理，对吗？"不得被 CR005 吞掉。"""
    cache = KnowledgeCache(); cache.load_from_seed()
    snap = cache.snap()
    engine = ComplianceEngine()
    slots = {"identity_confirmed": True, "not_self": True,
             "installment_count": 3, "installment_amount": 3000.0}
    text = "我跟您确认一下，您是希望分3期、每期3000元处理，对吗？"
    res = engine.check(snap, text, slots, dict(DEMO_CASE))
    assert res.passed is True
    assert "为保护隐私" not in res.text
    assert "3000" in res.text and "3期" in res.text


async def test_confirm_self_clears_stale_not_self_slot():
    """CONFIRM_SELF 命中后必须同时把 not_self 拉回 False，避免后续 CR005 误伤。"""
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    state.slots["not_self"] = True  # 模拟早先误判残留
    await orch.handle_turn(state, "是我，什么事？")
    assert state.slots.get("identity_confirmed") is True
    assert state.slots.get("not_self") is False


async def test_llm_classifier_cannot_flip_confirmed_identity_to_not_self():
    """身份已确认后，LLM 兜底分类即便返回 NOT_SELF 也应被门控降级，不污染 not_self 槽位。"""

    class FlipFlopLLM:
        async def complete_short(self, messages, max_tokens=40, timeout_ms=None):
            return '{"intent":"NOT_SELF","confidence":0.9}'

        async def short_reply(self, messages, max_chars=None):
            return None

    settings = get_settings()
    cache = KnowledgeCache(); cache.load_from_seed()
    classifier = IntentClassifier(settings, http=None, llm=FlipFlopLLM())
    orch = DialogOrchestrator(cache, classifier, FlipFlopLLM(), ComplianceEngine(), settings)
    state, _ = await new_call(orch, call_id="C_GUARD")
    state.slots["identity_confirmed"] = True
    # 一个关键词无法识别的尾部表达 → 触发 LLM 兜底
    r = await orch.handle_turn(state, "啦啦啦啦啦啦啦")
    assert r.intent != "NOT_SELF"
    assert state.slots.get("not_self") is not True


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
    assert "没有命中标准流程" in llm.last_prompt
    assert "最近对话" in llm.last_prompt
    # 新增：动态把当前节点主问句作为参考话术喂给模型
    assert "参考话术" in llm.last_prompt


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
