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
