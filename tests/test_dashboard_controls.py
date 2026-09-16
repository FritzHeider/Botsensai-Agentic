"""Unit tests for Backtesting, Paper Trading, and Sniper Info dashboard control endpoints."""

import pytest
from fastapi.testclient import TestClient

from botsensai.api import create_headless_api_app
from botsensai.config import Settings
from botsensai.store.db import Database


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_ctrl.db"
    settings = Settings(db_path=str(db_path))
    db = Database(db_path)
    # Seed a paper position
    db.upsert_paper_position(
        token_key="solana:So11111111111111111111111111111111111111112",
        symbol="TEST_COIN",
        mint="So11111111111111111111111111111111111111112",
        amount_token=1000000.0,
        cost_basis_native=0.05,
        entry_price_native=0.00000005,
        peak_price_native=0.00000006,
        last_price_native=0.000000055,
        opened_at=1700000000.0,
        status="OPEN",
    )
    # Seed an execution signal
    db.record_signal(
        token_key="solana:So11111111111111111111111111111111111111112",
        symbol="TEST_COIN",
        side="BUY",
        size_native=0.05,
        max_slippage_bps=300,
        jito_tip_lamports=10000,
        score=0.85,
        as_of=1700000000.0,
    )
    db.close()

    app = create_headless_api_app(settings)
    return TestClient(app)


def test_papertrade_endpoints(client):
    # 1. Get portfolio
    res = client.get("/api/v1/papertrade/portfolio")
    assert res.status_code == 200
    data = res.json()
    assert data["open_positions_count"] == 1
    assert len(data["open_positions"]) == 1
    assert data["open_positions"][0]["symbol"] == "TEST_COIN"
    assert data["open_positions"][0]["multiple"] > 1.0

    # 2. Close position
    res_close = client.post(
        "/api/v1/papertrade/close",
        json={"token_key": "solana:So11111111111111111111111111111111111111112", "reason": "test_close"},
    )
    assert res_close.status_code == 200
    close_data = res_close.json()
    assert close_data["status"] == "success"
    assert close_data["closed_position"]["status"] == "CLOSED"

    # 3. Verify closed in portfolio
    res_after = client.get("/api/v1/papertrade/portfolio")
    assert res_after.json()["open_positions_count"] == 0
    assert len(res_after.json()["recent_closed_trades"]) == 1

    # 4. Reset
    res_reset = client.post("/api/v1/papertrade/reset")
    assert res_reset.status_code == 200
    assert client.get("/api/v1/papertrade/portfolio").json()["closed_trades_count"] == 0


def test_sniper_endpoints(client):
    # 1. Sniper status
    res_status = client.get("/api/v1/sniper/status")
    assert res_status.status_code == 200
    status = res_status.json()
    assert status["safety_invariants"]["zero_signing_verified"] is True
    assert status["dry_run"] is True
    assert "execution" in status
    assert status["outbox"]["total_recorded_signals"] >= 1

    # 2. Sniper signals
    res_sig = client.get("/api/v1/sniper/signals")
    assert res_sig.status_code == 200
    sigs = res_sig.json()
    assert len(sigs) >= 1
    assert sigs[0]["symbol"] == "TEST_COIN"

    # 3. Test snipe simulation
    res_sim = client.post(
        "/api/v1/sniper/test_snipe",
        json={"mint": "So11111111111111111111111111111111111111112", "size_sol": 0.05},
    )
    assert res_sim.status_code == 200
    sim_data = res_sim.json()
    assert sim_data["status"] == "SIMULATED"
    assert "simulated_fill" in sim_data
    assert sim_data["simulated_fill"]["amount_sol"] == 0.05


def test_backtest_endpoints(client):
    # 1. Check initial latest backtest (idle)
    res_init = client.get("/api/v1/backtest/latest")
    assert res_init.status_code == 200
    assert res_init.json()["status"] == "idle"

    # 2. Run backtest with synthetic fallback
    res_run = client.post(
        "/api/v1/backtest/run",
        json={
            "lookback_days": 1.0,
            "capital_sol": 10.0,
            "min_score": 0.65,
            "synthetic": True,
            "universe": 15,
            "baselines": False,
        },
    )
    assert res_run.status_code == 200
    bt = res_run.json()
    assert bt["synthetic"] is True
    assert "summary" in bt
    assert "trades" in bt
    assert bt["duration_seconds"] >= 0

    # 3. Check latest backtest now returns cached result
    res_latest = client.get("/api/v1/backtest/latest")
    assert res_latest.status_code == 200
    assert res_latest.json()["token_count"] == bt["token_count"]
