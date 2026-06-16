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
    assert r1.action_type == "FALLBACK"
    # 邀请用户直说，但不应再带"没有听清/抱歉/再确认"等暗示未识别的措辞
    assert any(kw in r1.reply for kw in ("您直说", "您说说", "告诉我", "您看怎么处理"))
    assert "抱歉" not in r1.reply and "没有听清" not in r1.reply and "再确认" not in r1.reply
    assert state.current_node == "N002"
    r2 = await orch.handle_turn(state, "叽里咕噜")
    assert state.current_node == "N002"
    r3 = await orch.handle_turn(state, "哇啦哇啦")          # 超过 max_retry=2 -> 兜底 N003
    assert state.current_node == "N002" or "调解" in r3.reply
    assert "工作人员" in r3.reply                            # N003 身份解释话术


# ---------------- 复读抑制：归一化辅助 ----------------
def test_is_dup_normalized_helper():
    """跨标点/空白的同义句应被识别为复读，不同句子不应误杀。"""
    from app.engines.orchestrator import _is_dup
    assert _is_dup("您好，调解中心。", ["您好 调解中心"])
    assert _is_dup("感谢配合！", ["感谢配合。"])
    assert _is_dup("请问您是张三本人吗？", ["请问您是张三本人吗"])
    assert not _is_dup("您好", ["请问几点"])
    assert not _is_dup("", ["任意"])
    assert not _is_dup("您好", [])


# ---------------- 复读抑制：fallback 不重复节点主问句 ----------------
async def test_fallback_skips_entry_when_user_just_heard_it():
    """上一轮 bot 已问过节点主问句，本轮 fallback 仅给转折提示。"""
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    # 开场已经播报 N002 主问句"请问您是张三本人吗？"
    r1 = await orch.handle_turn(state, "呜啦呜啦巴拉巴拉")
    assert r1.action_type == "FALLBACK"
    # 给出邀请用户直说的转折提示，且不暗示"未识别"
    assert any(kw in r1.reply for kw in ("您直说", "您说说", "告诉我", "您看怎么处理"))
    # 主问句不应在 fallback 中再次播报
    assert "请问您是" not in r1.reply


# ---------------- 复读抑制：fallback 连续两轮 retry 必须换变体 ----------------
async def test_consecutive_fallbacks_use_different_retry_variants():
    """两轮 fallback 之间，TPL_RETRY_001 必须选用与上一轮 bot 不同的变体。"""
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    r1 = await orch.handle_turn(state, "呜啦呜啦")
    r2 = await orch.handle_turn(state, "巴拉巴拉")
    from app.engines.orchestrator import _norm
    assert _norm(r1.reply) != _norm(r2.reply)


# ---------------- 复读抑制：一句话内相邻重复子句压缩 ----------------
def test_collapse_self_repeat_in_single_string():
    from app.engines.orchestrator import DialogOrchestrator
    collapse = DialogOrchestrator._collapse_self_repeat
    assert collapse("我这边再确认一下。我这边再确认一下。") == "我这边再确认一下。"
    assert collapse("好的。好的。请稍等。") == "好的。请稍等。"
    assert collapse("请问几点？请问几点？") == "请问几点？"
    assert collapse("您好。再见。") == "您好。再见。"


# ---------------- LLM 短回复：思考模式关闭 ----------------
async def test_llm_payload_disables_thinking_when_configured():
    from app.engines.llm_client import LLMClient

    class CaptureClient:
        def __init__(self):
            self.captured = {}

        def stream(self, method, url, json):
            self.captured = json

            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("short-circuit")

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    settings = get_settings()
    llm = LLMClient(settings)
    capture = CaptureClient()
    llm.client = capture
    await llm.short_reply([{"role": "user", "content": "hi"}])
    assert capture.captured.get("thinking") == {"type": "disabled"}


# ---------------- 复读抑制：_avoid_repeat 压缩重复前缀 ----------------
async def test_avoid_repeat_strips_overlap_prefix():
    orch = make_orchestrator(FakeLLM())
    state, _ = await new_call(orch)
    reply, segs = orch._avoid_repeat(
        "您好调解中心。请问您方便吗？",
        ["您好调解中心。请问您方便吗？"],
        _state_with_last_bot("您好，调解中心！"),
    )
    # 上一轮 bot 已说过"您好，调解中心"，本轮应只保留新增问句
    assert "请问您方便" in reply
    assert reply.startswith("请问") or "您好调解中心" not in reply


def _state_with_last_bot(last: str) -> CallState:
    state = CallState(call_id="DUP_T", case=dict(DEMO_CASE))
    state.remember("bot", last)
    return state


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
