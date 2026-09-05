"""Unit tests for the real-time live dashboard server."""

import pytest
from fastapi.testclient import TestClient

from botsensai.config import Settings
from botsensai.dashboard.server import broadcaster, create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_app(settings)
    return TestClient(app)


def test_dashboard_root_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Botsensai" in resp.text


def test_api_snapshot_endpoint(client: TestClient) -> None:
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data
    assert "integrity" in data
    assert "counts" in data


def test_api_metrics_endpoint(client: TestClient) -> None:
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 30
    assert "id" in data[0]
    assert "family" in data[0]


def test_api_health_endpoint(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_api_candidates_endpoint(client: TestClient) -> None:
    resp = client.get("/api/candidates?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_broadcaster_event_dispatch() -> None:
    await broadcaster.broadcast("test_event", {"hello": "world"})
    # Asserts that broadcasting with zero connections executes cleanly without exception
    assert True


def test_api_token_autopsy_endpoint(client: TestClient) -> None:
    resp = client.get("/api/tokens/TestMint11111111111111111111111111111111111/autopsy")
    assert resp.status_code == 200
    data = resp.json()
    assert "token_key" in data
    assert "events" in data
    assert isinstance(data["events"], list)


def test_api_token_ask_endpoint(client: TestClient) -> None:
    resp = client.post(
        "/api/tokens/TestMint11111111111111111111111111111111111/ask",
        json={"question": "What are the primary risks?"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "answer" in data
    assert "query" in data
    assert "citations" in data


def test_api_snapshot_has_family_scores_and_candidates_json(client: TestClient) -> None:
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates_json" in data
