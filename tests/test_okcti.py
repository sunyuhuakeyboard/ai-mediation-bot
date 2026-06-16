"""OKCTI / LLM-IVR SSE 兼容接口测试。"""
import os

os.environ.setdefault("OFFLINE_MODE", "1")
os.environ.setdefault("LLM_API_KEY", "")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402


def _client() -> TestClient:
    get_settings.cache_clear()
    return TestClient(create_app())


def _payload(callid: str, typ: str, text: str = "") -> dict:
    return {
        "callid": callid,
        "caller": "95000000",
        "callee": "13900000000",
        "direct": 1,
        "type": typ,
        "usrtype": 2 if typ == "QA" else 0,
        "usrcontent": text,
        "usrrecurl": "",
        "fsx": 1,
        "ch": 1,
        "sysid": 1,
        "taskid": "TASK_DEMO",
        "calltaskid": "CASE20260610001",
        "oricaller": "",
        "video": False,
    }


def test_okcti_start_returns_sse_ivr():
    with _client() as client:
        resp = client.post("/ivr/okcti/welcome", json=_payload("OKCTI_T1", "START"))
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event:wait" not in resp.text
        assert "event:ivr" in resp.text
        assert "[E-N=D]" in resp.text
        assert '"callid":"OKCTI_T1"' in resp.text
        assert "请问您是张三本人吗" in resp.text


def test_okcti_stream_alias_returns_sse_ivr():
    with _client() as client:
        resp = client.post("/ivr/okcti/welcome/stream",
                           json=_payload("OKCTI_T1_STREAM", "START"))
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event:ivr" in resp.text
        assert "[E-N=D]" in resp.text
        assert '"callid":"OKCTI_T1_STREAM"' in resp.text


def test_okcti_qa_uses_existing_dialog_state():
    with _client() as client:
        client.post("/ivr/okcti/welcome", json=_payload("OKCTI_T2", "START"))
        resp = client.post("/ivr/okcti/welcome",
                           json=_payload("OKCTI_T2", "QA", "是我，什么事"))
        assert resp.status_code == 200
        assert "event:ivr" in resp.text
        assert '"currentnodeid":"N007"' in resp.text
        assert "橘子分期" in resp.text


