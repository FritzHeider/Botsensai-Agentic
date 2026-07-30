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


def _minimal_snapshot() -> dict:
    return {
        "generated_at": utcnow(),
        "mode": "static",
        "trading_mode": "paper",
        "candidates": [
            {"token_key": "solana:" + "1" * 44, "symbol": "CHEEMS", "composite": 0.59,
             "coverage": 0.39, "regime": "hot", "vetoes": ["mint_authority_live"],
             "refused": True, "as_of": utcnow(), "contaminated": True,
             "explanation": "REJECTED (mint authority live)"},
        ],
        "families": {"social_authenticity": {"total": 9, "measured": 5, "metric_ids": []}},
        "integrity": [
            {"id": "posts_reachable", "level": "alarm",
             "headline": "No collected post is readable by any metric",
             "detail": "0 of 494 posts carry a token_key."},
        ],
        "posts": {},
        "counts": {"social_posts": 494, "outcomes": 0},
        "runs": [],
        "weights_version": "v0",
        "entry_threshold": 0.68,
        "min_coverage": 0.5,
        "labelled_outcomes": 0,
        "contaminated_before": utcnow(),
    }


def test_render_produces_a_self_contained_page():
    from botsensai.dashboard.render import render_html

    html = render_html(_minimal_snapshot())

    assert "<html" in html
    assert len(html) > 5000
    assert "http://" not in html
    assert 'src="https://' not in html
    assert 'href="https://' not in html
    assert "UNVALIDATED" in html
    assert "0 labelled outcomes" in html


def test_render_shows_integrity_alarms_and_contamination():
    from botsensai.dashboard.render import render_html

    html = render_html(_minimal_snapshot())

    assert "No collected post is readable by any metric" in html
    assert "PRE-FIX" in html
    assert "mint_authority_live" in html


def test_render_shows_an_unmeasured_family_as_absent_not_zero():
    """A family with nothing measured must not read as a zero score.

    Asserting on the rendered family row rather than on any static template
    text: a test that passes because the word MISSING appears somewhere in the
    stylesheet proves nothing.
    """
    import re

    from botsensai.dashboard.render import render_html

    snap = _minimal_snapshot()
    snap["families"] = {"social_authenticity": {"total": 9, "measured": 0,
                                                "metric_ids": []}}
    html = render_html(snap)

    assert "0/9" in html, "must state how many of the family were measured"
    # The bar for an unmeasured family must be empty, not absent or full.
    assert re.search(r'<i style="width:0%"></i>', html)
    assert "MISSING, not zero" in html


def test_render_marks_every_pre_fix_candidate():
    """Contamination marking must be per-row, not a page-level note."""
    from botsensai.dashboard.render import render_html

    snap = _minimal_snapshot()
    snap["candidates"].append({
        "token_key": "solana:" + "2" * 44, "symbol": "CLEAN", "composite": 0.4,
        "coverage": 0.5, "regime": "hot", "vetoes": [], "refused": True,
        "as_of": utcnow(), "contaminated": False, "explanation": None,
    })
    html = render_html(snap)

    assert html.count("PRE-FIX") == 1, "only the contaminated row may be marked"


def test_render_escapes_store_supplied_strings():
    """Nothing read out of the store may reach the page as live markup.

    `select_autoescape(["html"])` matches on the filename suffix and
    `dashboard.html.j2` ends in `.j2`, so it silently disables escaping. Post
    authors, token symbols and collector error text are attacker-supplied, and
    an injected remote <img> would also defeat the no-external-assets rule.
    """
    from botsensai.dashboard.render import render_html

    snap = _minimal_snapshot()
    snap["integrity"] = [{
        "id": "posts_reachable", "level": 'x"><script>alert(1)</script>',
        "headline": "<script>alert(1)</script>",
        "detail": '<img src="https://evil.test/pixel">',
    }]
    html = render_html(snap)

    assert "<script>" not in html
    assert "<img" not in html
    # The hostile URL may survive as inert escaped text; what must not survive
    # is a live attribute the browser would fetch.
    assert 'src="https://evil.test' not in html
    assert 'href="https://evil.test' not in html
    assert "&lt;script&gt;" in html, "the text must still be shown, escaped"


def test_p5_01_acceptance_command(tmp_path):
    """The acceptance command from @fix_plan.md P5-01, run verbatim."""
    import subprocess
    import sys

    out = tmp_path / "dash.html"
    result = subprocess.run(
        [sys.executable, "-m", "botsensai.cli", "dashboard", "--out", str(out)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    html = out.read_text()
    assert "<html" in html
    assert len(html) > 5000
