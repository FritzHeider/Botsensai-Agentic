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
