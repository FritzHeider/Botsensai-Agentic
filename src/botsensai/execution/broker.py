"""Paper broker and risk enforcement.

The broker owns three responsibilities that must not live anywhere else:

* **Risk limits are enforced here, not advised here.** Position caps, exposure
  caps, daily loss limits and rate limits are checked on every order and reject
  it outright. A limit implemented as a warning in the strategy layer is not a
  limit.
* **Exit management is mechanical.** Take-profit ladders, stop losses, trailing
  stops and time-based exits run on a fixed schedule rather than being decided
  in the moment, because the moment is exactly when judgement is worst.
* **Live execution is structurally absent.** `LiveBroker` exists as an explicit
  stub that raises. There is no code path in this repository that can sign a
  transaction, and adding one is a deliberate act rather than a config flip.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from botsensai.config import RiskSettings, Settings, TradingMode, get_settings
from botsensai.execution.fills import CurveState, FillContext, FillSimulator
from botsensai.models import (
    Fill,
    MarketSnapshot,
    Order,
    Position,
    Side,
    TokenRef,
)
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    adjusted_size: float | None = None


@dataclass
class AccountState:
    """Cash, positions and the counters the risk limits are enforced against."""

    cash_native: float = 10.0
    starting_native: float = 10.0
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[Position] = field(default_factory=list)
    realized_pnl_native: float = 0.0
    fees_paid_native: float = 0.0
    trade_timestamps: list[datetime] = field(default_factory=list)
    daily_loss_native: float = 0.0
    day_anchor: datetime | None = None
    rejected_orders: int = 0

    @property
    def open_exposure_native(self) -> float:
        return sum(p.cost_basis_native for p in self.positions.values() if p.is_open)

    @property
    def equity_native(self) -> float:
        marked = sum(
            p.amount_token * p.last_price_native for p in self.positions.values() if p.is_open
        )
        return self.cash_native + marked

    @property
    def total_return(self) -> float:
        if self.starting_native <= 0:
            return 0.0
        return (self.equity_native / self.starting_native) - 1.0


class RiskManager:
    """Pre-trade checks. Every one of these can veto an order."""

    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()

    def check_entry(
        self, order: Order, account: AccountState, snapshot: MarketSnapshot | None, age_seconds: float
    ) -> RiskDecision:
        s = self.settings

        if s.kill_switch:
            return RiskDecision(False, "kill switch engaged")

        if order.size_native <= 0:
            return RiskDecision(False, "non-positive size")

        key = order.token.key
        if key in account.positions and account.positions[key].is_open:
            return RiskDecision(False, "already holding this token")

        open_count = sum(1 for p in account.positions.values() if p.is_open)
        if open_count >= s.max_concurrent_positions:
            return RiskDecision(False, f"at position limit ({s.max_concurrent_positions})")

        size, sizing_veto = self._size_within_limits(order, account)
        if sizing_veto is not None:
            return RiskDecision(False, sizing_veto)

        if account.daily_loss_native >= s.max_daily_loss_native:
            return RiskDecision(
                False, f"daily loss limit hit ({account.daily_loss_native:.3f} native)"
            )

        cutoff = order.as_of - timedelta(hours=1)
        recent = [t for t in account.trade_timestamps if t >= cutoff]
        if len(recent) >= s.max_trades_per_hour:
            return RiskDecision(False, f"trade rate limit ({s.max_trades_per_hour}/hr)")

        token_veto = self._token_veto(snapshot, age_seconds)
        if token_veto is not None:
            return RiskDecision(False, token_veto)

        return RiskDecision(True, "ok", adjusted_size=size)

    def _size_within_limits(
        self, order: Order, account: AccountState
    ) -> tuple[float, str | None]:
        """Clamp the requested size to the position, portfolio and cash limits.

        Returns the clamped size and, if one of those limits leaves nothing to
        trade, the reason to veto on.
        """
        s = self.settings
        size = min(order.size_native, s.max_position_native)

        headroom = s.max_portfolio_exposure_native - account.open_exposure_native
        if headroom <= 0:
            return size, "portfolio exposure limit reached"
        size = min(size, headroom)

        if size > account.cash_native:
            size = account.cash_native
        if size <= 1e-6:
            return size, "insufficient cash after limits"
        return size, None

    def _token_veto(self, snapshot: MarketSnapshot | None, age_seconds: float) -> str | None:
        """Age and liquidity gates on the token itself, or None if it passes."""
        s = self.settings
        if age_seconds < s.min_token_age_seconds:
            return f"token too young ({age_seconds:.0f}s)"
        if age_seconds > s.max_token_age_seconds:
            return f"token too old ({age_seconds / 3600:.1f}h)"

        if snapshot is not None and snapshot.liquidity_usd is not None:
            if snapshot.liquidity_usd < s.min_liquidity_usd:
                return f"liquidity ${snapshot.liquidity_usd:,.0f} below floor"
        return None


class PaperBroker:
    """Simulated execution against the same fill model the backtester uses."""

    def __init__(
        self,
        settings: Settings | None = None,
        starting_native: float = 10.0,
        seed: int = 1337,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = RiskManager(self.settings.risk)
        self.simulator = FillSimulator(self.settings.execution, seed=seed)
        self.account = AccountState(cash_native=starting_native, starting_native=starting_native)
        self.fills: list[Fill] = []

    # -- helpers ------------------------------------------------------------ #

    def _roll_day(self, now: datetime) -> None:
        if self.account.day_anchor is None:
            self.account.day_anchor = now
            return
        if (now - self.account.day_anchor).total_seconds() >= 86400:
            self.account.day_anchor = now
            self.account.daily_loss_native = 0.0

    def mark(self, token: TokenRef, price_native: float) -> None:
        """Update the mark on an open position. Drives trailing stops."""
        pos = self.account.positions.get(token.key)
        if pos is None or not pos.is_open:
            return
        pos.last_price_native = price_native
        if price_native > pos.peak_price_native:
            pos.peak_price_native = price_native

    # -- entry -------------------------------------------------------------- #

    def open_position(
        self,
        token: TokenRef,
        size_native: float,
        snapshot: MarketSnapshot,
        as_of: datetime,
        age_seconds: float,
        reason: str = "",
        score: float | None = None,
        contention: float = 0.0,
        future_price_native: float | None = None,
        recent_volatility: float = 0.0,
    ) -> Fill | None:
        self._roll_day(as_of)

        order = Order(
            token=token,
            as_of=as_of,
            side=Side.BUY,
            size_native=size_native,
            max_slippage_bps=self.settings.risk.max_slippage_bps,
            priority_fee_lamports=self.settings.execution.priority_fee_lamports,
            jito_tip_lamports=self.settings.execution.jito_tip_lamports,
            reason=reason,
            score_at_entry=score,
            client_id=uuid.uuid4().hex[:12],
        )

        decision = self.risk.check_entry(order, self.account, snapshot, age_seconds)
        if not decision.allowed:
            self.account.rejected_orders += 1
            log.debug("broker.entry_rejected", token=token.key, reason=decision.reason)
            return None
        order.size_native = decision.adjusted_size or order.size_native

        ctx = FillContext(
            snapshot=snapshot,
            curve=CurveState.from_snapshot(snapshot),
            future_price_native=future_price_native,
            recent_volatility=recent_volatility,
            contention=contention,
        )
        fill = self.simulator.simulate(order, ctx)
        self.fills.append(fill)

        # A rejected fill still costs fees; charge them and move on.
        self.account.cash_native -= fill.fee_native + fill.tip_native
        self.account.fees_paid_native += fill.fee_native + fill.tip_native

        if fill.rejected or fill.amount_token <= 0:
            return fill

        self.account.cash_native -= fill.amount_native
        self.account.trade_timestamps.append(as_of)

        position = Position(
            token=token,
            opened_at=fill.as_of,
            amount_token=fill.amount_token,
            cost_basis_native=fill.amount_native + fill.fee_native + fill.tip_native,
            peak_price_native=fill.price_native,
            last_price_native=fill.price_native,
            fills=[fill],
        )
        self.account.positions[token.key] = position
        log.info(
            "broker.opened",
            token=token.key,
            size=round(fill.amount_native, 4),
            price=fill.price_native,
            slippage_bps=round(fill.slippage_bps, 1),
            reason=reason,
        )
        return fill

    # -- exit --------------------------------------------------------------- #

    def close_position(
        self,
        token: TokenRef,
        snapshot: MarketSnapshot,
        as_of: datetime,
        fraction: float = 1.0,
        reason: str = "",
        contention: float = 0.0,
    ) -> Fill | None:
        position = self.account.positions.get(token.key)
        if position is None or not position.is_open:
            return None

        tokens_to_sell = position.amount_token * max(0.0, min(1.0, fraction))
        if tokens_to_sell <= 0:
            return None

        order = Order(
            token=token,
            as_of=as_of,
            side=Side.SELL,
            size_native=tokens_to_sell,
            max_slippage_bps=max(self.settings.risk.max_slippage_bps, 2_000),
            priority_fee_lamports=self.settings.execution.priority_fee_lamports,
            jito_tip_lamports=0,
            reason=reason,
            client_id=uuid.uuid4().hex[:12],
        )

        ctx = FillContext(
            snapshot=snapshot,
            curve=CurveState.from_snapshot(snapshot),
            contention=contention,
        )
        fill = self.simulator.simulate(order, ctx)
        self.fills.append(fill)

        self.account.cash_native -= fill.fee_native + fill.tip_native
        self.account.fees_paid_native += fill.fee_native + fill.tip_native

        if fill.rejected or fill.amount_native <= 0:
            log.warning("broker.exit_failed", token=token.key, reason=fill.reject_reason)
            return fill

        self.account.cash_native += fill.amount_native
        position.fills.append(fill)

        cost_released = position.cost_basis_native * (tokens_to_sell / max(1e-18, position.amount_token))
        pnl = fill.amount_native - cost_released
        position.realized_pnl_native += pnl
        position.amount_token -= fill.amount_token
        position.cost_basis_native -= cost_released
        position.last_price_native = fill.price_native
        self.account.realized_pnl_native += pnl
        if pnl < 0:
            self.account.daily_loss_native += -pnl

        if position.amount_token <= 1e-9 or fraction >= 1.0:
            position.closed_at = fill.as_of
            position.amount_token = 0.0
            position.exit_reason = reason
            self.account.closed.append(position)
            self.account.positions.pop(token.key, None)

        log.info(
            "broker.closed" if position.closed_at else "broker.trimmed",
            token=token.key,
            pnl=round(pnl, 4),
            reason=reason,
        )
        return fill

    # -- exit policy -------------------------------------------------------- #

    def exit_signals(self, token: TokenRef, as_of: datetime) -> list[tuple[float, str]]:
        """Mechanical exit rules. Returns (fraction_to_sell, reason) pairs.

        Evaluated in priority order: hard stops first, then the profit ladder,
        then time. Returning fractions rather than a boolean is what lets the
        ladder scale out rather than making one all-or-nothing decision.
        """
        position = self.account.positions.get(token.key)
        if position is None or not position.is_open:
            return []

        s = self.settings.risk
        entry_price = position.cost_basis_native / max(1e-18, position.amount_token)
        current = position.last_price_native
        if entry_price <= 0 or current <= 0:
            return []

        multiple = current / entry_price
        signals: list[tuple[float, str]] = []

        # Hard stop.
        if multiple <= (1.0 - s.stop_loss_pct):
            return [(1.0, f"stop loss at {multiple:.2f}x")]

        # Trailing stop, armed only once the position has been profitable.
        if position.peak_price_native > entry_price:
            drawdown = 1.0 - (current / max(1e-18, position.peak_price_native))
            if drawdown >= s.trailing_stop_pct:
                return [(1.0, f"trailing stop, {drawdown:.0%} off peak")]

        # Time stop.
        held = (as_of - position.opened_at).total_seconds()
        if held >= s.max_hold_seconds:
            return [(1.0, f"max hold {held / 3600:.1f}h reached")]

        # Profit ladder. Each rung fires once, tracked by how much has already
        # been sold relative to the original size.
        original_tokens = sum(
            f.amount_token for f in position.fills if f.side is Side.BUY and not f.rejected
        )
        sold_tokens = sum(
            f.amount_token for f in position.fills if f.side is Side.SELL and not f.rejected
        )
        sold_fraction = sold_tokens / max(1e-18, original_tokens)

        cumulative = 0.0
        for target, fraction in zip(s.take_profit_multiples, s.take_profit_fractions, strict=True):
            cumulative += fraction
            if multiple >= target and sold_fraction < cumulative - 1e-6:
                remaining_of_original = max(0.0, cumulative - sold_fraction)
                current_fraction = min(
                    1.0, remaining_of_original * original_tokens / max(1e-18, position.amount_token)
                )
                if current_fraction > 1e-6:
                    signals.append((current_fraction, f"take profit at {target:.1f}x"))
                break

        return signals

    def apply_exits(
        self, token: TokenRef, snapshot: MarketSnapshot, as_of: datetime
    ) -> list[Fill]:
        """Run the exit policy and execute whatever it produces."""
        out: list[Fill] = []
        for fraction, reason in self.exit_signals(token, as_of):
            fill = self.close_position(token, snapshot, as_of, fraction=fraction, reason=reason)
            if fill is not None:
                out.append(fill)
        return out

    def close_all(self, snapshots: dict[str, MarketSnapshot], as_of: datetime) -> list[Fill]:
        """Liquidate everything. Used at the end of a backtest window."""
        out: list[Fill] = []
        for key in list(self.account.positions.keys()):
            position = self.account.positions[key]
            snapshot = snapshots.get(key)
            if snapshot is None:
                continue
            fill = self.close_position(
                position.token, snapshot, as_of, fraction=1.0, reason="end of window"
            )
            if fill is not None:
                out.append(fill)
        return out

    # -- reporting ---------------------------------------------------------- #

    def summary(self) -> dict[str, float | int]:
        closed = self.account.closed
        wins = [p for p in closed if p.realized_pnl_native > 0]
        return {
            "equity_native": round(self.account.equity_native, 6),
            "cash_native": round(self.account.cash_native, 6),
            "realized_pnl_native": round(self.account.realized_pnl_native, 6),
            "fees_paid_native": round(self.account.fees_paid_native, 6),
            "total_return": round(self.account.total_return, 6),
            "open_positions": sum(1 for p in self.account.positions.values() if p.is_open),
            "closed_positions": len(closed),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "rejected_orders": self.account.rejected_orders,
            "fills": len(self.fills),
            "failed_fills": sum(1 for f in self.fills if f.rejected),
        }


class LiveBroker:
    """Deliberately non-functional live adapter.

    This class exists to document the boundary rather than to cross it. There is
    no signing code, no key handling and no RPC submission anywhere in this
    repository. Anyone wiring real execution has to write it themselves, which
    means they have to think about key custody, MEV protection and failure
    handling on purpose rather than by inheriting a default.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.trading_mode is TradingMode.LIVE:
            raise NotImplementedError(
                "Live execution is not implemented in this repository by design.\n"
                "Botsensai ships backtest and paper modes only. To trade real funds you "
                "must implement signing and submission yourself, and you should not do so "
                "until a walk-forward backtest and an extended paper run both show positive "
                "expectancy net of fees, slippage and failed transactions."
            )

    def open_position(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("live trading is not available")

    def close_position(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("live trading is not available")


def build_broker(settings: Settings | None = None, starting_native: float = 10.0) -> PaperBroker:
    """Return the broker appropriate to the configured mode.

    In live mode this raises rather than returning something that trades, which
    is the intended behaviour: the failure is loud and happens at startup.
    """
    resolved = settings or get_settings()
    if resolved.trading_mode is TradingMode.LIVE:
        LiveBroker(resolved)  # raises
    return PaperBroker(resolved, starting_native=starting_native)


__all__ = [
    "AccountState",
    "LiveBroker",
    "PaperBroker",
    "RiskDecision",
    "RiskManager",
    "build_broker",
]
