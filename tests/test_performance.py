"""Guard the store's hot read paths against silently becoming table scans.

The backtester asks a question per launch, and then several more per launch:
snapshots, trades, holders, posts, metric values, the deployer's prior record,
each wallet's prior history. Every one of those is filtered on `as_of` and
`observed_at`, and every one of them is inside a loop over the corpus. So the
cost of the whole backtest is (number of launches) x (cost of one read), and a
single read path that drops off its index turns a linear walk into a quadratic
one.

That regression is invisible to every other check in the suite. The results
stay correct, the assertions stay green, and the only symptom is that a run
which took a minute in July takes an hour in October. By then the change that
caused it is a hundred commits back.

So this pins the structural property instead of the timing: no hot read path
may plan a full scan of a table that grows with the corpus. It asserts on
SQLite's own query plan for the exact SQL and the exact parameters the helper
issued, rather than on a copy of the SQL kept here, because a copy drifts and a
drifted copy is worse than no test — it passes while the real query scans.

`scripts/benchmark.py` supplies the measured curve behind this. Measured
2026-07-30 across a 16x corpus (500 -> 8000 tokens, ~700k rows), best of 5
rounds: every read path came in between 1.0x and 1.7x, against tables that grew
16x. Nothing is proportional to corpus size, which is the same conclusion the
query plans give. `metric_values_as_of` is the absolute outlier at ~270us
rising to ~450us — it is the one path worth optimising if a backtest ever gets
slow, and the one to re-measure after any change to it.

Treat those absolute microseconds as soft. Repeated runs on a loaded machine
inflate an entire column at once — every path together, including ones that are
provably index-bound — so a single slow run says the laptop was busy, not that
something regressed. The growth *ratios* are the stable part, and the query
plan is the part that cannot drift at all, which is why this file asserts on
the plan and only documents the timings.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from botsensai.models import (
    HolderRecord,
    Launch,
    MarketSnapshot,
    MetricValue,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)
from botsensai.store.db import Database

# Tables whose row count grows with the corpus. A scan of one of these is the
# bug. `schema_meta`, `deployer_profiles` and friends are bounded or tiny, and
# a scan of those costs nothing worth failing a build over.
GROWING_TABLES = {
    "launches",
    "market_snapshots",
    "trades",
    "holders",
    "social_posts",
    "metric_values",
    "scores",
    "security_reports",
}


class _Spy:
    """A connection stand-in that records every `execute` and forwards it on.

    `sqlite3.Connection` is a C type whose `execute` cannot be reassigned, so
    the interception has to happen one level up, at the object `Database` hands
    out. Everything except `execute` is proxied straight through.
    """

    def __init__(self, real: sqlite3.Connection, calls: list[tuple[str, tuple]]) -> None:
        self._real = real
        self._calls = calls

    def execute(self, sql, params=()):
        self._calls.append((sql, tuple(params)))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Recorder:
    """Keeps every (sql, params) pair a `Database` call issued.

    Taking the SQL off the live connection rather than copying it into this
    file is the whole point: the assertion then cannot drift away from what
    `Database` really runs. That does mean reaching into `Database`'s private
    connection slots, which is the price of observing the real statement
    instead of a restatement of it.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._real = db.conn  # also forces the lazy connection into existence
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self) -> _Recorder:
        spy = _Spy(self._real, self.calls)
        if self._db._shared is not None:
            self._db._shared = spy  # type: ignore[assignment]
        else:
            self._db._local.conn = spy
        return self

    def __exit__(self, *exc) -> None:
        if self._db._shared is not None:
            self._db._shared = self._real
        else:
            self._db._local.conn = self._real


def _plan(conn: sqlite3.Connection, sql: str, params: tuple) -> list[str]:
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return [r["detail"] for r in rows]


def _scanned_tables(details: list[str]) -> set[str]:
    """Tables the planner said it would scan end to end.

    A `USE TEMP B-TREE FOR ORDER BY` line is a sort, not a scan, and a
    `CORRELATED SCALAR SUBQUERY` line is a header for the lines beneath it.
    Only `SCAN <name>` means reading every row.
    """
    scanned = set()
    for detail in details:
        text = detail.strip()
        if not text.startswith("SCAN "):
            continue
        # "SCAN trades" / "SCAN p" / "SCAN trades AS t"
        scanned.add(text.split()[1])
    return scanned


