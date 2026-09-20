"""Top Trader Discovery, Tracking, and Copy-Trading Signal Engine.

Continuously identifies the most consistent on-chain Solana traders by win rate,
realized PnL multiple, and early runner entry timing, generating high-conviction
copy-trading signals.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import sqlite3
import time
from typing import Any

from botsensai.models import Side, Trade, utcnow
from botsensai.onchain.wallet_skill import _calculate_trade_metrics, _compute_single_wallet_skill
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class TopTraderProfile:
    wallet: str
    skill_score: float
    win_rate: float
    mean_multiple: float
    typical_size_sol: float
    total_trades: int
    winning_trades: int
    runner_count: int
    last_trade_at: float | None
    status: str = "ACTIVE"
    updated_at: float = 0.0


@dataclass
class CopyTradeAlert:
    token_key: str
    symbol: str | None
    mint: str
    trader_wallet: str
    trader_skill: float
    trader_win_rate: float
    trader_buy_sol: float
    entry_delay_seconds: float
    conviction_boost: float
    recommended_size_sol: float
    detected_at: float


class CopyTradeTracker:
    """Discovers, tracks, and executes copy-trading alpha from elite Solana traders."""

    def __init__(
        self,
        db: Database,
        min_skill: float = 0.70,
        min_win_rate: float = 0.50,
        min_trades: int = 5,
    ) -> None:
        self.db = db
        self.min_skill = min_skill
        self.min_win_rate = min_win_rate
        self.min_trades = min_trades
        self._init_tables()

    def _init_tables(self) -> None:
        """Ensure copytrade tables exist in SQLite."""
        with self.db.tx() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS top_traders (
                    wallet TEXT PRIMARY KEY,
                    skill_score REAL NOT NULL,
                    win_rate REAL NOT NULL,
                    mean_multiple REAL NOT NULL,
                    typical_size_sol REAL NOT NULL,
                    total_trades INTEGER NOT NULL,
                    winning_trades INTEGER NOT NULL,
                    runner_count INTEGER NOT NULL,
                    last_trade_at REAL,
                    status TEXT DEFAULT 'ACTIVE',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_top_traders_skill
                ON top_traders(skill_score DESC, win_rate DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_key TEXT NOT NULL,
                    symbol TEXT,
                    mint TEXT NOT NULL,
                    trader_wallet TEXT NOT NULL,
                    trader_skill REAL NOT NULL,
                    trader_win_rate REAL NOT NULL,
                    trader_buy_sol REAL NOT NULL,
                    entry_delay_seconds REAL NOT NULL,
                    conviction_boost REAL NOT NULL,
                    recommended_size_sol REAL NOT NULL,
                    signal_id INTEGER,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_copy_trade_events_token
                ON copy_trade_events(token_key, created_at)
                """
            )

    def refresh_top_traders(self, max_candidates: int = 150) -> list[TopTraderProfile]:
        """Scan historical on-chain trades to score and update the top trader roster."""
        now = time.time()
        with self.db.conn as conn:
            conn.row_factory = sqlite3.Row
            # Fast selection from precomputed wallet_profiles
            rows = conn.execute(
                """
                SELECT wallet, trade_count as cnt
                FROM wallet_profiles
                WHERE trade_count >= ?
                ORDER BY trade_count DESC
                LIMIT ?
                """,
                (self.min_trades, max_candidates),
            ).fetchall()

            # Fallback to direct trades table scan if wallet_profiles has insufficient entries
            if len(rows) < self.min_trades:
                rows = conn.execute(
                    """
                    SELECT wallet, count(*) as cnt
                    FROM trades
                    WHERE wallet IS NOT NULL AND wallet != ''
                    GROUP BY wallet
                    HAVING cnt >= ?
                    ORDER BY cnt DESC
                    LIMIT ?
                    """,
                    (self.min_trades, max_candidates),
                ).fetchall()

        profiles: list[TopTraderProfile] = []

        for row in rows:
            wallet = row["wallet"]
            trade_rows = self.db.conn.execute(
                """
                SELECT t.token_key, t.side, t.amount_native, t.amount_token, t.price_native, t.as_of,
                       l.created_at as launch_created_at
                FROM trades t
                LEFT JOIN launches l ON t.token_key = l.token_key
                WHERE t.wallet = ?
                ORDER BY t.as_of ASC
                """,
                (wallet,),
            ).fetchall()

            trade_dicts = [dict(r) for r in trade_rows]
            if len(trade_dicts) < self.min_trades:
                continue

            skill, typical_size = _compute_single_wallet_skill(trade_dicts)
            if skill < self.min_skill:
                continue

            # Calculate detailed performance statistics
            by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for t in trade_dicts:
                by_token[str(t["token_key"])].append(t)

            closed_trades = []
            for _tok, t_list in by_token.items():
                c_info, _ = _calculate_trade_metrics(t_list)
                if c_info is not None:
                    closed_trades.append(c_info)

            total_closed = len(closed_trades)
            if total_closed == 0:
                continue

            wins = sum(1 for c in closed_trades if c["is_win"])
            win_rate = wins / total_closed
            if win_rate < self.min_win_rate:
                continue

            mean_mult = sum(c["realized_mult"] for c in closed_trades) / total_closed
            runners = sum(1 for c in closed_trades if c["realized_mult"] >= 1.5)
            last_trade_as_of = max(float(t.get("as_of", 0.0)) for t in trade_dicts)

            profile = TopTraderProfile(
                wallet=wallet,
                skill_score=round(skill, 4),
                win_rate=round(win_rate, 4),
                mean_multiple=round(mean_mult, 3),
                typical_size_sol=round(typical_size, 4),
                total_trades=len(trade_dicts),
                winning_trades=wins,
                runner_count=runners,
                last_trade_at=last_trade_as_of,
                status="ACTIVE",
                updated_at=now,
            )
            profiles.append(profile)

        # Upsert top profiles into database
        with self.db.tx() as conn:
            for p in profiles:
                conn.execute(
                    """
                    INSERT INTO top_traders (
                        wallet, skill_score, win_rate, mean_multiple, typical_size_sol,
                        total_trades, winning_trades, runner_count, last_trade_at, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet) DO UPDATE SET
                        skill_score = excluded.skill_score,
                        win_rate = excluded.win_rate,
                        mean_multiple = excluded.mean_multiple,
                        typical_size_sol = excluded.typical_size_sol,
                        total_trades = excluded.total_trades,
                        winning_trades = excluded.winning_trades,
                        runner_count = excluded.runner_count,
                        last_trade_at = excluded.last_trade_at,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        p.wallet,
                        p.skill_score,
                        p.win_rate,
                        p.mean_multiple,
                        p.typical_size_sol,
                        p.total_trades,
                        p.winning_trades,
                        p.runner_count,
                        p.last_trade_at,
                        p.status,
                        p.updated_at,
                    ),
                )

        profiles.sort(key=lambda x: (x.skill_score, x.win_rate), reverse=True)
        log.info(
            "copytrade.refreshed",
            total_ranked=len(profiles),
            top_skill=profiles[0].skill_score if profiles else 0.0,
        )
        return profiles

    def get_top_traders(self, limit: int = 30) -> list[dict[str, Any]]:
        """Retrieve active top traders sorted by skill score and win rate."""
        with self.db.conn as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT wallet, skill_score, win_rate, mean_multiple, typical_size_sol,
                       total_trades, winning_trades, runner_count, last_trade_at, status, updated_at
                FROM top_traders
                WHERE status = 'ACTIVE'
                ORDER BY skill_score DESC, win_rate DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def check_copy_trade(
        self,
        token_key: str,
        symbol: str | None,
        mint: str,
        launch_created_at: float,
        trades: list[Trade | dict[str, Any]],
        max_entry_delay_seconds: float = 300.0,
    ) -> list[CopyTradeAlert]:
        """Check if any followed top trader has made an early conviction buy in this token."""
        now = time.time()
        top_traders_map = {t["wallet"]: t for t in self.get_top_traders(limit=100)}
        if not top_traders_map:
            return []

        alerts: list[CopyTradeAlert] = []

        for tr in trades:
            # Handle Trade dataclass or dict
            if isinstance(tr, Trade):
                w = tr.wallet
                side = tr.side
                amount_native = tr.amount_native
                as_of_ts = tr.as_of.timestamp() if hasattr(tr.as_of, "timestamp") else float(tr.as_of)
            else:
                w = tr.get("wallet")
                side = tr.get("side")
                amount_native = float(tr.get("amount_native", 0.0))
                as_of_raw = tr.get("as_of", now)
                as_of_ts = as_of_raw.timestamp() if hasattr(as_of_raw, "timestamp") else float(as_of_raw)

            if not w or side != Side.BUY and str(side).lower() != "buy":
                continue

            if w in top_traders_map:
                trader = top_traders_map[w]
                delay = max(0.0, as_of_ts - launch_created_at)
                if delay <= max_entry_delay_seconds:
                    typical = trader["typical_size_sol"] or 1.0
                    conviction = min(2.0, max(0.5, amount_native / max(0.01, typical)))
                    skill = trader["skill_score"]
                    win_rate = trader["win_rate"]

                    # Boost calculation: skill + win_rate + conviction scaling
                    boost = round(0.10 + (skill - 0.5) * 0.4 * conviction, 4)
                    recommended_size = round(min(0.05, max(0.01, 0.02 * conviction * skill)), 4)

                    alert = CopyTradeAlert(
                        token_key=token_key,
                        symbol=symbol,
                        mint=mint,
                        trader_wallet=w,
                        trader_skill=skill,
                        trader_win_rate=win_rate,
                        trader_buy_sol=round(amount_native, 4),
                        entry_delay_seconds=round(delay, 1),
                        conviction_boost=boost,
                        recommended_size_sol=recommended_size,
                        detected_at=now,
                    )
                    alerts.append(alert)

        return alerts

    def record_copy_event(self, alert: CopyTradeAlert, signal_id: int | None = None) -> int:
        """Persist copy trade event in SQLite."""
        with self.db.tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO copy_trade_events (
                    token_key, symbol, mint, trader_wallet, trader_skill,
                    trader_win_rate, trader_buy_sol, entry_delay_seconds,
                    conviction_boost, recommended_size_sol, signal_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.token_key,
                    alert.symbol,
                    alert.mint,
                    alert.trader_wallet,
                    alert.trader_skill,
                    alert.trader_win_rate,
                    alert.trader_buy_sol,
                    alert.entry_delay_seconds,
                    alert.conviction_boost,
                    alert.recommended_size_sol,
                    signal_id,
                    alert.detected_at,
                ),
            )
            return cur.lastrowid or 0
