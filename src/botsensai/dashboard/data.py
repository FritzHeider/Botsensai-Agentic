"""Build the single read-only snapshot both dashboard modes render from."""

from __future__ import annotations

import json
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
#:
#: Anchored on evidence, not on a round number or a commit timestamp. The sweep
#: at 2026-07-30T04:15:11Z is the first whose posts carry resolved authors — 171
#: distinct across 260 posts, zero "unknown". The preceding sweep at 03:24:09Z
#: still recorded every author as "unknown", which made
#: mention_author_diversity report one account posting everything. A commit
#: timestamp would be the wrong anchor: the fix was on disk and in effect for
#: the 04:15 sweep roughly six minutes before it was committed.
CONTAMINATED_BEFORE = datetime(2026, 7, 30, 4, 15, tzinfo=UTC)


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

    candidates = []
    for row in scores:
        token_key = row["token_key"]
        as_of = row["as_of"]
        mint = token_key.split(":")[-1]

        launch = db.launch(token_key)
        symbol = launch.token.symbol if launch and launch.token.symbol else mint[:12]

        family_scores: dict[str, float] = {
            "topology": float(row["composite"]),
            "social": float(row["composite"]),
            "community": float(row["composite"]),
            "narrative": float(row["composite"]),
            "credibility": float(row["composite"]),
            "execution": float(row["composite"]),
        }

        metric_values = db.metric_values_as_of(token_key, as_of)
        if metric_values:
            family_buckets: dict[str, list[float]] = {}
            for mv in metric_values:
                if mv.normalized is not None:
                    m_def = registry.get(mv.metric_id)
                    if m_def:
                        fam = m_def.family
                        key = (
                            "topology" if "topology" in fam
                            else "social" if "social" in fam
                            else "community" if "community" in fam
                            else "narrative" if "narrative" in fam
                            else "credibility" if "credibility" in fam
                            else "execution" if "execution" in fam
                            else fam
                        )
                        family_buckets.setdefault(key, []).append(float(mv.normalized))
            for k, vals in family_buckets.items():
                if vals and k in family_scores:
                    family_scores[k] = round(sum(vals) / len(vals), 3)

        candidates.append(
            {
                "token_key": token_key,
                "mint": mint,
                "symbol": symbol,
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
                "family_scores": family_scores,
            }
        )

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
        "candidates_json": json.dumps(candidates, default=str),
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
