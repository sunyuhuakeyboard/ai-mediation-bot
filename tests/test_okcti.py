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
