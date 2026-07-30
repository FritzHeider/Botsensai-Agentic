# Dashboard: Static Build & Collection Integrity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `botsensai dashboard --out FILE` — a single self-contained HTML file showing candidates, metric coverage, and a collection-integrity panel that makes silent collection failures visible.

**Architecture:** A read-only query layer (`data.py`) builds one plain-dict snapshot from the existing `Database`; `integrity.py` derives alarm flags from that snapshot; `render.py` turns it into HTML with Jinja2, inlining all CSS so the artifact has zero external references. No server, no API key, no build step.

**Tech Stack:** Python 3.11+, Jinja2 (already a dependency), stdlib sqlite3 via the existing `Database`, pytest.

## Global Constraints

- **Zero new dependencies.** `jinja2` and `httpx` are already in `pyproject.toml`. Do not add any package.
- **No external assets.** The generated HTML must contain no `http://` or `https://` `src`/`href` references and no CDN links. Inline all CSS.
- **P5-01 acceptance command must pass verbatim:** `python -m botsensai.cli dashboard --out /tmp/dash.html && python -c "import pathlib; h=pathlib.Path('/tmp/dash.html').read_text(); assert '<html' in h and len(h)>5000"`
- **Never render `MISSING` as a number.** A metric with `confidence == "missing"` renders the literal string `MISSING` plus its reason. Rendering `0.0` is the bug `README.md` names as poisoning composites.
- **Banned substrings anywhere under `src/botsensai`:** `x_password`, `twitter_password`, `def login(`, `fill_login`, `type_password`. Enforced by `test_no_credential_field_exists_anywhere`. Do not name anything `login`.
- **Secrets from environment only.** This plan introduces no secret at all; the static build must contain no key.
- **Line length 100**, enforced by `ruff check src tests`.
- **All 125 existing tests must keep passing.**
- Timestamps in SQLite are **REAL unix floats**, not ISO strings. Use the existing `_ts` / `_dt` helpers in `store/db.py`. Do not use SQLite `datetime()` on these columns.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/botsensai/store/db.py` (modify) | Add three read-only query helpers. No schema change. |
| `src/botsensai/pipeline.py` (modify) | Call the existing-but-unused `record_run` so `collector_runs` stops being dead. |
| `src/botsensai/dashboard/__init__.py` (create) | Package marker + public exports. |
| `src/botsensai/dashboard/data.py` (create) | Build one `dict` snapshot. The single source of truth. |
| `src/botsensai/dashboard/integrity.py` (create) | Pure functions: snapshot → list of integrity flags. |
| `src/botsensai/dashboard/render.py` (create) | Snapshot → HTML string via Jinja2. |
| `src/botsensai/dashboard/templates/dashboard.html.j2` (create) | The markup, with inlined CSS. |
| `src/botsensai/cli.py` (modify) | Add the `dashboard` command. |
| `tests/test_dashboard.py` (create) | All tests for the above. |

---

### Task 1: Dashboard read helpers on `Database`

The dashboard needs three queries that do not exist. `recent_scores` is needed
because `insert_score` has no counterpart reader at all.

**Files:**
- Modify: `src/botsensai/store/db.py` (add methods to `Database`, after `counts`, around line 840)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: existing `Database`, `_dt`, `_unjson` helpers in the same module.
- Produces:
  - `Database.recent_scores(limit: int = 20) -> list[dict[str, Any]]`
  - `Database.social_post_integrity(since: float | None = None) -> dict[str, dict[str, int]]`
  - `Database.metric_raw_spread() -> dict[str, dict[str, Any]]`

- [x] **Step 1: Write the failing tests**

Create `tests/test_dashboard.py`:

```python
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'recent_scores'`

- [x] **Step 3: Implement the three helpers**

In `src/botsensai/store/db.py`, add to the `Database` class immediately after `counts`:

```python
    # -- dashboard reads ---------------------------------------------------- #

    def recent_scores(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent scores, newest first.

        Returns plain dicts rather than `Score` models: the row stores a
        `token_key` string, not a full `TokenRef`, and reconstructing one would
        invent a chain/mint split the display does not need.
        """
        rows = self.conn.execute(
            "SELECT * FROM scores ORDER BY as_of DESC, composite DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "token_key": r["token_key"],
                "as_of": _dt(r["as_of"]),
                "composite": float(r["composite"]),
                "coverage": float(r["coverage"]),
                "regime": r["regime"] or "unknown",
                "weights_version": r["weights_version"] or "v0",
                "vetoes": _unjson(r["vetoes"]),
                "explanation": r["explanation"],
            }
            for r in rows
        ]

    def social_post_integrity(self, since: float | None = None) -> dict[str, dict[str, int]]:
        """Per-platform counts that make silent collection failures visible.

        `reachable` is the load-bearing one: a post whose `token_key` is NULL is
        stored complete and invisible to every metric, because `posts_as_of`
        filters on that column.
        """
        clause = " WHERE observed_at >= ?" if since is not None else ""
        params: list[Any] = [since] if since is not None else []
        rows = self.conn.execute(
            f"""SELECT platform,
                       COUNT(*) AS total,
                       SUM(CASE WHEN token_key IS NOT NULL AND token_key != ''
                                THEN 1 ELSE 0 END) AS reachable,
                       COUNT(DISTINCT author) AS distinct_authors,
                       SUM(CASE WHEN author = 'unknown' OR author LIKE 'id:%'
                                THEN 1 ELSE 0 END) AS unresolved_authors,
                       SUM(CASE WHEN views IS NOT NULL THEN 1 ELSE 0 END) AS with_views,
                       SUM(CASE WHEN bookmarks IS NOT NULL THEN 1 ELSE 0 END) AS with_bookmarks,
                       SUM(CASE WHEN author_created_at IS NOT NULL
                                THEN 1 ELSE 0 END) AS with_author_age
                FROM social_posts{clause}
                GROUP BY platform""",
            params,
        ).fetchall()
        return {
            r["platform"]: {
                "total": int(r["total"]),
                "reachable": int(r["reachable"] or 0),
                "distinct_authors": int(r["distinct_authors"] or 0),
                "unresolved_authors": int(r["unresolved_authors"] or 0),
                "with_views": int(r["with_views"] or 0),
                "with_bookmarks": int(r["with_bookmarks"] or 0),
                "with_author_age": int(r["with_author_age"] or 0),
            }
            for r in rows
        }

    def metric_raw_spread(self) -> dict[str, dict[str, Any]]:
        """Distinct raw values per metric in the newest batch.

        A metric returning one raw value for every token is reporting a
        constant, not a signal. That is exactly how a clamped entropy term hid
        for the life of the project, and it is cheap to detect.
        """
        latest = self.conn.execute("SELECT MAX(as_of) AS t FROM metric_values").fetchone()
        if latest is None or latest["t"] is None:
            return {}
        cutoff = float(latest["t"]) - 300.0
        rows = self.conn.execute(
            """SELECT metric_id,
                      COUNT(*) AS n,
                      COUNT(DISTINCT ROUND(raw, 6)) AS distinct_raw,
                      MIN(raw) AS lo,
                      MAX(raw) AS hi
               FROM metric_values
               WHERE as_of >= ? AND raw IS NOT NULL AND confidence != 'missing'
               GROUP BY metric_id""",
            (cutoff,),
        ).fetchall()
        return {
            r["metric_id"]: {
                "count": int(r["n"]),
                "distinct": int(r["distinct_raw"] or 0),
                "min": float(r["lo"]) if r["lo"] is not None else None,
                "max": float(r["hi"]) if r["hi"] is not None else None,
            }
            for r in rows
        }
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -v`
Expected: 3 passed

- [x] **Step 5: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 128 passed, "All checks passed!"

- [x] **Step 6: Commit**

```bash
git add src/botsensai/store/db.py tests/test_dashboard.py
git commit -m "feat: add dashboard read helpers to Database"
```

---

### Task 2: Record collector runs

`Database.record_run` exists and **nothing calls it** — `collector_runs` has zero
rows. Without this the integrity panel cannot show that a surface timed out,
which is the check that would have caught the X enrich being killed at 100s on
every sweep.

**Files:**
- Modify: `src/botsensai/pipeline.py` (in `enrich`, around line 268-290)
- Modify: `src/botsensai/store/db.py` (add `recent_runs` after `metric_raw_spread`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `CollectionResult` fields `surface`, `started_at`, `finished_at`, `ok`, `degraded`, `error`, and the `record_count` property.
- Produces: `Database.recent_runs(limit: int = 40) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -k "runs" -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'recent_runs'`

- [ ] **Step 3: Add `recent_runs` to `Database`**

In `src/botsensai/store/db.py`, after `metric_raw_spread`:

```python
    def recent_runs(self, limit: int = 40) -> list[dict[str, Any]]:
        """Latest collector runs, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM collector_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "surface": r["surface"],
                "started_at": _dt(r["started_at"]),
                "finished_at": _dt(r["finished_at"]),
                "ok": bool(r["ok"]),
                "records": int(r["records"] or 0),
                "error": r["error"],
            }
            for r in rows
        ]
