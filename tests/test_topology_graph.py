"""Unit tests for on-chain topology and funder graph explorer."""

from datetime import UTC, datetime

from botsensai.dashboard.graph import build_topology_graph, render_topology_html
from botsensai.models import HolderRecord, Side, TokenRef, Trade
from botsensai.store.db import Database


def test_build_and_render_topology_graph(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token_key = "solana:testmint12345"
    token = TokenRef(mint="testmint12345", name="Test", symbol="TEST")

    # Insert dummy holders
    db.insert_holders(
        [
            HolderRecord(
                token=token,
                wallet="dev_wallet_123",
                balance=1000.0,
                share_of_supply=0.15,
                labels=["deployer"],
                as_of=now,
                observed_at=now,
            ),
            HolderRecord(
                token=token,
                wallet="insider_wallet_456",
                balance=500.0,
                share_of_supply=0.08,
                labels=["insider"],
                funded_by="funder_wallet_999",
                as_of=now,
                observed_at=now,
            ),
            HolderRecord(
                token=token,
                wallet="trader_wallet_789",
                balance=100.0,
                share_of_supply=0.01,
                labels=[],
                as_of=now,
                observed_at=now,
            ),
        ]
    )

    # Insert dummy trades
    db.insert_trades(
        [
            Trade(
                token=token,
                signature="sig1",
                wallet="trader_wallet_789",
                side=Side.BUY,
                amount_token=100.0,
                amount_native=1.5,
                as_of=now,
                observed_at=now,
            )
        ]
    )

    graph = build_topology_graph(token_key, db, as_of=now)
    assert len(graph.nodes) >= 4  # Pool + 3 holders + 1 funder
    assert len(graph.links) >= 3
    assert graph.summary["insiders"] == 1
    assert graph.summary["deployers"] == 1
    assert graph.summary["funders"] == 1

    html = render_topology_html(graph)
    assert "<!doctype html>" in html
    assert "Topology Graph" in html
    assert "d3.forceSimulation" in html
    assert token_key in html

    db.close()
