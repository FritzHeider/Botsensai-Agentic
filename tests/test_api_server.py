"""Unit tests for headless REST & WebSocket API Server."""

from fastapi.testclient import TestClient

from botsensai.api import create_headless_api_app
from botsensai.config import Settings


def test_api_server_endpoints(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_headless_api_app(settings)
    client = TestClient(app)

    # 1. Metrics catalogue
    resp = client.get("/api/v1/metrics")
    assert resp.status_code == 200
    metrics = resp.json()
    assert len(metrics) >= 32

    # 2. Market regime
    resp_regime = client.get("/api/v1/regime")
    assert resp_regime.status_code == 200
    data_regime = resp_regime.json()
    assert "trading_mode" in data_regime

    # 3. Candidates
    resp_cand = client.get("/api/v1/candidates")
    assert resp_cand.status_code == 200
    assert isinstance(resp_cand.json(), list)

    # 4. System metrics
    resp_sys = client.get("/api/v1/system/metrics")
    assert resp_sys.status_code == 200
    data_sys = resp_sys.json()
    assert data_sys["status"] == "healthy"
    assert "counts" in data_sys
    assert "capabilities" in data_sys
    assert "helius_rpc" in data_sys["capabilities"]

    # 5. Prometheus metrics exposition
    resp_prom = client.get("/api/v1/system/metrics/prometheus")
    assert resp_prom.status_code == 200
    assert resp_prom.headers["content-type"].startswith("text/plain")
    prom_text = resp_prom.text
    assert "botsensai_up 1" in prom_text
    assert "botsensai_db_records_total" in prom_text
    assert "botsensai_provider_configured" in prom_text

    # 6. Live Dashboard HTML root & redirect
    resp_root = client.get("/")
    assert resp_root.status_code == 200
    assert "text/html" in resp_root.headers["content-type"]
    assert "Botsensai" in resp_root.text
    assert client.head("/").status_code == 200

    resp_dash = client.get("/dashboard", follow_redirects=False)
    assert resp_dash.status_code in (302, 307)
    assert resp_dash.headers["location"] == "/"

    # 7. Operational Actions: Status, Pause, and Resume
    resp_act_status = client.get("/api/v1/system/actions/status")
    assert resp_act_status.status_code == 200
    assert resp_act_status.json()["engine_paused"] is False

    resp_pause = client.post("/api/v1/system/actions/pause")
    assert resp_pause.status_code == 200
    assert resp_pause.json()["action"] == "pause"

    resp_act_paused = client.get("/api/v1/system/actions/status")
    assert resp_act_paused.json()["engine_paused"] is True

    # Sweep should be refused while paused
    resp_sweep_paused = client.post("/api/v1/system/actions/sweep")
    assert resp_sweep_paused.status_code == 409

    resp_resume = client.post("/api/v1/system/actions/resume")
    assert resp_resume.status_code == 200
    assert resp_resume.json()["action"] == "resume"

    # 8. Operational Actions: Surface Doctor
    resp_doc = client.post("/api/v1/system/actions/doctor")
    assert resp_doc.status_code == 200
    doc_data = resp_doc.json()
    assert doc_data["action"] == "doctor"
    assert "total_surfaces" in doc_data
    assert "surfaces" in doc_data
    assert isinstance(doc_data["surfaces"], list)

