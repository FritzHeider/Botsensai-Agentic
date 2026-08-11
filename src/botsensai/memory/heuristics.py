"""Heuristic formation from post-mortems.

Generalizes trade post-mortems into durable agentic heuristics, clustered by
exit reason and metric profile.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from botsensai.memory.store import MemoryStore
from botsensai.models import MemoryEntry, MemoryKind
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

KNOWN_EXIT_REASONS = (
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "max_hold_time",
    "vetoed",
    "dev_sold",
    "manual",
)


def _extract_exit_reason(entry: MemoryEntry) -> str:
    """Extract exit reason from post-mortem tags or body."""
    for tag in entry.tags:
        t = tag.lower().strip()
        if t in KNOWN_EXIT_REASONS:
            return t

    body_lower = entry.body.lower()
    for reason in KNOWN_EXIT_REASONS:
        if reason in body_lower or reason.replace("_", " ") in body_lower:
            return reason

    for tag in entry.tags:
        t = tag.lower().strip()
        if t not in ("postmortem", "loss", "profit", "entry"):
            return t

    return "unknown"


def _extract_db_features(
    db: Database, subject: str, cutoff: datetime
) -> dict[str, str]:
    features: dict[str, str] = {}
    mvs = db.metric_values_as_of(subject, cutoff)
    for mv in mvs:
        if not mv.usable or mv.normalized is None:
            continue
        if mv.normalized <= 0.35:
            features[mv.metric_id] = "low"
        elif mv.normalized >= 0.65:
            features[mv.metric_id] = "high"
    return features


def _extract_evidence_features(evidence: list[str]) -> dict[str, str]:
    features: dict[str, str] = {}
    for ev in evidence:
        if "=" not in ev:
            continue
        k, _, v = ev.partition("=")
        try:
            val = float(v)
            if val <= 0.35:
                features[k.strip()] = "low"
            elif val >= 0.65:
                features[k.strip()] = "high"
        except ValueError:
            pass
    return features


def _extract_metric_features(
    entry: MemoryEntry, db: Database | None, as_of: datetime | None
) -> dict[str, str]:
    """Identify metric features (e.g. metric_id -> 'low' | 'high') for a post-mortem."""
    if db is not None and entry.subject:
        cutoff = as_of or entry.created_at
        features = _extract_db_features(db, entry.subject, cutoff)
        if features:
            return features

    if entry.evidence:
        return _extract_evidence_features(entry.evidence)

    return {}



def _cluster_key(exit_reason: str, metric_id: str, level: str) -> tuple[str, str, str]:
    return (exit_reason, metric_id, level)


def _build_clusters(
    postmortems: list[MemoryEntry], db: Database | None, as_of: datetime | None
) -> dict[tuple[str, str, str], list[MemoryEntry]]:
    """Group post-mortems into clusters by (exit_reason, metric_id, level)."""
    clusters: dict[tuple[str, str, str], list[MemoryEntry]] = defaultdict(list)

    for entry in postmortems:
        exit_reason = _extract_exit_reason(entry)
        features = _extract_metric_features(entry, db, as_of)

        if features:
            for metric_id, level in features.items():
                key = _cluster_key(exit_reason, metric_id, level)
                clusters[key].append(entry)
        else:
            key = _cluster_key(exit_reason, "general", "observed")
            clusters[key].append(entry)

    return clusters


def _calculate_confidence(cluster_size: int) -> float:
    """Set confidence monotonically based on the size of the supporting cluster."""
    base = 0.4 + 0.12 * math.log2(max(1, cluster_size))
    return min(0.95, max(0.4, round(base, 4)))


def _create_heuristic_entry(
    store: MemoryStore,
    key: tuple[str, str, str],
    cluster: list[MemoryEntry],
    as_of: datetime | None,
) -> MemoryEntry:
    """Create and persist a HEURISTIC memory entry for a cluster."""
    exit_reason, metric_id, level = key
    confidence = _calculate_confidence(len(cluster))

    evidence_citations: list[str] = []
    for pm in cluster:
        if pm.evidence:
            evidence_citations.extend(pm.evidence[:2])
        evidence_citations.append(f"postmortem:{pm.id}:{pm.subject}")

    # Remove duplicates while preserving order
    evidence = list(dict.fromkeys(evidence_citations))

    if metric_id != "general":
        title = f"Heuristic: Elevated {exit_reason} risk when {metric_id} is {level}"
        body = (
            f"Observed {len(cluster)} trade post-mortems exiting via {exit_reason} "
            f"when signal {metric_id} was {level}. Supporting cluster size: {len(cluster)}."
        )
        tags = ["heuristic", exit_reason, metric_id, level]
    else:
        title = f"Heuristic: Pattern of {exit_reason} exits"
        body = (
            f"Observed {len(cluster)} trade post-mortems exiting via {exit_reason}. "
            f"Supporting cluster size: {len(cluster)}."
        )
        tags = ["heuristic", exit_reason]

    existing = store.recall(
        kinds=[MemoryKind.HEURISTIC],
        subject="global",
        query=title,
        limit=1,
        apply_decay=False,
        as_of=as_of,
    )
    supersedes_id = existing[0].id if existing and existing[0].title == title else None

    return store.remember(
        kind=MemoryKind.HEURISTIC,
        subject="global",
        title=title,
        body=body,
        tags=tags,
        confidence=confidence,
        evidence=evidence,
        supersedes=supersedes_id,
        created_at=as_of,
    )


def form_heuristics_from_postmortems(
    store: MemoryStore,
    db: Database | None = None,
    *,
    min_cluster_size: int = 2,
    as_of: datetime | None = None,
) -> list[MemoryEntry]:
    """Form and persist HEURISTIC memories from trade post-mortems.

    Clusters post-mortems by exit reason and metric profile, and sets confidence
    based on the size of the supporting cluster. Every generated heuristic cites
    the post-mortems and trades it came from.
    """
    postmortems = store.recall(
        kinds=[MemoryKind.POSTMORTEM],
        as_of=as_of,
        limit=1000,
        apply_decay=False,
        min_confidence=0.0,
    )

    if not postmortems:
        log.debug("heuristics.no_postmortems")
        return []

    clusters = _build_clusters(postmortems, db, as_of)
    heuristics: list[MemoryEntry] = []

    for key, cluster in sorted(clusters.items()):
        if len(cluster) < min_cluster_size:
            continue
        entry = _create_heuristic_entry(store, key, cluster, as_of)
        heuristics.append(entry)

    log.info("heuristics.formed", count=len(heuristics), postmortems=len(postmortems))
    return heuristics


__all__ = ["form_heuristics_from_postmortems"]
