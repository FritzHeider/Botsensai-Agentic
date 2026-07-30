"""SQLite-backed persistence with point-in-time query semantics.

SQLite is deliberate: the whole dataset for a research operation of this size
fits comfortably, it needs no server, and a backtest that reads a single file is
trivially reproducible. Every table that holds a fact carries both `as_of` and
`observed_at`, and the read helpers that a backtest uses (`*_as_of`) filter on
BOTH so that data backfilled later can never leak into a past decision.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from botsensai.models import (
    Chain,
    Confidence,
    CurveStage,
    HolderRecord,
    Launch,
    Launchpad,
    MarketSnapshot,
    MetricValue,
    Outcome,
    Platform,
    Score,
    SecurityReport,
    Side,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)
from botsensai.util.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS launches (
    token_key      TEXT PRIMARY KEY,
    chain          TEXT NOT NULL,
    mint           TEXT NOT NULL,
    symbol         TEXT,
    name           TEXT,
    launchpad      TEXT NOT NULL,
    deployer       TEXT,
    created_at     REAL NOT NULL,
    observed_at    REAL NOT NULL,
    description    TEXT,
    image_uri      TEXT,
    metadata_uri   TEXT,
    website        TEXT,
    twitter        TEXT,
    telegram       TEXT,
    initial_supply REAL,
    dev_buy_sol    REAL,
    source         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_launches_created ON launches(created_at);
CREATE INDEX IF NOT EXISTS ix_launches_deployer ON launches(deployer);
CREATE INDEX IF NOT EXISTS ix_launches_pad ON launches(launchpad, created_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    token_key               TEXT NOT NULL,
    as_of                   REAL NOT NULL,
    observed_at             REAL NOT NULL,
    stage                   TEXT,
    price_usd               REAL,
    price_native            REAL,
    market_cap_usd          REAL,
    fdv_usd                 REAL,
    liquidity_usd           REAL,
    volume_5m_usd           REAL,
    volume_1h_usd           REAL,
    volume_24h_usd          REAL,
    txns_5m_buys            INTEGER,
    txns_5m_sells           INTEGER,
    txns_1h_buys            INTEGER,
    txns_1h_sells           INTEGER,
    holder_count            INTEGER,
    bonding_curve_progress  REAL,
    pair_address            TEXT,
    dex                     TEXT,
    source                  TEXT,
    PRIMARY KEY (token_key, as_of, source)
);
CREATE INDEX IF NOT EXISTS ix_ms_token_time ON market_snapshots(token_key, as_of);
CREATE INDEX IF NOT EXISTS ix_ms_observed ON market_snapshots(observed_at);

CREATE TABLE IF NOT EXISTS trades (
    signature              TEXT NOT NULL,
    token_key              TEXT NOT NULL,
    slot                   INTEGER,
    as_of                  REAL NOT NULL,
    observed_at            REAL NOT NULL,
    wallet                 TEXT NOT NULL,
    side                   TEXT NOT NULL,
    amount_token           REAL NOT NULL,
    amount_native          REAL NOT NULL,
    price_usd              REAL,
    price_native           REAL,
    priority_fee_lamports  INTEGER,
    jito_tip_lamports      INTEGER,
    is_bundled             INTEGER,
    bundle_id              TEXT,
    source                 TEXT,
    PRIMARY KEY (signature, token_key, wallet, side)
);
CREATE INDEX IF NOT EXISTS ix_trades_token_time ON trades(token_key, as_of);
CREATE INDEX IF NOT EXISTS ix_trades_wallet ON trades(wallet, as_of);
CREATE INDEX IF NOT EXISTS ix_trades_slot ON trades(token_key, slot);

CREATE TABLE IF NOT EXISTS holders (
    token_key           TEXT NOT NULL,
    as_of               REAL NOT NULL,
    observed_at         REAL NOT NULL,
    wallet              TEXT NOT NULL,
    balance             REAL NOT NULL,
    share_of_supply     REAL NOT NULL,
    first_seen_at       REAL,
    wallet_age_seconds  REAL,
    funded_by           TEXT,
    labels              TEXT,
    PRIMARY KEY (token_key, as_of, wallet)
);
CREATE INDEX IF NOT EXISTS ix_holders_token_time ON holders(token_key, as_of);
CREATE INDEX IF NOT EXISTS ix_holders_funder ON holders(funded_by);

CREATE TABLE IF NOT EXISTS security_reports (
    token_key                 TEXT NOT NULL,
    as_of                     REAL NOT NULL,
    observed_at               REAL NOT NULL,
    mint_authority_revoked    INTEGER,
    freeze_authority_revoked  INTEGER,
    lp_burned_share           REAL,
    lp_locked_until           REAL,
    transfer_fee_bps          INTEGER,
    is_mutable_metadata       INTEGER,
    top10_share               REAL,
    insider_share             REAL,
    bundled_share             REAL,
    sniper_share              REAL,
    dev_sold                  INTEGER,
    dev_holding_share         REAL,
    source                    TEXT,
    PRIMARY KEY (token_key, as_of, source)
);
CREATE INDEX IF NOT EXISTS ix_sec_token_time ON security_reports(token_key, as_of);

CREATE TABLE IF NOT EXISTS social_posts (
    platform           TEXT NOT NULL,
    post_id            TEXT NOT NULL,
    token_key          TEXT,
    author             TEXT NOT NULL,
    author_id          TEXT,
    as_of              REAL NOT NULL,
    observed_at        REAL NOT NULL,
    text               TEXT,
    lang               TEXT,
    url                TEXT,
    parent_id          TEXT,
    is_repost          INTEGER,
    quoted_id          TEXT,
    likes              INTEGER,
    replies            INTEGER,
    reposts            INTEGER,
    quotes             INTEGER,
    bookmarks          INTEGER,
    views              INTEGER,
    media_urls         TEXT,
    media_hashes       TEXT,
    author_followers   INTEGER,
    author_created_at  REAL,
    mentioned_tokens   TEXT,
    source             TEXT,
    PRIMARY KEY (platform, post_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_posts_token_time ON social_posts(token_key, as_of);
CREATE INDEX IF NOT EXISTS ix_posts_author ON social_posts(platform, author);
CREATE INDEX IF NOT EXISTS ix_posts_observed ON social_posts(observed_at);

CREATE TABLE IF NOT EXISTS metric_values (
    metric_id    TEXT NOT NULL,
    token_key    TEXT NOT NULL,
    as_of        REAL NOT NULL,
    observed_at  REAL NOT NULL,
    raw          REAL,
    normalized   REAL,
    confidence   TEXT NOT NULL,
    inputs_used  TEXT,
    notes        TEXT,
    PRIMARY KEY (metric_id, token_key, as_of)
);
CREATE INDEX IF NOT EXISTS ix_mv_token_time ON metric_values(token_key, as_of);
CREATE INDEX IF NOT EXISTS ix_mv_metric ON metric_values(metric_id, as_of);

CREATE TABLE IF NOT EXISTS scores (
    token_key        TEXT NOT NULL,
    as_of            REAL NOT NULL,
    observed_at      REAL NOT NULL,
    composite        REAL NOT NULL,
    coverage         REAL NOT NULL,
    regime           TEXT,
    weights_version  TEXT,
    vetoes           TEXT,
    contributions    TEXT,
    explanation      TEXT,
    PRIMARY KEY (token_key, as_of)
);
CREATE INDEX IF NOT EXISTS ix_scores_time ON scores(as_of);
CREATE INDEX IF NOT EXISTS ix_scores_composite ON scores(composite);

CREATE TABLE IF NOT EXISTS outcomes (
    token_key                 TEXT PRIMARY KEY,
    labeled_at                REAL NOT NULL,
    graduated                 INTEGER,
    graduated_at              REAL,
    rugged                    INTEGER,
    peak_market_cap_usd       REAL,
    peak_at                   REAL,
    max_multiple_from_t0      REAL,
    time_to_peak_seconds      REAL,
    survived_1h               INTEGER,
    survived_24h              INTEGER,
    survived_7d               INTEGER,
    final_market_cap_usd      REAL,
    max_realizable_multiple   REAL
);

CREATE TABLE IF NOT EXISTS wallet_profiles (
    wallet             TEXT PRIMARY KEY,
    first_seen_at      REAL,
    last_seen_at       REAL,
    labels             TEXT,
    trade_count        INTEGER DEFAULT 0,
    win_count          INTEGER DEFAULT 0,
    realized_pnl_sol   REAL DEFAULT 0,
    median_hold_secs   REAL,
    funded_by          TEXT,
    cluster_id         TEXT,
    updated_at         REAL
);
CREATE INDEX IF NOT EXISTS ix_wallets_cluster ON wallet_profiles(cluster_id);

CREATE TABLE IF NOT EXISTS deployer_profiles (
    deployer        TEXT PRIMARY KEY,
    launch_count    INTEGER DEFAULT 0,
    rug_count       INTEGER DEFAULT 0,
    graduate_count  INTEGER DEFAULT 0,
    first_launch_at REAL,
    last_launch_at  REAL,
    best_multiple   REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS collector_runs (
    run_id       TEXT NOT NULL,
    surface      TEXT NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    ok           INTEGER,
    records      INTEGER DEFAULT 0,
    error        TEXT,
    PRIMARY KEY (run_id, surface, started_at)
);
"""


