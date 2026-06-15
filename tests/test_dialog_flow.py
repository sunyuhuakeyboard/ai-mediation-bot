"""端到端对话流程测试（OFFLINE：种子知识 + 内存状态 + 假LLM，无外部依赖）。

覆盖：完整调解链路 / 分期槽位收集闭环 / 预约回访 / 非本人 / 怀疑诈骗 /
转人工 / 合规拦截修复 / fallback重试 / 延迟打点。
"""
import os

os.environ.setdefault("OFFLINE_MODE", "1")
os.environ.setdefault("LLM_API_KEY", "")

import pytest  # noqa: E402

from app.cache.knowledge_cache import KnowledgeCache  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.engines.call_state import CallState, StateStore  # noqa: E402
from app.engines.classifier import IntentClassifier  # noqa: E402
from app.engines.compliance import ComplianceEngine  # noqa: E402
from app.engines.orchestrator import DialogOrchestrator  # noqa: E402
from app.knowledge.seed import DEMO_CASE  # noqa: E402


class FakeLLM:
    """可编程假LLM：队列出词；默认返回 None 模拟超时（应回退参考话术）。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = 0

    async def short_reply(self, messages, max_chars=None):
        self.calls += 1
        return self.replies.pop(0) if self.replies else None


def make_orchestrator(llm=None):
    settings = get_settings()
    cache = KnowledgeCache()
    cache.load_from_seed()
    classifier = IntentClassifier(settings, http=None)
    return DialogOrchestrator(cache, classifier, llm, ComplianceEngine(), settings)


async def new_call(orch):
    state = CallState(call_id="T1", case=dict(DEMO_CASE))
    opening = await orch.opening(state)
    return state, opening


# ---------------- 完整主链路 ----------------
async def test_full_flow_reach_agreement():
    orch = make_orchestrator(FakeLLM())
    state, opening = await new_call(orch)
    assert "调解" in opening.reply and "本人" in opening.reply
    assert state.current_node == "N002"
    # 未确认本人前，开场白不得出现平台/金额
    assert "橘子分期" not in opening.reply and "16000" not in opening.reply

    r = await orch.handle_turn(state, "是我，什么事？")
    assert state.slots.get("identity_confirmed") is True
    assert "橘子分期" in r.reply           # 确认本人后才允许披露平台
    assert state.current_node == "N007"    # 告知后自动衔接通知确认问句

    r = await orch.handle_turn(state, "收到过短信")
    assert state.current_node == "N009" and "愿意" in r.reply

    r = await orch.handle_turn(state, "我现在确实没钱啊")
    assert r.intent == "NO_MONEY" and state.current_node == "N018"
    assert state.slots.get("willingness") is True
    assert "时间或金额" in r.reply          # FakeLLM 返回 None -> 回退参考话术

    r = await orch.handle_turn(state, "下个月10号发了工资能还1000")
    assert r.intent == "PROVIDE_PLAN"
    assert state.current_node == "N021"
    assert "1000" in r.reply and "下个月10号" in r.reply and "对吗" in r.reply

    r = await orch.handle_turn(state, "对的")
    assert r.end_call is True
    assert r.call_result == "达成方案"
    assert "记录" in r.reply and "感谢" in r.reply
    assert state.ended


# ---------------- 分期槽位收集闭环 ----------------
async def test_installment_slot_loop():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    r = await orch.handle_turn(state, "能不能分期慢慢还")
    assert state.current_node == "N019" and "分几期" in r.reply

    r = await orch.handle_turn(state, "分6期吧")
    assert state.slots.get("installment_count") == 6
    assert state.current_node == "N019" and "每期" in r.reply   # 追问缺失槽位

    r = await orch.handle_turn(state, "每期2000")
    assert state.slots.get("installment_amount") == 2000.0
    assert state.current_node == "N021"
    assert "6" in r.reply and "2000" in r.reply

    r = await orch.handle_turn(state, "可以")
    assert r.end_call and r.call_result == "达成方案"


# ---------------- 预约回访 ----------------
async def test_callback_booking():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    r = await orch.handle_turn(state, "我现在不方便，晚点再打")
    assert state.current_node == "N022" and r.route_id == "R020"

    r = await orch.handle_turn(state, "明天下午3点吧")
    assert r.end_call and r.call_result == "预约回访"
    assert "明天下午3点" in r.reply


# ---------------- 非本人 ----------------
async def test_not_self_relay():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r = await orch.handle_turn(state, "你打错了，不是我")
    assert r.intent == "NOT_SELF" and state.current_node == "N005"
    assert "张三" in r.reply and "不便" in r.reply
    # 第三方在场时不得披露平台/金额
    assert "橘子分期" not in r.reply and "16000" not in r.reply

    r = await orch.handle_turn(state, "认识，是我家人")
    assert r.end_call and r.call_result == "非本人"
    assert "转告" in r.reply


# ---------------- 怀疑诈骗 -> 核验 -> 继续 ----------------
async def test_fraud_then_confirm():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r = await orch.handle_turn(state, "你不会是骗子吧")
    assert r.route_id == "R002" and "核验" in r.reply
    assert state.current_node == "N004"

    r = await orch.handle_turn(state, "行吧，我就是张三")
    assert state.slots.get("identity_confirmed") is True
    assert "橘子分期" in r.reply


# ---------------- 转人工 ----------------
async def test_transfer_human():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r = await orch.handle_turn(state, "我要转人工")
    assert r.transfer_human is True and r.route_id == "R001"
    assert "人工" in r.reply
    assert state.call_result == "转人工" and state.ended


# ---------------- 合规拦截：LLM输出威胁话术被替换 ----------------
async def test_compliance_repair_on_llm_output():
    bad = FakeLLM(replies=["你必须今天马上还清，不然就起诉你，列入失信名单。"])
    orch = make_orchestrator(bad)
    state, _ = await new_call(orch)
    await orch.handle_turn(state, "我是")
    await orch.handle_turn(state, "收到了")
    r = await orch.handle_turn(state, "我没钱还")     # 走 LLM 路由 R131
    assert r.llm_used is True
    assert r.compliance["passed"] is False
    assert any(v["rule_id"] == "CR003" for v in r.compliance["violations"])
    assert "自愿" in r.reply                          # 已替换为CR003修复话术
    assert "起诉" not in r.reply and "失信" not in r.reply
    assert "CR003" in state.risk_flags


# ---------------- 合规：未确认本人不得披露隐私 ----------------
def test_privacy_pre_identity_dynamic_rule():
    cache = KnowledgeCache()
    snap = cache.load_from_seed()
    engine = ComplianceEngine()
    res = engine.check(snap, "您在橘子分期有一笔16000元的欠款逾期了", {}, dict(DEMO_CASE))
    assert res.repaired and res.violations[0]["rule_id"] == "CR001"
    assert "本人" in res.text

    ok = engine.check(snap, "您好，我这边是XX民商事调解中心工作人员。", {}, dict(DEMO_CASE))
    assert ok.passed and not ok.repaired


# ---------------- fallback 重试与兜底跳转 ----------------
async def test_fallback_retry_then_jump():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r1 = await orch.handle_turn(state, "呜啦呜啦巴拉巴拉")
    assert r1.action_type == "FALLBACK" and "没有听清" in r1.reply
    assert state.current_node == "N002"
    r2 = await orch.handle_turn(state, "叽里咕噜")
    assert state.current_node == "N002"
    r3 = await orch.handle_turn(state, "哇啦哇啦")          # 超过 max_retry=2 -> 兜底 N003
    assert state.current_node == "N002" or "调解" in r3.reply
    assert "工作人员" in r3.reply                            # N003 身份解释话术


# ---------------- 打点与状态字段 ----------------
async def test_latency_and_segments():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r = await orch.handle_turn(state, "我是")
    for key in ("classify", "route", "reply", "compliance", "total"):
        assert key in r.latency_ms
    assert r.latency_ms["total"] < 500          # 离线全模板路径应为毫秒级
    assert isinstance(r.segments, list) and r.segments
    assert r.node_before == "N002" and r.node_after == "N007"


# ---------------- 状态存取（内存模式） ----------------
async def test_state_store_roundtrip():
    store = StateStore(redis=None)
    st = CallState(call_id="X", case=dict(DEMO_CASE))
    st.slots["identity_confirmed"] = True
    await store.save(st)
    back = await store.get("X")
    assert back is not None and back.slots["identity_confirmed"] is True
    await store.delete("X")
    assert await store.get("X") is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