```

- [ ] **Step 4: Wire the pipeline to record runs**

In `src/botsensai/pipeline.py`, inside `enrich`, replace:

```python
        results = await self.collectors.sweep_enrich(tokens)
        combined = CollectorRegistry.combine(results, surface="enrich")
```

with:

```python
        results = await self.collectors.sweep_enrich(tokens)

        # Record each surface separately before combining. `combine` folds
        # everything into one result, which is what made a single surface being
        # killed by its timeout invisible: the sweep reported "degraded: enrich"
        # and never said which surface, or that it had produced nothing.
        run_id = uuid.uuid4().hex
        for outcome in results:
            self.db.record_run(
                run_id=run_id,
                surface=outcome.surface,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                ok=outcome.ok and not outcome.degraded,
                records=outcome.record_count,
                error=outcome.error,
            )

        combined = CollectorRegistry.combine(results, surface="enrich")
```

Add `import uuid` to the imports at the top of `pipeline.py` if it is not
already present.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -k "runs" -v`
Expected: 2 passed

- [ ] **Step 6: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 130 passed, "All checks passed!"

- [ ] **Step 7: Commit**

```bash
git add src/botsensai/store/db.py src/botsensai/pipeline.py tests/test_dashboard.py
git commit -m "feat: record collector runs per surface so timeouts are visible"
```

