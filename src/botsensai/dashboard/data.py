"""Build the single read-only snapshot both dashboard modes render from."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botsensai.config import Settings
from botsensai.dashboard.integrity import check_integrity
from botsensai.metrics import build_registry
from botsensai.models import utcnow
from botsensai.store.db import Database

#: Scores written before this instant were produced by a collection path with
#: known-wrong author parsing, which penalised every token for fabricated author
#: concentration. They are displayed, but never as a baseline.
CONTAMINATED_BEFORE = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)


def build_snapshot(settings: Settings, db: Database) -> dict[str, Any]:
    """Everything the dashboard displays, as plain dicts.

    Plain dicts rather than models on purpose: the template must not be able to
    trigger a database read while rendering, and a later served mode has to be
    able to serialise this straight to JSON.
    """
    registry = build_registry()
    scores = db.recent_scores(limit=20)
    posts = db.social_post_integrity()
    runs = db.recent_runs()
    spread = db.metric_raw_spread()

    candidates = [
        {
            "token_key": row["token_key"],
            "symbol": row["token_key"].split(":")[-1][:12],
            "composite": row["composite"],
            "coverage": row["coverage"],
            "regime": row["regime"],
            "vetoes": list(row["vetoes"] or []),
            "refused": bool(row["vetoes"])
            or row["composite"] < settings.scoring.entry_threshold
            or row["coverage"] < settings.scoring.min_coverage,
            "as_of": row["as_of"],
            "contaminated": row["as_of"] < CONTAMINATED_BEFORE,
            "explanation": row["explanation"],
        }
        for row in scores
    ]

    families: dict[str, dict[str, Any]] = {}
    for family, metric_ids in registry.families().items():
        measured = [m for m in metric_ids if spread.get(m, {}).get("count")]
        families[family] = {
            "total": len(metric_ids),
            "measured": len(measured),
            "metric_ids": sorted(metric_ids),
        }

    counts = db.counts()
    return {
        "generated_at": utcnow(),
        "mode": "static",
        "trading_mode": settings.trading_mode.value,
        "candidates": candidates,
        "families": families,
        "integrity": [f.__dict__ for f in check_integrity(posts, runs, spread)],
        "posts": posts,
        "counts": counts,
        "runs": runs[:12],
        "weights_version": settings.scoring.weights_version,
        "entry_threshold": settings.scoring.entry_threshold,
        "min_coverage": settings.scoring.min_coverage,
        "labelled_outcomes": counts.get("outcomes", 0),
        "contaminated_before": CONTAMINATED_BEFORE,
    }


__all__ = ["CONTAMINATED_BEFORE", "build_snapshot"]