def _ts(value: datetime | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value.timestamp()


def _dt(value: float | None) -> datetime | None:
    if value is None:
        return None

    return datetime.fromtimestamp(float(value), tz=UTC)


def _json(value: Any) -> str:
    return orjson.dumps(value or []).decode()


def _unjson(value: str | None) -> Any:
    if not value:
        return []
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        return []


class Database:
    """Thread-safe SQLite wrapper. One instance per process is plenty."""

    def __init__(self, path: str | Path = "data/botsensai.db") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            # An in-memory DB must share one connection or each thread gets its
            # own empty database. Tests rely on this.
            self._shared = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared.row_factory = sqlite3.Row
        self.migrate()

    # -- connection --------------------------------------------------------- #

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Any:
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def migrate(self) -> None:
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # -- writes ------------------------------------------------------------- #

    def upsert_launch(self, launch: Launch) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO launches (token_key, chain, mint, symbol, name, launchpad,
                       deployer, created_at, observed_at, description, image_uri, metadata_uri,
                       website, twitter, telegram, initial_supply, dev_buy_sol, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(token_key) DO UPDATE SET
                       -- Both timestamps take the minimum, and neither is a
                       -- detail. The websocket ingester learns of a mint before
                       -- anything else does but is given no mint time by the
                       -- feed, so it writes its receipt time — an upper bound.
                       -- Letting the first writer pin `created_at` would leave
                       -- that approximation in place permanently and skew every
                       -- age screen; taking the minimum lets the REST path's
                       -- authoritative `created_timestamp` correct it. And
                       -- `observed_at` must be the *earliest* observation for
                       -- the latency measurement to mean anything, so a later
                       -- sweep re-seeing a token must not push it forward.
                       created_at=MIN(excluded.created_at, launches.created_at),
                       observed_at=MIN(excluded.observed_at, launches.observed_at),
                       symbol=COALESCE(excluded.symbol, launches.symbol),
                       name=COALESCE(excluded.name, launches.name),
                       deployer=COALESCE(excluded.deployer, launches.deployer),
                       description=COALESCE(excluded.description, launches.description),
                       image_uri=COALESCE(excluded.image_uri, launches.image_uri),
                       website=COALESCE(excluded.website, launches.website),
                       twitter=COALESCE(excluded.twitter, launches.twitter),
                       telegram=COALESCE(excluded.telegram, launches.telegram),
                       initial_supply=COALESCE(excluded.initial_supply, launches.initial_supply),
                       dev_buy_sol=COALESCE(excluded.dev_buy_sol, launches.dev_buy_sol)""",
                (
                    launch.token.key,
                    launch.token.chain.value,
                    launch.token.mint,
                    launch.token.symbol,
                    launch.token.name,
                    launch.launchpad.value,
                    launch.deployer,
                    _ts(launch.created_at),
                    _ts(launch.observed_at),
                    launch.description,
                    launch.image_uri,
                    launch.metadata_uri,
                    launch.website,
                    launch.twitter,
                    launch.telegram,
                    launch.initial_supply,
                    launch.dev_buy_sol,
                    launch.source,
                ),
            )

    def insert_snapshots(self, snapshots: Iterable[MarketSnapshot]) -> int:
        rows = [
            (
                s.token.key,
                _ts(s.as_of),
                _ts(s.observed_at),
                s.stage.value,
                s.price_usd,
                s.price_native,
                s.market_cap_usd,
                s.fdv_usd,
                s.liquidity_usd,
                s.volume_5m_usd,
                s.volume_1h_usd,
                s.volume_24h_usd,
                s.txns_5m_buys,
                s.txns_5m_sells,
                s.txns_1h_buys,
                s.txns_1h_sells,
                s.holder_count,
                s.bonding_curve_progress,
                s.pair_address,
                s.dex,
                s.source,
            )
            for s in snapshots
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO market_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def insert_trades(self, trades: Iterable[Trade]) -> int:
        rows = [
            (
                t.signature,
                t.token.key,
                t.slot,
                _ts(t.as_of),
                _ts(t.observed_at),
                t.wallet,
                t.side.value,
                t.amount_token,
                t.amount_native,
                t.price_usd,
                t.price_native,
                t.priority_fee_lamports,
                t.jito_tip_lamports,
                int(t.is_bundled) if t.is_bundled is not None else None,
                t.bundle_id,
                t.source,
            )
            for t in trades
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def insert_holders(self, holders: Iterable[HolderRecord]) -> int:
        rows = [
            (
                h.token.key,
                _ts(h.as_of),
                _ts(h.observed_at),
                h.wallet,
                h.balance,
                h.share_of_supply,
                _ts(h.first_seen_at),
                h.wallet_age_seconds,
                h.funded_by,
                _json(h.labels),
            )
            for h in holders
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany("INSERT OR REPLACE INTO holders VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def insert_security(self, report: SecurityReport) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO security_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.token.key,
                    _ts(report.as_of),
                    _ts(report.observed_at),
                    _b(report.mint_authority_revoked),
                    _b(report.freeze_authority_revoked),
                    report.lp_burned_share,
                    _ts(report.lp_locked_until),
                    report.transfer_fee_bps,
                    _b(report.is_mutable_metadata),
                    report.top10_share,
                    report.insider_share,
                    report.bundled_share,
                    report.sniper_share,
                    _b(report.dev_sold),
                    report.dev_holding_share,
                    report.source,
                ),
            )

    def insert_posts(self, posts: Iterable[SocialPost], token_key: str | None = None) -> int:
        """Store posts, tagged with the token they were collected for.

        `token_key` overrides for callers that hold one token's posts; otherwise
        each post's own `token_key` is used. One of the two must be present or the
        row is written unreachable — `posts_as_of` filters on this column, so an
        untagged post can never be read back by a metric.
        """
        rows = [
            (
                p.platform.value,
                p.post_id,
                token_key if token_key is not None else p.token_key,
                p.author,
                p.author_id,
                _ts(p.as_of),
                _ts(p.observed_at),
                p.text,
                p.lang,
                p.url,
                p.parent_id,
                int(p.is_repost),
                p.quoted_id,
                p.likes,
                p.replies,
                p.reposts,
                p.quotes,
                p.bookmarks,
                p.views,
                _json(p.media_urls),
                _json(p.media_hashes),
                p.author_followers,
                _ts(p.author_created_at),
                _json(p.mentioned_tokens),
                p.source,
            )
            for p in posts
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO social_posts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def insert_metric_values(self, values: Iterable[MetricValue]) -> int:
        rows = [
            (
                m.metric_id,
                m.token.key,
                _ts(m.as_of),
                _ts(m.observed_at),
                m.raw,
                m.normalized,
                m.confidence.value,
                _json(m.inputs_used),
                m.notes,
            )
            for m in values
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany("INSERT OR REPLACE INTO metric_values VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def insert_score(self, score: Score) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    score.token.key,
                    _ts(score.as_of),
                    _ts(score.observed_at),
                    score.composite,
                    score.coverage,
                    score.regime,
                    score.weights_version,
                    _json([v.value for v in score.vetoes]),
                    _json(score.contributions),
                    score.explanation,
                ),
            )

    def upsert_outcome(self, outcome: Outcome) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outcome.token.key,
                    _ts(outcome.labeled_at),
                    _b(outcome.graduated),
                    _ts(outcome.graduated_at),
                    _b(outcome.rugged),
                    outcome.peak_market_cap_usd,
                    _ts(outcome.peak_at),
                    outcome.max_multiple_from_t0,
                    outcome.time_to_peak_seconds,
                    _b(outcome.survived_1h),
                    _b(outcome.survived_24h),
                    _b(outcome.survived_7d),
                    outcome.final_market_cap_usd,
                    outcome.max_realizable_multiple,
                ),
            )

    # -- point-in-time reads ------------------------------------------------ #

    def launch(self, token_key: str) -> Launch | None:
        row = self.conn.execute(
            "SELECT * FROM launches WHERE token_key = ?", (token_key,)
        ).fetchone()
        return _row_to_launch(row) if row else None

    def launches_between(self, start: datetime, end: datetime) -> list[Launch]:
        rows = self.conn.execute(
            "SELECT * FROM launches WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
            (_ts(start), _ts(end)),
        ).fetchall()
        return [_row_to_launch(r) for r in rows]

    def snapshots_as_of(
        self, token_key: str, as_of: datetime, lookback_seconds: float | None = None
    ) -> list[MarketSnapshot]:
        """All snapshots true AND observed at or before `as_of`.

        The `observed_at <= as_of` clause is the anti-look-ahead guarantee.
        """
        t = _ts(as_of)
        params: list[Any] = [token_key, t, t]
        clause = ""
        if lookback_seconds is not None:
            clause = " AND as_of >= ?"
            params.append(t - lookback_seconds)
        rows = self.conn.execute(
            "SELECT * FROM market_snapshots WHERE token_key = ? AND as_of <= ? "
            "AND observed_at <= ?" + clause + " ORDER BY as_of",
            params,
        ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def trades_as_of(
        self, token_key: str, as_of: datetime, lookback_seconds: float | None = None
    ) -> list[Trade]:
        t = _ts(as_of)
        params: list[Any] = [token_key, t, t]
        clause = ""
        if lookback_seconds is not None:
            clause = " AND as_of >= ?"
            params.append(t - lookback_seconds)
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE token_key = ? AND as_of <= ? AND observed_at <= ?"
            + clause
            + " ORDER BY as_of, slot",
            params,
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def holders_as_of(self, token_key: str, as_of: datetime) -> list[HolderRecord]:
        """Holder set from the most recent snapshot visible at `as_of`."""
        t = _ts(as_of)
        row = self.conn.execute(
            "SELECT MAX(as_of) AS m FROM holders WHERE token_key = ? AND as_of <= ? "
            "AND observed_at <= ?",
            (token_key, t, t),
        ).fetchone()
        if row is None or row["m"] is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM holders WHERE token_key = ? AND as_of = ? ORDER BY share_of_supply DESC",
            (token_key, row["m"]),
        ).fetchall()
        return [_row_to_holder(r) for r in rows]

    def security_as_of(self, token_key: str, as_of: datetime) -> SecurityReport | None:
        t = _ts(as_of)
        row = self.conn.execute(
            "SELECT * FROM security_reports WHERE token_key = ? AND as_of <= ? "
            "AND observed_at <= ? ORDER BY as_of DESC LIMIT 1",
            (token_key, t, t),
        ).fetchone()
        return _row_to_security(row) if row else None

    def posts_as_of(
        self, token_key: str, as_of: datetime, lookback_seconds: float | None = None
    ) -> list[SocialPost]:
        """Latest observation of each post visible at `as_of`.

        Posts are stored once per observation so engagement can be tracked over
        time; this collapses to the freshest row per post_id that was already
        visible, which is what a metric computed at `as_of` would have seen.
        """
        t = _ts(as_of)
        params: list[Any] = [token_key, t, t]
        clause = ""
        if lookback_seconds is not None:
            clause = " AND as_of >= ?"
            params.append(t - lookback_seconds)
        rows = self.conn.execute(
            """SELECT * FROM social_posts p
               WHERE token_key = ? AND as_of <= ? AND observed_at <= ?"""
            + clause
            + """ AND observed_at = (
                   SELECT MAX(observed_at) FROM social_posts q
                   WHERE q.platform = p.platform AND q.post_id = p.post_id
                     AND q.observed_at <= ?)
               ORDER BY as_of""",
            [*params, t],
        ).fetchall()
        return [_row_to_post(r) for r in rows]

    def metric_values_as_of(self, token_key: str, as_of: datetime) -> list[MetricValue]:
        t = _ts(as_of)
        rows = self.conn.execute(
            """SELECT * FROM metric_values m
               WHERE token_key = ? AND as_of <= ? AND observed_at <= ?
                 AND as_of = (SELECT MAX(as_of) FROM metric_values n
                              WHERE n.token_key = m.token_key AND n.metric_id = m.metric_id
                                AND n.as_of <= ? AND n.observed_at <= ?)""",
            (token_key, t, t, t, t),
        ).fetchall()
        return [_row_to_metric(r) for r in rows]

    def metric_history(self, metric_id: str, limit: int = 5000) -> list[float]:
        """Raw values of one metric across all tokens, for calibration."""
        rows = self.conn.execute(
            "SELECT raw FROM metric_values WHERE metric_id = ? AND raw IS NOT NULL "
            "ORDER BY as_of DESC LIMIT ?",
            (metric_id, limit),
        ).fetchall()
        return [float(r["raw"]) for r in rows]

    def outcome(self, token_key: str) -> Outcome | None:
        row = self.conn.execute(
            "SELECT * FROM outcomes WHERE token_key = ?", (token_key,)
        ).fetchone()
        return _row_to_outcome(row) if row else None

    def deployer_history(self, deployer: str, before: datetime | None = None) -> dict[str, Any]:
        """Prior launches by this deployer, restricted to before `before`.

        The time restriction matters: using a deployer's full lifetime record in
        a backtest is look-ahead bias, and it is the most common way this class
        of feature silently inflates results.
        """
        params: list[Any] = [deployer]
        clause = ""
        if before is not None:
            clause = " AND l.created_at < ?"
            params.append(_ts(before))
        rows = self.conn.execute(
            """SELECT l.token_key, l.created_at, o.rugged, o.graduated, o.max_multiple_from_t0
               FROM launches l LEFT JOIN outcomes o ON o.token_key = l.token_key
               WHERE l.deployer = ?"""
            + clause,
            params,
        ).fetchall()
        launches = len(rows)
        rugs = sum(1 for r in rows if r["rugged"])
        grads = sum(1 for r in rows if r["graduated"])
        best = max((r["max_multiple_from_t0"] or 0.0) for r in rows) if rows else 0.0
        return {
            "deployer": deployer,
            "launch_count": launches,
            "rug_count": rugs,
            "graduate_count": grads,
            "best_multiple": best,
        }

    def wallet_seen_before(self, wallet: str, before: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE wallet = ? AND as_of < ?",
            (wallet, _ts(before)),
        ).fetchone()
        return int(row["c"]) if row else 0

    def record_run(
        self,
        run_id: str,
        surface: str,
        started_at: datetime,
        finished_at: datetime | None,
        ok: bool,
        records: int,
        error: str | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO collector_runs VALUES (?,?,?,?,?,?,?)",
                (run_id, surface, _ts(started_at), _ts(finished_at), int(ok), records, error),
            )

    def counts(self) -> dict[str, int]:
        tables = [
            "launches",
            "market_snapshots",
            "trades",
            "holders",
            "security_reports",
            "social_posts",
            "metric_values",
            "scores",
            "outcomes",
        ]
        out: dict[str, int] = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
            out[t] = int(row["c"]) if row else 0
        return out

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

    def collection_gaps(
        self,
        tolerance_seconds: float,
        surface: str = "sweep",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Stretches between consecutive heartbeats longer than `tolerance_seconds`.

        This is the read the heartbeat exists for. A gap here is time the
        collector was not running, which is the difference between "the market
        produced nothing" and "the process was dead" — two states that produce
        an identical corpus and opposite conclusions.

        Measured from one run's `finished_at` to the next run's `started_at`, so
        a slow sweep is not itself reported as a gap.
        """
        rows = self.conn.execute(
            """SELECT started_at, finished_at FROM (
                   SELECT started_at, finished_at FROM collector_runs
                   WHERE surface = ? ORDER BY started_at DESC LIMIT ?
               ) ORDER BY started_at ASC""",
            (surface, limit),
        ).fetchall()

        gaps: list[dict[str, Any]] = []
        for previous, following in zip(rows, rows[1:], strict=False):
            ended = previous["finished_at"] or previous["started_at"]
            seconds = float(following["started_at"]) - float(ended)
            if seconds > tolerance_seconds:
                gaps.append(
                    {
                        "after": _dt(ended),
                        "before": _dt(following["started_at"]),
                        "seconds": round(seconds, 1),
                    }
                )
        return gaps

    def observation_latency(self, since: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Per-source time from mint to the first time Botsensai saw the token.

        `source` on a launch row is the collector that *created* it and is never
        overwritten by a later upsert, so grouping by it answers the question the
        websocket work exists to answer: which surface got there first, and by
        how much did it beat the poller.

        Two counts are reported and they are not interchangeable:

        * `launches` — every row this source discovered.
        * `measured` — the subset with `created_at < observed_at`, i.e. those
          whose mint time came from somewhere other than the observation itself.

        The distinction is the whole point. The pumpportal feed carries no mint
        timestamp, so a websocket-discovered token starts life with
        `created_at == observed_at` and a latency of exactly zero — not because
        it was seen instantly, but because nothing yet knows when it was minted.
        Those rows are excluded from the percentiles rather than averaged in as
        zeroes, which would manufacture a spectacular and completely fictional
        latency figure. They join `measured` once the REST path corroborates the
        mint time, which `upsert_launch` folds in with a MIN.

        Percentiles are computed here rather than in SQL because sqlite has no
        median, and the alternative — an average — is meaningless over a
        distribution with a tail this long.
        """
        clause = "WHERE observed_at >= ?" if since is not None else ""
        params: tuple[Any, ...] = (_ts(since),) if since is not None else ()
        rows = self.conn.execute(
            f"SELECT source, created_at, observed_at FROM launches {clause}", params
        ).fetchall()

        buckets: dict[str, list[float]] = {}
        totals: dict[str, int] = {}
        for row in rows:
            source = row["source"] or "unknown"
            totals[source] = totals.get(source, 0) + 1
            seconds = float(row["observed_at"]) - float(row["created_at"])
            if seconds > 0:
                buckets.setdefault(source, []).append(seconds)

        def percentile(values: list[float], fraction: float) -> float:
            index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
            return values[index]

        report: dict[str, dict[str, Any]] = {}
        for source, count in sorted(totals.items(), key=lambda kv: -kv[1]):
            measured = sorted(buckets.get(source, []))
            report[source] = {
                "launches": count,
                "measured": len(measured),
                "median_seconds": round(percentile(measured, 0.5), 2) if measured else None,
                "p90_seconds": round(percentile(measured, 0.9), 2) if measured else None,
                "fastest_seconds": round(measured[0], 2) if measured else None,
            }
        return report


def _b(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _ub(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _token(row: sqlite3.Row, prefix: str = "") -> TokenRef:
    key = row[f"{prefix}token_key"]
    chain, _, mint = key.partition(":")
    return TokenRef(
        chain=Chain(chain),
        mint=mint,
        # sqlite3.Row does not implement __contains__, so .keys() is required here.
        symbol=row["symbol"] if "symbol" in row.keys() else None,  # noqa: SIM118
        name=row["name"] if "name" in row.keys() else None,  # noqa: SIM118
    )


def _row_to_launch(row: sqlite3.Row) -> Launch:
    return Launch(
        token=TokenRef(
            chain=Chain(row["chain"]), mint=row["mint"], symbol=row["symbol"], name=row["name"]
        ),
        launchpad=Launchpad(row["launchpad"]),
        deployer=row["deployer"],
        created_at=_dt(row["created_at"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        description=row["description"],
        image_uri=row["image_uri"],
        metadata_uri=row["metadata_uri"],
        website=row["website"],
        twitter=row["twitter"],
        telegram=row["telegram"],
        initial_supply=row["initial_supply"],
        dev_buy_sol=row["dev_buy_sol"],
        source=row["source"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> MarketSnapshot:
    return MarketSnapshot(
        token=_token(row),
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        stage=CurveStage(row["stage"]) if row["stage"] else CurveStage.BONDING,
        price_usd=row["price_usd"],
        price_native=row["price_native"],
        market_cap_usd=row["market_cap_usd"],
        fdv_usd=row["fdv_usd"],
        liquidity_usd=row["liquidity_usd"],
        volume_5m_usd=row["volume_5m_usd"],
        volume_1h_usd=row["volume_1h_usd"],
        volume_24h_usd=row["volume_24h_usd"],
        txns_5m_buys=row["txns_5m_buys"],
        txns_5m_sells=row["txns_5m_sells"],
        txns_1h_buys=row["txns_1h_buys"],
        txns_1h_sells=row["txns_1h_sells"],
        holder_count=row["holder_count"],
        bonding_curve_progress=row["bonding_curve_progress"],
        pair_address=row["pair_address"],
        dex=row["dex"],
        source=row["source"] or "unknown",
    )


def _row_to_trade(row: sqlite3.Row) -> Trade:
    return Trade(
        token=_token(row),
        signature=row["signature"],
        slot=row["slot"],
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        wallet=row["wallet"],
        side=Side(row["side"]),
        amount_token=row["amount_token"],
        amount_native=row["amount_native"],
        price_usd=row["price_usd"],
        price_native=row["price_native"],
        priority_fee_lamports=row["priority_fee_lamports"],
        jito_tip_lamports=row["jito_tip_lamports"],
        is_bundled=_ub(row["is_bundled"]),
        bundle_id=row["bundle_id"],
        source=row["source"] or "unknown",
    )


def _row_to_holder(row: sqlite3.Row) -> HolderRecord:
    return HolderRecord(
        token=_token(row),
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        wallet=row["wallet"],
        balance=row["balance"],
        share_of_supply=row["share_of_supply"],
        first_seen_at=_dt(row["first_seen_at"]),
        wallet_age_seconds=row["wallet_age_seconds"],
        funded_by=row["funded_by"],
        labels=_unjson(row["labels"]),
    )


def _row_to_security(row: sqlite3.Row) -> SecurityReport:
    return SecurityReport(
        token=_token(row),
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        mint_authority_revoked=_ub(row["mint_authority_revoked"]),
        freeze_authority_revoked=_ub(row["freeze_authority_revoked"]),
        lp_burned_share=row["lp_burned_share"],
        lp_locked_until=_dt(row["lp_locked_until"]),
        transfer_fee_bps=row["transfer_fee_bps"],
        is_mutable_metadata=_ub(row["is_mutable_metadata"]),
        top10_share=row["top10_share"],
        insider_share=row["insider_share"],
        bundled_share=row["bundled_share"],
        sniper_share=row["sniper_share"],
        dev_sold=_ub(row["dev_sold"]),
        dev_holding_share=row["dev_holding_share"],
        source=row["source"] or "unknown",
    )


def _row_to_post(row: sqlite3.Row) -> SocialPost:
    return SocialPost(
        platform=Platform(row["platform"]),
        post_id=row["post_id"],
        token_key=row["token_key"],
        author=row["author"],
        author_id=row["author_id"],
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        text=row["text"] or "",
        lang=row["lang"],
        url=row["url"],
        parent_id=row["parent_id"],
        is_repost=bool(row["is_repost"]),
        quoted_id=row["quoted_id"],
        likes=row["likes"],
        replies=row["replies"],
        reposts=row["reposts"],
        quotes=row["quotes"],
        bookmarks=row["bookmarks"],
        views=row["views"],
        media_urls=_unjson(row["media_urls"]),
        media_hashes=_unjson(row["media_hashes"]),
        author_followers=row["author_followers"],
        author_created_at=_dt(row["author_created_at"]),
        mentioned_tokens=_unjson(row["mentioned_tokens"]),
        source=row["source"] or "unknown",
    )


def _row_to_metric(row: sqlite3.Row) -> MetricValue:
    return MetricValue(
        metric_id=row["metric_id"],
        token=_token(row),
        as_of=_dt(row["as_of"]),  # type: ignore[arg-type]
        observed_at=_dt(row["observed_at"]),  # type: ignore[arg-type]
        raw=row["raw"],
        normalized=row["normalized"],
        confidence=Confidence(row["confidence"]),
        inputs_used=_unjson(row["inputs_used"]),
        notes=row["notes"],
    )


def _row_to_outcome(row: sqlite3.Row) -> Outcome:
    return Outcome(
        token=_token(row),
        labeled_at=_dt(row["labeled_at"]) or utcnow(),
        graduated=bool(row["graduated"]),
        graduated_at=_dt(row["graduated_at"]),
        rugged=bool(row["rugged"]),
        peak_market_cap_usd=row["peak_market_cap_usd"],
        peak_at=_dt(row["peak_at"]),
        max_multiple_from_t0=row["max_multiple_from_t0"],
        time_to_peak_seconds=row["time_to_peak_seconds"],
        survived_1h=bool(row["survived_1h"]),
        survived_24h=bool(row["survived_24h"]),
        survived_7d=bool(row["survived_7d"]),
        final_market_cap_usd=row["final_market_cap_usd"],
        max_realizable_multiple=row["max_realizable_multiple"],
    )


__all__ = ["Database", "SCHEMA", "SCHEMA_VERSION"]