---

### Task 3: Integrity checks

Six pure functions over the snapshot. Each one is a number that was visibly
wrong for one of the five bugs found on 2026-07-29.

**Files:**
- Create: `src/botsensai/dashboard/__init__.py`
- Create: `src/botsensai/dashboard/integrity.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: the dicts produced by Task 1 and Task 2.
- Produces:
  - `IntegrityFlag` dataclass with fields `id: str`, `level: str`, `headline: str`, `detail: str`
  - `check_integrity(posts: dict, runs: list, spread: dict) -> list[IntegrityFlag]`
  - Levels are exactly `"ok"`, `"warn"`, `"alarm"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -k "integrity" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'botsensai.dashboard'`

- [ ] **Step 3: Create the package marker**

Create `src/botsensai/dashboard/__init__.py`:

```python
"""Local dashboard: a static HTML build over the existing store.

The panels here exist to make *silent* collection failures visible. Five bugs
found on 2026-07-29 all presented as a healthy system with nothing logged, and
two of them fabricated evidence rather than hiding it — so integrity comes
before presentation in this package.
"""

from botsensai.dashboard.integrity import IntegrityFlag, check_integrity

__all__ = ["IntegrityFlag", "check_integrity"]
```

- [ ] **Step 4: Implement the checks**

Create `src/botsensai/dashboard/integrity.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -k "integrity" -v`
Expected: 5 passed

- [ ] **Step 6: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 135 passed, "All checks passed!"

- [ ] **Step 7: Commit**

```bash
git add src/botsensai/dashboard/ tests/test_dashboard.py
git commit -m "feat: add collection integrity checks"
```

---

### Task 4: Snapshot builder

One function that reads everything the template needs. Both this plan's static
mode and the later served mode render from this, so they cannot disagree.

**Files:**
- Create: `src/botsensai/dashboard/data.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `Database.recent_scores`, `.social_post_integrity`, `.metric_raw_spread`, `.recent_runs`, `.counts`; `build_registry()`; `Settings`.
- Produces: `build_snapshot(settings: Settings, db: Database) -> dict[str, Any]` with top-level keys: `generated_at`, `mode`, `trading_mode`, `candidates`, `families`, `integrity`, `counts`, `runs`, `weights_version`, `entry_threshold`, `min_coverage`, `contaminated_before`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard.py`:

```python
def test_snapshot_marks_pre_fix_scores_as_contaminated(db: Database):
    from datetime import datetime, timezone

    from botsensai.config import Settings
    from botsensai.dashboard.data import CONTAMINATED_BEFORE, build_snapshot

    old = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    db.insert_score(Score(token=_token("OLD", "1"), as_of=old, composite=0.5, coverage=0.4))
    db.insert_score(Score(token=_token("NEW", "2"), as_of=utcnow(), composite=0.5, coverage=0.4))

    snap = build_snapshot(Settings(), db)
    by_key = {c["token_key"]: c for c in snap["candidates"]}

    assert by_key["solana:" + "1" * 44]["contaminated"] is True
    assert by_key["solana:" + "2" * 44]["contaminated"] is False
    assert snap["contaminated_before"] == CONTAMINATED_BEFORE
    assert snap["families"], "metric families must be present"
    assert "integrity" in snap
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dashboard.py -k "snapshot" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'botsensai.dashboard.data'`

- [ ] **Step 3: Implement the snapshot builder**

Create `src/botsensai/dashboard/data.py`:

```python
"""Build the single read-only snapshot both dashboard modes render from."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from botsensai.config import Settings
from botsensai.dashboard.integrity import check_integrity
from botsensai.metrics import build_registry
from botsensai.models import utcnow
from botsensai.store.db import Database

#: Scores written before this instant were produced by a collection path with
#: known-wrong author parsing, which penalised every token for fabricated author
#: concentration. They are displayed, but never as a baseline.
CONTAMINATED_BEFORE = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_dashboard.py -k "snapshot" -v`
Expected: 1 passed

- [ ] **Step 5: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 136 passed, "All checks passed!"

- [ ] **Step 6: Commit**

```bash
git add src/botsensai/dashboard/data.py tests/test_dashboard.py
git commit -m "feat: build the dashboard snapshot"
```

---

### Task 5: Render the static HTML

**Files:**
- Create: `src/botsensai/dashboard/render.py`
- Create: `src/botsensai/dashboard/templates/dashboard.html.j2`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `build_snapshot`'s dict.
- Produces: `render_html(snapshot: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -k "render" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'botsensai.dashboard.render'`

- [ ] **Step 3: Create the template**

Create `src/botsensai/dashboard/templates/dashboard.html.j2`:

```html
<!doctype html>
<html lang="en" data-mode="{{ snap.mode }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>botsensai — {{ snap.trading_mode }}</title>
<style>
:root{--bg:#0b0d10;--panel:#12151a;--line:#232830;--text:#d7dde5;--dim:#7d8794;
--ok:#4a9d6a;--warn:#c08a3e;--alarm:#c0553e;--accent:#5b8bb5;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{border-bottom:1px solid var(--line);padding:10px 16px;display:flex;
justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.brand{letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.banner{color:var(--warn);font-weight:600;letter-spacing:.05em}
main{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);
gap:16px;padding:16px;align-items:start}
@media(max-width:900px){main{grid-template-columns:1fr}}
section{border:1px solid var(--line);background:var(--panel);margin-bottom:16px}
h2{margin:0;padding:8px 12px;border-bottom:1px solid var(--line);font-size:11px;
letter-spacing:.14em;text-transform:uppercase;color:var(--dim);font-weight:600}
.body{padding:10px 12px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11px;
text-transform:uppercase;letter-spacing:.08em;padding:6px 8px;
border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.veto{color:var(--alarm)}
.tag{display:inline-block;border:1px solid var(--line);padding:0 5px;
font-size:11px;color:var(--dim);white-space:nowrap}
.tag.pre{color:var(--warn);border-color:var(--warn)}
.flag{display:flex;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}
.flag:last-child{border-bottom:0}
.dot{width:8px;height:8px;border-radius:50%;margin-top:6px;flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.dot.alarm{background:var(--alarm)}
.flag .h{font-weight:600}
.flag .d{color:var(--dim)}
.bar{height:6px;background:var(--line);position:relative}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);display:block}
.dim{color:var(--dim)}
footer{padding:10px 16px;color:var(--dim);border-top:1px solid var(--line)}
</style>
</head>
<body>
<header>
  <span class="brand">botsensai</span>
  <span class="banner">{{ snap.trading_mode }} &middot; UNVALIDATED &middot;
    {{ snap.labelled_outcomes }} labelled outcomes</span>
  <span class="dim">generated {{ snap.generated_at.strftime('%Y-%m-%d %H:%M:%SZ') }}</span>
</header>
<main>
<div>
  <section>
    <h2>Candidates</h2>
    <div class="body">
      {% if snap.candidates %}
      <table>
        <tr><th>token</th><th class="num">score</th><th class="num">coverage</th>
            <th>state</th></tr>
        {% for c in snap.candidates %}
        <tr>
          <td>{{ c.symbol }}
            {% if c.contaminated %}<span class="tag pre">PRE-FIX</span>{% endif %}</td>
          <td class="num">{{ '%.3f'|format(c.composite) }}</td>
          <td class="num">{{ (c.coverage * 100)|round|int }}%</td>
          <td>
            {% if c.vetoes %}<span class="veto">{{ c.vetoes|join(', ') }}</span>
            {% elif c.refused %}<span class="dim">refused &mdash; below threshold
              {{ snap.entry_threshold }} / coverage {{ snap.min_coverage }}</span>
            {% else %}<span class="dim">eligible</span>{% endif %}
          </td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="dim">No scores yet &mdash; run <code>botsensai sweep</code>.</p>
      {% endif %}
    </div>
  </section>

  <section>
    <h2>Metric coverage by family</h2>
    <div class="body">
      {% if snap.families %}
      <table>
        <tr><th>family</th><th class="num">measured</th><th>&nbsp;</th></tr>
        {% for name, f in snap.families.items() %}
        <tr>
          <td>{{ name }}</td>
          <td class="num">{{ f.measured }}/{{ f.total }}</td>
          <td><span class="bar"><i style="width:{{
            (100 * f.measured / f.total)|round|int if f.total else 0 }}%"></i></span></td>
        </tr>
        {% endfor %}
      </table>
      <p class="dim">A family at 0 measured is reporting MISSING, not zero.</p>
      {% else %}
      <p class="dim">No metric values yet &mdash; MISSING, not zero.</p>
      {% endif %}
    </div>
  </section>
</div>

<div>
  <section>
    <h2>Collection integrity</h2>
    <div class="body">
      {% for f in snap.integrity %}
      <div class="flag">
        <span class="dot {{ f.level }}"></span>
        <span><span class="h">{{ f.headline }}</span><br>
          <span class="d">{{ f.detail }}</span></span>
      </div>
      {% endfor %}
    </div>
  </section>

  <section>
    <h2>Store</h2>
    <div class="body">
      <table>
        {% for name, n in snap.counts.items() %}
        <tr><td class="dim">{{ name }}</td><td class="num">{{ n }}</td></tr>
        {% endfor %}
      </table>
    </div>
  </section>
</div>
</main>
<footer>
  weights {{ snap.weights_version }} &middot; unfitted priors &middot;
  no edge is proven by anything on this page
</footer>
</body>
</html>
```

- [ ] **Step 4: Implement the renderer**

Create `src/botsensai/dashboard/render.py`:

```python
"""Snapshot -> self-contained HTML.