def _seed(db: Database, tokens: int = 3) -> list[str]:
    """A corpus small enough to be instant and wide enough to be realistic.

    Plan shape does not depend on row count here: nothing runs ANALYZE, so
    SQLite plans from fixed cardinality estimates. A handful of rows exercises
    the same planner decisions the real store does.
    """
    base = utcnow() - timedelta(days=2)
    keys = []
    for i in range(tokens):
        mint = f"mint{i}"
        keys.append(f"solana:{mint}")
        ref = TokenRef(chain="solana", mint=mint, symbol=f"S{i}")
        created = base + timedelta(minutes=i)
        db.upsert_launch(
            Launch(
                token=ref,
                launchpad="pumpfun",
                deployer="deployer0",
                created_at=created,
                observed_at=created,
                source="test",
            )
        )
        db.insert_snapshots(
            [
                MarketSnapshot(
                    token=ref, as_of=created, observed_at=created, price_usd=0.1, market_cap_usd=99.0
                )
            ]
        )
        db.insert_trades(
            [
                Trade(
                    token=ref,
                    signature=f"{mint}-sig",
                    as_of=created,
                    observed_at=created,
                    wallet="wallet0",
                    side="buy",
                    amount_token=10.0,
                    amount_native=0.5,
                    slot=100 + i,
                )
            ]
        )
        db.insert_holders(
            [
                HolderRecord(
                    token=ref,
                    as_of=created,
                    observed_at=created,
                    wallet="wallet0",
                    balance=10.0,
                    share_of_supply=0.2,
                )
            ]
        )
        db.insert_metric_values(
            [
                MetricValue(
                    metric_id="age_seconds",
                    token=ref,
                    as_of=created,
                    observed_at=created,
                    raw=1.0,
                    normalized=0.5,
                    confidence="high",
                )
            ]
        )
        db.insert_posts(
            [
                SocialPost(
                    platform="x",
                    post_id=f"{mint}-p",
                    author="author0",
                    text="gm",
                    as_of=created,
                    observed_at=created,
                )
            ],
            token_key=f"solana:{mint}",
        )
    return keys


def test_hot_read_paths_never_scan_a_growing_table(tmp_path):
    db = Database(str(tmp_path / "plans.db"))
    keys = _seed(db)
    token_key = keys[0]
    as_of = utcnow()

    # Every read the backtester makes per launch. Adding a read helper to this
    # loop is deliberately cheap; leaving one out is how the guard rots.
    reads = {
        "launch": lambda: db.launch(token_key),
        "launches_between": lambda: db.launches_between(as_of - timedelta(days=7), as_of),
        "snapshots_as_of": lambda: db.snapshots_as_of(token_key, as_of),
        "trades_as_of": lambda: db.trades_as_of(token_key, as_of),
        "holders_as_of": lambda: db.holders_as_of(token_key, as_of),
        "security_as_of": lambda: db.security_as_of(token_key, as_of),
        "posts_as_of": lambda: db.posts_as_of(token_key, as_of),
        "metric_values_as_of": lambda: db.metric_values_as_of(token_key, as_of),
        "metric_history": lambda: db.metric_history("age_seconds"),
        "outcome": lambda: db.outcome(token_key),
        "deployer_history": lambda: db.deployer_history("deployer0", as_of),
        "wallet_seen_before": lambda: db.wallet_seen_before("wallet0", as_of),
    }

    offenders: dict[str, set[str]] = {}
    for name, call in reads.items():
        with _Recorder(db) as recorder:
            call()
        assert recorder.calls, f"{name} issued no SQL; the spy is not wired up"
        for sql, params in recorder.calls:
            scanned = _scanned_tables(_plan(db.conn, sql, params)) & GROWING_TABLES
            if scanned:
                offenders.setdefault(name, set()).update(scanned)

    assert not offenders, (
        "these read paths plan a full table scan, which makes the backtest "
        f"quadratic in corpus size: {offenders}"
    )
    db.close()


def test_growing_tables_all_have_an_index_on_their_lookup_key(tmp_path):
    """Every corpus-scale table must be reachable by something other than rowid.

    This is the cheaper half of the same guard: it catches a new table added
    without an index before anyone writes the query that scans it.
    """
    db = Database(str(tmp_path / "indexes.db"))
    unindexed = []
    for table in sorted(GROWING_TABLES):
        indexes = db.conn.execute(f"PRAGMA index_list({table})").fetchall()
        if not indexes:
            unindexed.append(table)
    assert not unindexed, f"corpus-scale tables with no index at all: {unindexed}"
    db.close()


def test_the_scan_guard_actually_fails_on_a_scan(tmp_path):
    """The guard above is only worth having if it can fail.

    A query with no usable index must be reported, or
    `test_hot_read_paths_never_scan_a_growing_table` is green for the wrong
    reason and would stay green through the regression it exists to catch.
    """
    db = Database(str(tmp_path / "negative.db"))
    _seed(db)

    # `source` carries no index, so this can only be answered by reading every row.
    details = _plan(db.conn, "SELECT * FROM launches WHERE source = ?", ("test",))
    assert _scanned_tables(details) & GROWING_TABLES == {"launches"}, details
    db.close()
