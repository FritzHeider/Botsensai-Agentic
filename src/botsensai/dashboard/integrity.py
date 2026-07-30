"""Checks that turn silent collection failures into visible alarms.

Every check below is a number that was wrong, undetected, for one of the bugs
found on 2026-07-29. None of them raised an error at the time; the system
reported a healthy verified session throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A metric evaluated on at least this many tokens is expected to vary.
MIN_TOKENS_FOR_VARIANCE = 3


@dataclass(frozen=True)
class IntegrityFlag:
    """One integrity check's verdict. `level` is 'ok', 'warn' or 'alarm'."""

    id: str
    level: str
    headline: str
    detail: str


def _totals(posts: dict[str, dict[str, int]], key: str) -> int:
    return sum(int(stats.get(key, 0)) for stats in posts.values())


def check_integrity(
    posts: dict[str, dict[str, int]],
    runs: list[dict[str, Any]],
    spread: dict[str, dict[str, Any]],
) -> list[IntegrityFlag]:
    """Run every integrity check and return one flag each, in display order."""
    return [
        _check_reachable(posts),
        _check_author_resolution(posts),
        _check_author_ages(posts),
        _check_session_fields(posts),
        _check_surfaces(runs),
        _check_metric_variance(spread),
    ]


def _check_reachable(posts: dict[str, dict[str, int]]) -> IntegrityFlag:
    """Posts with no token_key are stored complete and unreadable by metrics."""
    total = _totals(posts, "total")
    reachable = _totals(posts, "reachable")
    if total == 0:
        return IntegrityFlag("posts_reachable", "ok", "No posts collected yet", "")
    if reachable == 0:
        return IntegrityFlag(
            "posts_reachable", "alarm", "No collected post is readable by any metric",
            f"{reachable} of {total} posts carry a token_key. posts_as_of filters on "
            "that column, so the rest are invisible to the social family.",
        )
    if reachable < total:
        return IntegrityFlag(
            "posts_reachable", "warn", "Some posts are unreachable",
            f"{reachable} of {total} posts carry a token_key.",
        )
    return IntegrityFlag(
        "posts_reachable", "ok", "All posts reachable", f"{reachable} of {total}."
    )


def _check_author_resolution(posts: dict[str, dict[str, int]]) -> IntegrityFlag:
    """Unresolved authors collapse distinct accounts into a fake shill signal."""
    total = _totals(posts, "total")
    unresolved = _totals(posts, "unresolved_authors")
    distinct = _totals(posts, "distinct_authors")
    if total == 0:
        return IntegrityFlag("author_resolution", "ok", "No posts collected yet", "")
    if unresolved >= total or distinct <= 1:
        return IntegrityFlag(
            "author_resolution", "alarm", "Post authors did not resolve",
            f"{unresolved} of {total} posts have no usable author and only {distinct} "
            "distinct author(s) were seen. mention_author_diversity reads this as one "
            "account posting everything, which is a manufactured shill signal.",
        )
    if unresolved > total * 0.2:
        return IntegrityFlag(
            "author_resolution", "warn", "Many authors unresolved",
            f"{unresolved} of {total} posts fell back to an account id.",
        )
    return IntegrityFlag(
        "author_resolution", "ok", "Authors resolved",
        f"{distinct} distinct authors across {total} posts.",
    )


def _check_author_ages(posts: dict[str, dict[str, int]]) -> IntegrityFlag:
    """engager_age_dispersion needs author_created_at and silently skips without it."""
    total = _totals(posts, "total")
    aged = _totals(posts, "with_author_age")
    if total == 0:
        return IntegrityFlag("author_ages", "ok", "No posts collected yet", "")
    if aged == 0:
        return IntegrityFlag(
            "author_ages", "alarm", "No author creation dates captured",
            f"0 of {total} posts carry author_created_at, so engager_age_dispersion "
            "cannot run at all.",
        )
    if aged < total * 0.5:
        return IntegrityFlag(
            "author_ages", "warn", "Sparse author creation dates",
            f"{aged} of {total} posts carry author_created_at.",
        )
    return IntegrityFlag(
        "author_ages", "ok", "Author ages captured", f"{aged} of {total} posts."
    )


def _check_session_fields(posts: dict[str, dict[str, int]]) -> IntegrityFlag:
    """views and bookmarks exist on no free X path; their absence means no session."""
    total = _totals(posts, "total")
    with_views = _totals(posts, "with_views")
    with_bookmarks = _totals(posts, "with_bookmarks")
    if total == 0:
        return IntegrityFlag("session_fields", "ok", "No posts collected yet", "")
    if with_views == 0 and with_bookmarks == 0:
        return IntegrityFlag(
            "session_fields", "warn", "No session-only fields present",
            f"0 of {total} posts carry views or bookmarks, so engagement_depth_ratio "
            "has no input. Expected when session collection is off.",
        )
    return IntegrityFlag(
        "session_fields", "ok", "Session fields present",
        f"views on {with_views}, bookmarks on {with_bookmarks}, of {total} posts.",
    )


def _check_surfaces(runs: list[dict[str, Any]]) -> IntegrityFlag:
    """A surface that failed or produced nothing is not the same as a quiet market."""
    if not runs:
        return IntegrityFlag("surface_health", "ok", "No runs recorded yet", "")
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        latest.setdefault(run["surface"], run)
    failed = [s for s, r in latest.items() if not r.get("ok")]
    empty = [s for s, r in latest.items() if r.get("ok") and not r.get("records")]
    if failed:
        detail = ", ".join(
            f"{s}: {latest[s].get('error') or 'failed'}" for s in sorted(failed)
        )
        return IntegrityFlag(
            "surface_health", "alarm", f"{len(failed)} surface(s) failed", detail
        )
    if empty:
        return IntegrityFlag(
            "surface_health", "warn", f"{len(empty)} surface(s) returned nothing",
            ", ".join(sorted(empty)),
        )
    return IntegrityFlag(
        "surface_health", "ok", "All surfaces healthy", f"{len(latest)} surfaces."
    )


def _check_metric_variance(spread: dict[str, dict[str, Any]]) -> IntegrityFlag:
    """A metric with one raw value across every token is a constant, not a signal."""
    if not spread:
        return IntegrityFlag("metric_variance", "ok", "No metric values yet", "")
    flat = [
        metric_id
        for metric_id, stats in spread.items()
        if stats.get("count", 0) >= MIN_TOKENS_FOR_VARIANCE and stats.get("distinct", 0) <= 1
    ]
    if flat:
        return IntegrityFlag(
            "metric_variance", "alarm",
            f"{len(flat)} metric(s) returned a constant",
            "Same raw value on every token: " + ", ".join(sorted(flat))
            + ". That is a constant, not a signal.",
        )
    return IntegrityFlag(
        "metric_variance", "ok", "Metrics vary across tokens", f"{len(spread)} metrics."
    )