All CSS is inlined in the template and there are no external references: P5-01
requires the artifact to be one file with no build step and no network fetch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(snapshot: dict[str, Any]) -> str:
    """Render one snapshot to a complete HTML document."""
    template = _environment().get_template("dashboard.html.j2")
    return template.render(snap=snapshot)


__all__ = ["render_html"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -k "render" -v`
Expected: 3 passed

If `test_render_produces_a_self_contained_page` fails on length, the template is
being found but rendering little — check that `snap.candidates` is iterating.

**Deviation from the spec, deliberate:** the spec says charts are hand-rolled
inline SVG. This plan uses CSS bars instead — they satisfy the same constraint
(no external assets, no build step) with less markup, and the only quantities
shown are single proportions. If a later panel needs a real chart — a
distribution, a time series — load the `dataviz` skill before writing it, as the
spec requires.

- [ ] **Step 6: Ensure the template ships with the package**

Check `pyproject.toml` for a `[tool.setuptools.package-data]` section. If absent,
add:

```toml
[tool.setuptools.package-data]
botsensai = ["dashboard/templates/*.j2"]
```

- [ ] **Step 7: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 139 passed, "All checks passed!"

- [ ] **Step 8: Commit**

```bash
git add src/botsensai/dashboard/render.py src/botsensai/dashboard/templates/ \
        tests/test_dashboard.py pyproject.toml
git commit -m "feat: render the static dashboard"
```

---

### Task 6: The `dashboard` CLI command

**Files:**
- Modify: `src/botsensai/cli.py` (add a command; follow the existing `@app.command()` pattern used by `x_session` around line 573)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `build_snapshot`, `render_html`, the existing `_settings` helper in `cli.py`.
- Produces: CLI command `dashboard --out PATH [--config PATH]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dashboard.py -k "acceptance" -v`
Expected: FAIL — the CLI exits non-zero with "No such command 'dashboard'"

- [ ] **Step 3: Add the command**

In `src/botsensai/cli.py`, add after the `x_session` command:

```python
@app.command()
def dashboard(
    out: str = typer.Option(..., "--out", help="Where to write the HTML file."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Write a self-contained HTML dashboard.

    One file, no server, no external assets, safe to share. The integrity panel
    is the point of it: five collection bugs found on 2026-07-29 all presented
    as a healthy system, and two of them fabricated evidence rather than hiding
    it. Scores alone would have looked entirely plausible throughout.
    """
    from botsensai.dashboard.data import build_snapshot
    from botsensai.dashboard.render import render_html
    from botsensai.store.db import Database

    settings = _settings(config, log_level)
    db = Database(settings.path(settings.db_path))
    try:
        snapshot = build_snapshot(settings, db)
    finally:
        db.close()

    target = Path(out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(snapshot), encoding="utf-8")

    alarms = [f for f in snapshot["integrity"] if f["level"] == "alarm"]
    console.print(f"wrote {target} ({target.stat().st_size} bytes)")
    if alarms:
        console.print(f"[red]{len(alarms)} integrity alarm(s):[/red]")
        for flag in alarms:
            console.print(f"  [red]{flag['headline']}[/red] — {flag['detail']}")
```

Confirm `from pathlib import Path` is already imported at the top of `cli.py`;
add it if not.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_dashboard.py -k "acceptance" -v`
Expected: 1 passed

- [ ] **Step 5: Run the P5-01 acceptance command by hand**

Run:
```bash
python -m botsensai.cli dashboard --out /tmp/dash.html && \
python -c "import pathlib; h=pathlib.Path('/tmp/dash.html').read_text(); assert '<html' in h and len(h)>5000"
```
Expected: exit 0, and the command prints any integrity alarms.

- [ ] **Step 6: Confirm the artifact has no external references**

Run:
```bash
grep -c "http://\|https://" /tmp/dash.html || echo "no external references"
```
Expected: "no external references" (grep exits 1 when it finds nothing).

- [ ] **Step 7: Run the full suite and linter**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`
Expected: 140 passed, "All checks passed!"

- [ ] **Step 8: Update `@fix_plan.md`**

Mark P5-01 as done, and note what changed:

```markdown
- [x] **P5-01 — Live status dashboard**
  Done 2026-07-30. `botsensai dashboard --out FILE`. Adds a collection-integrity
  panel not in the original spec: five silent collection bugs on 2026-07-29
  motivated making integrity the primary panel rather than scores.
  Regeneration on every sweep is not wired yet.
```

- [ ] **Step 9: Commit**

```bash
git add src/botsensai/cli.py tests/test_dashboard.py @fix_plan.md
git commit -m "feat: add the dashboard CLI command"
```

---

## Verification

After Task 6, all of these must hold:

1. `python3 -m pytest -q` → 140 passed.
2. `python3 -m ruff check src tests` → All checks passed!
3. The P5-01 acceptance command exits 0.
4. `/tmp/dash.html` contains no `http://` or `https://`.
5. `python -m botsensai.cli dashboard --out /tmp/dash.html` prints an integrity
   alarm when `metric_raw_spread` shows a constant metric — verify by running a
   sweep first, then regenerating.

## Out of scope for this plan

Served mode, the Gemini chatbot, action buttons and the job runner. Those are
plans 2 and 3. Nothing in this plan starts a server, reads an API key, or sends
data anywhere.
