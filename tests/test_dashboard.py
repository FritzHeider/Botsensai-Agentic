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


def test_recent_runs_reports_degraded_surfaces(db: Database):
    now = utcnow()
    db.record_run(run_id="r1", surface="x", started_at=now, finished_at=now,
                  ok=False, records=0, error="enrich timed out")
    db.record_run(run_id="r1", surface="dexscreener", started_at=now, finished_at=now,
                  ok=True, records=42, error=None)

    runs = db.recent_runs()
    by_surface = {r["surface"]: r for r in runs}

    assert by_surface["x"]["ok"] is False
    assert by_surface["x"]["error"] == "enrich timed out"
    assert by_surface["x"]["records"] == 0
    assert by_surface["dexscreener"]["ok"] is True
    assert by_surface["dexscreener"]["records"] == 42


@pytest.mark.asyncio
async def test_pipeline_records_a_run_per_surface(tmp_path):
    """Regression: collector_runs was dead code, so a timed-out surface was invisible."""
    from botsensai.collectors.base import CollectionResult
    from botsensai.config import Settings
    from botsensai.pipeline import Pipeline

    settings = Settings()
    store = Database(str(tmp_path / "runs.db"))
    pipeline = Pipeline(settings, db=store)

    async def fake_sweep_enrich(tokens):
        now = utcnow()
        return [
            CollectionResult(surface="x", started_at=now, finished_at=now,
                             ok=False, degraded=True, error="enrich timed out"),
            CollectionResult(surface="pumpfun", started_at=now, finished_at=now, ok=True),
        ]

    pipeline.collectors.sweep_enrich = fake_sweep_enrich  # type: ignore[assignment]
    try:
        await pipeline.enrich([])
        surfaces = {r["surface"]: r for r in store.recent_runs()}
        assert surfaces["x"]["ok"] is False
        assert surfaces["x"]["error"] == "enrich timed out"
        assert surfaces["pumpfun"]["ok"] is True
    finally:
        store.close()


def test_integrity_flags_unreachable_posts():
    from botsensai.dashboard.integrity import check_integrity

    posts = {"x": {"total": 494, "reachable": 0, "distinct_authors": 40,
                   "unresolved_authors": 0, "with_views": 494,
                   "with_bookmarks": 494, "with_author_age": 494}}
    flags = {f.id: f for f in check_integrity(posts, runs=[], spread={})}

    assert flags["posts_reachable"].level == "alarm"
    assert "0 of 494" in flags["posts_reachable"].detail


def test_integrity_flags_collapsed_authors():
    from botsensai.dashboard.integrity import check_integrity

    posts = {"x": {"total": 390, "reachable": 390, "distinct_authors": 1,
                   "unresolved_authors": 390, "with_views": 390,
                   "with_bookmarks": 390, "with_author_age": 0}}
    flags = {f.id: f for f in check_integrity(posts, runs=[], spread={})}

    assert flags["author_resolution"].level == "alarm"
    assert flags["author_ages"].level == "alarm"


def test_integrity_flags_a_timed_out_surface():
    from botsensai.dashboard.integrity import check_integrity

    runs = [
        {"surface": "x", "ok": False, "records": 0, "error": "enrich timed out"},
        {"surface": "pumpfun", "ok": True, "records": 40, "error": None},
    ]
    flags = {f.id: f for f in check_integrity({}, runs=runs, spread={})}

    assert flags["surface_health"].level == "alarm"
    assert "x" in flags["surface_health"].detail


def test_integrity_flags_a_constant_metric():
    from botsensai.dashboard.integrity import check_integrity

    spread = {
        "flat_metric": {"count": 12, "distinct": 1, "min": 0.36, "max": 0.36},
        "real_metric": {"count": 12, "distinct": 9, "min": 0.05, "max": 0.9},
    }
    flags = {f.id: f for f in check_integrity({}, runs=[], spread=spread)}

    assert flags["metric_variance"].level == "alarm"
    assert "flat_metric" in flags["metric_variance"].detail
    assert "real_metric" not in flags["metric_variance"].detail


def test_integrity_is_quiet_when_everything_is_healthy():
    from botsensai.dashboard.integrity import check_integrity

    posts = {"x": {"total": 260, "reachable": 260, "distinct_authors": 171,
                   "unresolved_authors": 0, "with_views": 260,
                   "with_bookmarks": 260, "with_author_age": 260}}
    runs = [{"surface": "x", "ok": True, "records": 260, "error": None}]
    spread = {"m": {"count": 12, "distinct": 8, "min": 0.1, "max": 0.9}}

    flags = check_integrity(posts, runs=runs, spread=spread)

    assert all(f.level == "ok" for f in flags), [f.headline for f in flags if f.level != "ok"]


def test_snapshot_marks_pre_fix_scores_as_contaminated(db: Database):
    from datetime import UTC, datetime

    from botsensai.config import Settings
    from botsensai.dashboard.data import CONTAMINATED_BEFORE, build_snapshot

    old = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    db.insert_score(Score(token=_token("OLD", "1"), as_of=old, composite=0.5, coverage=0.4))
    db.insert_score(Score(token=_token("NEW", "2"), as_of=utcnow(), composite=0.5, coverage=0.4))

    snap = build_snapshot(Settings(), db)
    by_key = {c["token_key"]: c for c in snap["candidates"]}

    assert by_key["solana:" + "1" * 44]["contaminated"] is True
    assert by_key["solana:" + "2" * 44]["contaminated"] is False
    assert snap["contaminated_before"] == CONTAMINATED_BEFORE
    assert snap["families"], "metric families must be present"
    assert "integrity" in snap