def test_okcti_duplicate_qa_does_not_advance_state_twice():
    with _client() as client:
        client.post("/ivr/okcti/welcome", json=_payload("OKCTI_DUP", "START"))
        payload = _payload("OKCTI_DUP", "QA", "是我，什么事")
        payload["logid"] = "LOG_DUP_001"

        first = client.post("/ivr/okcti/welcome/stream", json=payload)
        second = client.post("/ivr/okcti/welcome/stream", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.text == first.text

        state = client.get("/api/v1/calls/OKCTI_DUP/state").json()
        assert state["current_node"] == "N007"
        assert state["turn_index"] == 1

        transcript = client.get("/api/v1/calls/OKCTI_DUP/transcript").json()
        assert [t["user_text"] for t in transcript["turns"]] == [None, "是我，什么事"]


def test_okcti_synthetic_asr_placeholder_stays_at_node():
    """复现生产事故：usrtype=9 + usrcontent='ASR content always empty'。
    占位符不得被当作真实话语，状态必须停留在 N002，不能跳到 N005。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_SYN", "START"))

        synthetic = _payload("OKCTI_SYN", "QA", "ASR content always empty")
        synthetic["usrtype"] = 9
        resp = client.post("/ivr/okcti/welcome/stream", json=synthetic)
        assert resp.status_code == 200
        assert "event:ivr" in resp.text

        state = client.get("/api/v1/calls/OKCTI_SYN/state").json()
        assert state["current_node"] == "N002"          # 仍在身份确认节点
        assert "not_self" not in state.get("slots", {})  # 占位符不得污染槽位
        # 后续用户清晰回应"是我"必须能正常推进
        resp2 = client.post("/ivr/okcti/welcome/stream",
                            json=_payload("OKCTI_SYN", "QA", "是我，什么事"))
        assert resp2.status_code == 200
        state2 = client.get("/api/v1/calls/OKCTI_SYN/state").json()
        assert state2["current_node"] == "N007"


def test_okcti_n005_recovery_when_user_clarifies_identity():
    """N005 误判恢复：进入"非本人"节点后用户说"我就是本人"，应回到事项告知而非结束。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_RCV", "START"))
        # 用户先回答"不是我"被路由到 N005
        client.post("/ivr/okcti/welcome/stream",
                    json=_payload("OKCTI_RCV", "QA", "你打错了，不是我"))
        s1 = client.get("/api/v1/calls/OKCTI_RCV/state").json()
        assert s1["current_node"] == "N005"

        # 用户更正：我就是本人
        resp = client.post("/ivr/okcti/welcome/stream",
                           json=_payload("OKCTI_RCV", "QA", "我就是本人"))
        assert resp.status_code == 200
        s2 = client.get("/api/v1/calls/OKCTI_RCV/state").json()
        # 应路由到 N006 并清掉 not_self
        assert s2["current_node"] in ("N006", "N007")
        assert s2["slots"].get("identity_confirmed") is True
        assert s2["slots"].get("not_self") is False
        assert s2["ended"] is False


def test_okcti_short_reply_emits_no_msg_events():
    """50 字以内的回复必须整体放入 cmdcontent，不产生 msg 事件。
    避免 OKCTI 把 cmdcontent + msg 当两段 TTS 接连播放，造成"每句话说两遍"。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_SR", "START"))
        resp = client.post("/ivr/okcti/welcome/stream",
                           json=_payload("OKCTI_SR", "QA", "我是本人"))
        body = resp.text
        # 整段 chained reply (案件告知+通知确认 50 字) 应整体放 cmdcontent
        assert "event:msg" not in body
        assert "请问您之前有收到过" in body  # 通知确认问句仍要送达，只是在 cmdcontent 里
        assert "民事调解事项" in body


def test_okcti_consecutive_silence_ends_call_gracefully():
    """连续静音超过阈值即温和挂断，不再无限循环回探（避免被 IVR 自播放大成"每句两遍"）。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_QUIET", "START"))
        silent = _payload("OKCTI_QUIET", "QA", "")
        silent["usrtype"] = 9
        # 前两轮静音仍是温和回探，未结束
        for _ in range(2):
            resp = client.post("/ivr/okcti/welcome/stream", json=silent)
            assert resp.status_code == 200
        s_mid = client.get("/api/v1/calls/OKCTI_QUIET/state").json()
        assert s_mid["ended"] is False
        # 第三轮静音超阈值：bot 优雅道别并挂断
        resp = client.post("/ivr/okcti/welcome/stream", json=silent)
        assert resp.status_code == 200
        assert "不打扰" in resp.text or "再联系" in resp.text
        assert "再见" in resp.text
        s_end = client.get("/api/v1/calls/OKCTI_QUIET/state").json()
        assert s_end["ended"] is True
        assert s_end["call_result"] == "用户未回应"


def test_okcti_real_input_resets_silence_count():
    """真实输入到达后静音计数归零，避免误伤偶尔静音后又开口的用户。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_RESET", "START"))
        silent = _payload("OKCTI_RESET", "QA", "")
        silent["usrtype"] = 9
        # 两次静音
        client.post("/ivr/okcti/welcome/stream", json=silent)
        client.post("/ivr/okcti/welcome/stream", json=silent)
        # 真实回答
        client.post("/ivr/okcti/welcome/stream",
                    json=_payload("OKCTI_RESET", "QA", "我是本人"))
        s = client.get("/api/v1/calls/OKCTI_RESET/state").json()
        # 真实输入推进了节点，且 silence_count 已复位
        assert s["current_node"] != "N002"
        assert s["ended"] is False


def test_okcti_silence_prompt_does_not_re_ask_node_question():
    """用户静音时仅发送"您还在吗"类短句，不再追加节点主问句，避免听感重复。"""
    with _client() as client:
        client.post("/ivr/okcti/welcome/stream", json=_payload("OKCTI_SIL", "START"))
        client.post("/ivr/okcti/welcome/stream",
                    json=_payload("OKCTI_SIL", "QA", "我是本人"))
        client.post("/ivr/okcti/welcome/stream",
                    json=_payload("OKCTI_SIL", "QA", "收到过短信"))
        # 触发静音兜底（usrtype=9 + 无 usrcontent）
        silent = _payload("OKCTI_SIL", "QA", "")
        silent["usrtype"] = 9
        resp = client.post("/ivr/okcti/welcome/stream", json=silent)
        body = resp.text
        # 不复读"调解是自愿..."类节点主问句
        assert "调解是自愿" not in body
        # 但仍应给出短的回探
        assert any(kw in body for kw in ("您慢慢说", "您还在", "听到您说话"))
        # 状态不变
        state = client.get("/api/v1/calls/OKCTI_SIL/state").json()
        assert state["current_node"] == "N009"
        assert state["ended"] is False


def test_okcti_default_org_and_debtor_name_in_opening():
    """OKCTI 不传 case 字段且无种子案件时，开场使用生产兜底值：亦法云调解中心 / 张小贤。"""
    with _client() as client:
        payload = _payload("OKCTI_DEF", "START")
        payload["calltaskid"] = "CASE_NOT_SEEDED_OKCTI_DEF"  # 走 _case_from_request 兜底分支
        resp = client.post("/ivr/okcti/welcome/stream", json=payload)
        body = resp.text
        assert "亦法云调解中心" in body
        assert "请问您是张小贤本人吗" in body
        # 不应再有占位符
        assert "XX民商事调解中心" not in body
        assert "请问您是客户本人吗" not in body


def test_okcti_not_self_variants_route_to_n005():
    """常见非本人口语表达都应触发 NOT_SELF 应答，路由到 N005。"""
    phrases = [
        "你打错了，不是我",
        "我不是当事人",
        "电话打错了",
        "你找错人了",
        "他不在家",
        "号码错了",
    ]
    for i, phrase in enumerate(phrases):
        with _client() as client:
            cid = f"OKCTI_NS_{i}"
            client.post("/ivr/okcti/welcome/stream", json=_payload(cid, "START"))
            client.post("/ivr/okcti/welcome/stream",
                        json=_payload(cid, "QA", phrase))
            state = client.get(f"/api/v1/calls/{cid}/state").json()
            assert state["current_node"] == "N005", \
                f"{phrase!r} should route to N005, got {state['current_node']}"
            assert state["slots"].get("not_self") is True


def test_okcti_short_deny_at_n002_routes_to_n005():
    """N002 上短否定（"我不是"/"不是了"）经极性映射应判为 NOT_SELF。"""
    for phrase in ("我不是", "不是了", "不是吧"):
        with _client() as client:
            cid = f"OKCTI_DENY_{phrase}"
            client.post("/ivr/okcti/welcome/stream", json=_payload(cid, "START"))
            client.post("/ivr/okcti/welcome/stream",
                        json=_payload(cid, "QA", phrase))
            state = client.get(f"/api/v1/calls/{cid}/state").json()
            assert state["current_node"] == "N005", \
                f"{phrase!r} should route to N005, got {state['current_node']}"


def test_okcti_end_returns_minimal_ivr_with_grade():
    with _client() as client:
        client.post("/ivr/okcti/welcome", json=_payload("OKCTI_T3", "START"))
        payload = _payload("OKCTI_T3", "END")
        payload["talktimelong"] = 60
        payload["callresult"] = 1
        resp = client.post("/api/v1/ivr/okcti/welcome", json=payload)
        assert resp.status_code == 200
        assert "event:ivr" in resp.text
        assert '"nodelabel":"END"' in resp.text
        assert '"grade":"' in resp.text
