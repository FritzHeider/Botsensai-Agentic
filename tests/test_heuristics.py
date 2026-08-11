"""Tests for heuristic formation from post-mortems."""

from datetime import timedelta

import pytest

from botsensai.memory.heuristics import form_heuristics_from_postmortems
from botsensai.memory.store import MemoryStore
from botsensai.models import Confidence, MemoryKind, MetricValue, TokenRef, utcnow
from botsensai.store.db import Database


@pytest.fixture
def memory_store(tmp_path):
    store_path = tmp_path / "memory.db"
    store = MemoryStore(store_path)
    yield store
    store.close()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "botsensai.db"
    database = Database(db_path)
    yield database
    database.close()


def test_form_heuristics_from_postmortems_empty(memory_store):
    heuristics = form_heuristics_from_postmortems(memory_store)
    assert heuristics == []


def test_form_heuristics_clusters_by_exit_reason_and_evidence(memory_store):
    now = utcnow()
    t1 = now - timedelta(hours=2)
    t2 = now - timedelta(hours=1)

    memory_store.remember(
        MemoryKind.POSTMORTEM,
        subject="solana:tokenA",
        title="loss -0.05 native on tokenA",
        body="Held 10m, exited via stop_loss.",
        tags=["postmortem", "loss", "stop_loss"],
        confidence=0.6,
        evidence=["insider_supply_share=0.8", "fill:123"],
        created_at=t1,
    )
    memory_store.remember(
        MemoryKind.POSTMORTEM,
        subject="solana:tokenB",
        title="loss -0.04 native on tokenB",
        body="Held 12m, exited via stop_loss.",
        tags=["postmortem", "loss", "stop_loss"],
        confidence=0.6,
        evidence=["insider_supply_share=0.85", "fill:124"],
        created_at=t2,
    )

    heuristics = form_heuristics_from_postmortems(memory_store, min_cluster_size=2)
    assert len(heuristics) > 0

    for h in heuristics:
        assert h.kind == MemoryKind.HEURISTIC
        assert len(h.evidence) > 0
        assert h.confidence >= 0.4
        assert "stop_loss" in h.tags


def test_form_heuristics_clusters_with_database_metric_values(memory_store, db):
    now = utcnow()
    token1 = TokenRef(mint="mint1", symbol="T1")
    token2 = TokenRef(mint="mint2", symbol="T2")
    token3 = TokenRef(mint="mint3", symbol="T3")

    t0 = now - timedelta(hours=3)

    # Insert metric values in DB
    db.insert_metric_values(
        [
            MetricValue(
                metric_id="insider_supply_share",
                token=token1,
                as_of=t0,
                normalized=0.8,
                confidence=Confidence.HIGH,
            ),
            MetricValue(
                metric_id="insider_supply_share",
                token=token2,
                as_of=t0,
                normalized=0.9,
                confidence=Confidence.HIGH,
            ),
            MetricValue(
                metric_id="insider_supply_share",
                token=token3,
                as_of=t0,
                normalized=0.85,
                confidence=Confidence.HIGH,
            ),
        ]
    )

    # Add post-mortems to memory store
    for token in (token1, token2, token3):
        memory_store.remember(
            MemoryKind.POSTMORTEM,
            subject=token.key,
            title=f"loss on {token.symbol}",
            body="Exited via stop_loss after price drop.",
            tags=["postmortem", "loss", "stop_loss"],
            confidence=0.6,
            evidence=[f"fill_{token.symbol}"],
            created_at=t0 + timedelta(minutes=10),
        )

    heuristics = form_heuristics_from_postmortems(memory_store, db=db, min_cluster_size=2)
    assert len(heuristics) > 0

    for h in heuristics:
        assert h.kind == MemoryKind.HEURISTIC
        assert len(h.evidence) > 0
        # Every evidence entry should cite postmortems or fills
        assert any("postmortem:" in ev or "fill_" in ev for ev in h.evidence)


def test_heuristic_confidence_scales_with_cluster_size(memory_store):
    now = utcnow()

    def add_pm(token_name: str, exit_reason: str):
        memory_store.remember(
            MemoryKind.POSTMORTEM,
            subject=f"solana:{token_name}",
            title=f"loss on {token_name}",
            body=f"Exited via {exit_reason}.",
            tags=["postmortem", "loss", exit_reason],
            confidence=0.6,
            evidence=[f"trade_{token_name}"],
            created_at=now,
        )

    # Cluster A: 2 postmortems
    add_pm("A1", "take_profit")
    add_pm("A2", "take_profit")

    # Cluster B: 4 postmortems
    add_pm("B1", "dev_sold")
    add_pm("B2", "dev_sold")
    add_pm("B3", "dev_sold")
    add_pm("B4", "dev_sold")

    heuristics = form_heuristics_from_postmortems(memory_store, min_cluster_size=2)
    h_by_reason = {h.tags[1]: h for h in heuristics if len(h.tags) > 1}

    assert "take_profit" in h_by_reason
    assert "dev_sold" in h_by_reason

    # Cluster of size 4 should have strictly higher confidence than cluster of size 2
    assert h_by_reason["dev_sold"].confidence > h_by_reason["take_profit"].confidence
    assert len(h_by_reason["dev_sold"].evidence) >= 4
    assert len(h_by_reason["take_profit"].evidence) >= 2


def test_min_cluster_size_threshold(memory_store):
    now = utcnow()
    memory_store.remember(
        MemoryKind.POSTMORTEM,
        subject="solana:loneToken",
        title="loss on loneToken",
        body="Exited via max_hold_time.",
        tags=["postmortem", "loss", "max_hold_time"],
        confidence=0.6,
        evidence=["trade_lone"],
        created_at=now,
    )

    heuristics_min2 = form_heuristics_from_postmortems(memory_store, min_cluster_size=2)
    assert heuristics_min2 == []

    heuristics_min1 = form_heuristics_from_postmortems(memory_store, min_cluster_size=1)
    assert len(heuristics_min1) == 1
    assert len(heuristics_min1[0].evidence) > 0
