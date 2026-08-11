"""Copytrade wallet skill scoring based strictly on point-in-time trade history.

Populates `ctx.extra["wallet_skill"]` and `ctx.extra["wallet_typical_size"]` used by
`SmartWalletParticipation`.

Points of design:
- Realized PnL: Evaluates closed positions (tokens bought and sold prior to `before`).
- Entry Earliness: Evaluates how early the wallet entered relative to launch time for tokens
  that subsequently ran (e.g. max realized multiple >= 1.5).
- Point-in-time knowledge bounds: Bounded on both event time (`as_of < before`) AND knowledge
  time (`observed_at <= observed_before`). Trades closed AFTER `before` or observed AFTER
  `observed_before` are strictly excluded to eliminate look-ahead bias.
- Caching: LRU cache keyed on `(wallet, before_timestamp, observed_before_timestamp)` with
  `observed_before` floored to 60-second buckets for hit-rate optimization.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

DEFAULT_CAPACITY = 50_000
DEFAULT_OBSERVED_BUCKET_SECONDS = 60.0


@dataclass
class WalletSkillResult:
    scores: dict[str, float]
    typical_sizes: dict[str, float]


def _calculate_trade_metrics(token_trades: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    """Extract closed trade performance and typical buy size for one token position."""
    buys = [t for t in token_trades if str(t["side"]).lower() == "buy"]
    sells = [t for t in token_trades if str(t["side"]).lower() == "sell"]
    buy_native = sum(float(t["amount_native"]) for t in buys)
    if not buys:
        return None, 0.0

    if not sells:
        return None, buy_native

    sell_native = sum(float(t["amount_native"]) for t in sells)
    realized_mult = sell_native / max(1e-9, buy_native)
    first_buy = min(float(t["as_of"]) for t in buys)
    launch_time = next((float(t["launch_created_at"]) for t in buys if t["launch_created_at"] is not None), None)
    if launch_time is None:
        launch_time = min(float(t["as_of"]) for t in token_trades)

    earliness_secs = max(0.0, first_buy - launch_time)
    earliness = max(0.0, 1.0 - earliness_secs / 3600.0)

    closed_info = {
        "realized_mult": realized_mult,
        "is_win": realized_mult > 1.0,
        "earliness": earliness,
    }
    return closed_info, buy_native


def _compute_single_wallet_skill(trades: list[dict[str, Any]]) -> tuple[float, float]:
    """Compute (skill_score, typical_size) for one wallet from its prior trades."""
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_token[str(t["token_key"])].append(t)

    closed_trades: list[dict[str, Any]] = []
    buy_sizes: list[float] = []

    for _token_key, token_trades in by_token.items():
        closed_info, buy_native = _calculate_trade_metrics(token_trades)
        if buy_native > 0:
            buy_sizes.append(buy_native)
        if closed_info is not None:
            closed_trades.append(closed_info)

    typical_size = float(median(buy_sizes)) if buy_sizes else 0.0

    if not closed_trades:
        return 0.5, typical_size

    n = len(closed_trades)
    wins = sum(1.0 for c in closed_trades if c["is_win"])
    win_rate = wins / n

    mult_scores = [min(1.0, max(0.0, (float(c["realized_mult"]) - 1.0) / 2.0 + 0.5)) for c in closed_trades]
    mean_pnl_score = sum(mult_scores) / n
    base_pnl = 0.5 * win_rate + 0.5 * mean_pnl_score

    runners = [float(c["earliness"]) for c in closed_trades if float(c["realized_mult"]) >= 1.5]
    if runners:
        mean_earliness = sum(runners) / len(runners)
        raw_skill = 0.6 * base_pnl + 0.4 * mean_earliness
    else:
        raw_skill = base_pnl

    confidence = n / (n + 3.0)
    skill = 0.5 + confidence * (raw_skill - 0.5)
    return min(1.0, max(0.0, skill)), typical_size


class WalletSkillIndex:
    """Time-restricted wallet skill scores and typical sizes, batched and memoized."""

    def __init__(
        self,
        db: Database,
        capacity: int = DEFAULT_CAPACITY,
        observed_bucket_seconds: float = DEFAULT_OBSERVED_BUCKET_SECONDS,
    ) -> None:
        self.db = db
        self.capacity = max(1, capacity)
        self.observed_bucket_seconds = max(0.0, observed_bucket_seconds)
        self._cache: OrderedDict[tuple[str, float, float | None], tuple[float, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def skills_for(
        self,
        wallets: Iterable[str],
        before: datetime,
        observed_before: datetime | None = None,
    ) -> WalletSkillResult:
        """Point-in-time wallet skills and typical sizes for requested wallets."""
        unique = list(dict.fromkeys(w for w in wallets if w))
        if not unique:
            return WalletSkillResult(scores={}, typical_sizes={})

        before_key = before.timestamp()
        observed_before = self._floor_observed(observed_before)
        observed_key = observed_before.timestamp() if observed_before is not None else None

        scores: dict[str, float] = {}
        typical_sizes: dict[str, float] = {}
        pending: list[str] = []

        for wallet in unique:
            cached = self._get((wallet, before_key, observed_key))
            if cached is None:
                pending.append(wallet)
            else:
                scores[wallet], typical_sizes[wallet] = cached
                self.hits += 1

        if pending:
            self.misses += len(pending)
            resolved = self._resolve(pending, before, observed_before)
            for wallet, (skill, size) in resolved.items():
                self._put((wallet, before_key, observed_key), (skill, size))
                scores[wallet] = skill
                typical_sizes[wallet] = size

        return WalletSkillResult(scores=scores, typical_sizes=typical_sizes)

    def skill_for(
        self, wallet: str, before: datetime, observed_before: datetime | None = None
    ) -> tuple[float, float]:
        res = self.skills_for([wallet], before, observed_before)
        return res.scores.get(wallet, 0.5), res.typical_sizes.get(wallet, 0.0)

    def _floor_observed(self, observed_before: datetime | None) -> datetime | None:
        if observed_before is None or not self.observed_bucket_seconds:
            return observed_before
        bucket = self.observed_bucket_seconds
        stamp = observed_before.timestamp()
        floored = stamp - (stamp % bucket)
        if floored == stamp:
            return observed_before
        return datetime.fromtimestamp(floored, tz=observed_before.tzinfo or UTC)

    def _resolve(
        self, wallets: Sequence[str], before: datetime, observed_before: datetime | None
    ) -> dict[str, tuple[float, float]]:
        all_trades = self.db.wallet_trades_before(wallets, before, observed_before)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in all_trades:
            grouped[str(t["wallet"])].append(t)

        out: dict[str, tuple[float, float]] = {}
        for wallet in wallets:
            w_trades = grouped.get(wallet, [])
            out[wallet] = _compute_single_wallet_skill(w_trades)
        return out

    def _get(self, key: tuple[str, float, float | None]) -> tuple[float, float] | None:
        value = self._cache.get(key)
        if value is None:
            return None
        self._cache.move_to_end(key)
        return value

    def _put(self, key: tuple[str, float, float | None], value: tuple[float, float]) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        self._cache.clear()


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_OBSERVED_BUCKET_SECONDS",
    "WalletSkillIndex",
    "WalletSkillResult",
]
