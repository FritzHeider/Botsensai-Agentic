"""Tests for the static dashboard build and its integrity checks."""

from __future__ import annotations

from datetime import timedelta

import pytest

from botsensai.models import (
    Chain,
    Platform,
    Score,
    SocialPost,
    TokenRef,
    VetoReason,
    utcnow,
)
from botsensai.store.db import Database


def _token(symbol: str, mint_char: str = "9") -> TokenRef:
    return TokenRef(chain=Chain.SOLANA, mint=mint_char * 44, symbol=symbol)


def _post(post_id: str, token_key: str | None, author: str, **kw) -> SocialPost:
    return SocialPost(
        platform=Platform.X,
        post_id=post_id,
        token_key=token_key,
        author=author,
        as_of=utcnow(),
        text="hello",
        source="x:graphql",
        **kw,
    )


@pytest.fixture()
def db(tmp_path) -> Database:
    store = Database(str(tmp_path / "dash.db"))
    yield store
    store.close()


def test_recent_scores_returns_newest_first(db: Database):
    now = utcnow()
    db.insert_score(
        Score(token=_token("OLD", "1"), as_of=now - timedelta(minutes=10),
              composite=0.20, coverage=0.30)
    )
    db.insert_score(
        Score(token=_token("NEW", "2"), as_of=now, composite=0.61, coverage=0.42,
              regime="hot", vetoes=[VetoReason.MINT_AUTHORITY_LIVE])
    )

    rows = db.recent_scores(limit=10)

    assert [r["token_key"] for r in rows][0].endswith("2" * 44)
    assert rows[0]["composite"] == pytest.approx(0.61)
    assert rows[0]["coverage"] == pytest.approx(0.42)
    assert rows[0]["regime"] == "hot"
    assert rows[0]["vetoes"] == ["mint_authority_live"]
    assert len(rows) == 2


def test_social_post_integrity_counts_unreachable_posts(db: Database):
    db.insert_posts([
        _post("1", "solana:aaa", "alice", views=10),
        _post("2", "solana:aaa", "bob", views=20),
        _post("3", None, "carol"),            # unreachable: no token_key
        _post("4", "solana:aaa", "unknown"),  # unresolved author
    ])

    stats = db.social_post_integrity()["x"]

    assert stats["total"] == 4
    assert stats["reachable"] == 3
    assert stats["distinct_authors"] == 4
    assert stats["unresolved_authors"] == 1
    assert stats["with_views"] == 2


def test_metric_raw_spread_flags_a_constant_metric(db: Database):
    from botsensai.models import Confidence, MetricValue

    now = utcnow()
    values = []
    for i in range(4):
        values.append(MetricValue(
            metric_id="always_same", token=_token(f"T{i}", str(i)), as_of=now,
            raw=0.3607, normalized=0.5, confidence=Confidence.HIGH,
        ))
        values.append(MetricValue(
            metric_id="varies", token=_token(f"T{i}", str(i)), as_of=now,
            raw=float(i), normalized=0.5, confidence=Confidence.HIGH,
        ))
    db.insert_metric_values(values)

    spread = db.metric_raw_spread()

    assert spread["always_same"]["distinct"] == 1
    assert spread["always_same"]["count"] == 4
    assert spread["varies"]["distinct"] == 4
