"""Track record publisher for paper trading.

Publishes a rolling performance page: every entry, its score, its outcome,
and cumulative expectancy with a bootstrap confidence interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from botsensai.backtest.engine import BacktestResult, TradeRecord

from botsensai.config import Settings, get_settings
from botsensai.models import utcnow
from botsensai.util.logging import get_logger

log = get_logger(__name__)


def _ci_callout(n_trades: int, low_ci: float, high_ci: float) -> list[str]:
    if n_trades < 5:
        return [
            "> [!WARNING]",
            (
                f"> Sample size of {n_trades} trades is below the minimum threshold (5) "
                "required for a reliable bootstrap confidence interval."
            ),
            (
                "> Reported bootstrap 95% confidence interval on expectancy: "
                f"**[{low_ci:+.6f}, {high_ci:+.6f}]** SOL / trade."
            ),
            "",
        ]
    return [
        "> [!NOTE]",
        (
            f"> Bootstrap 95% confidence interval on expectancy over {n_trades} trades: "
            f"**[{low_ci:+.6f}, {high_ci:+.6f}]** SOL per trade."
        ),
        "",
    ]


def _summary_rows(
    result: BacktestResult, summary: dict[str, Any], low_ci: float, high_ci: float
) -> list[str]:
    mode_str = "Paper Trading (Synthetic)" if result.synthetic else "Paper Trading (Real Store)"
    win_rate = summary.get("win_rate", 0.0)
    total_ret = summary.get("total_return", 0.0)
    expectancy = summary.get("expectancy_native", 0.0)
    start_cap = result.account.get("starting_native", 10.0)
    end_eq = result.account.get("equity_native", 10.0)
    realized_pnl = result.account.get("realized_pnl_native", 0.0)

    return [
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Trading Mode** | {mode_str} |",
        f"| **Evaluated Universe** | {result.universe_size} tokens |",
        f"| **Tokens Evaluated** | {result.evaluated} |",
        f"| **Positions Entered** | {result.entered} |",
        f"| **Completed Trades** | {len(result.trades)} |",
        f"| **Win Rate** | {win_rate:.2%} |",
        f"| **Starting Capital** | {start_cap:.4f} SOL |",
        f"| **Ending Equity** | {end_eq:.4f} SOL |",
        f"| **Realized PnL** | {realized_pnl:+.4f} SOL |",
        f"| **Total Return** | {total_ret:+.2%} |",
        f"| **Expectancy** | {expectancy:+.6f} SOL / trade |",
        f"| **Bootstrap 95% Confidence Interval** | **[{low_ci:+.6f}, {high_ci:+.6f}]** SOL / trade |",
    ]


def _trade_log_rows(trades: list[TradeRecord]) -> list[str]:
    rows: list[str] = [
        "| Token | Symbol | Entry Time | Score | Exit Time | Hold (min) | Exit Reason | Size (SOL) | PnL (SOL) | Multiple |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    if not trades:
        rows.append("| — | — | — | — | — | — | No trades entered | — | — | — |")
        return rows

    for t in trades:
        symbol = t.symbol or t.token_key[:8]
        entry_str = t.entered_at.strftime("%Y-%m-%d %H:%M")
        exit_str = t.exited_at.strftime("%Y-%m-%d %H:%M") if t.exited_at else "Open"
        hold_min = f"{t.hold_seconds / 60.0:.1f}"
        pnl_str = f"{t.pnl_native:+.4f}"
        mult_str = f"{t.multiple:.2f}x"
        rows.append(
            f"| `{t.token_key[:12]}` | `{symbol}` | {entry_str} | {t.score:.3f} | {exit_str} | "
            f"{hold_min} | {t.exit_reason} | {t.size_native:.3f} | {pnl_str} | {mult_str} |"
        )
    return rows


def generate_track_record_markdown(
    result: BacktestResult,
    as_of: datetime | None = None,
) -> str:
    """Format a BacktestResult or paper-trading run into a markdown track record page."""
    timestamp = (as_of or utcnow()).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = result.summary()
    low_ci, high_ci = result.bootstrap_expectancy_ci()
    n_trades = len(result.trades)

    lines: list[str] = [
        "# Botsensai Paper-Trading Track Record",
        "",
        f"*Generated at: {timestamp}*",
        "",
        "## Executive Summary",
        "",
    ]
    lines.extend(_summary_rows(result, summary, low_ci, high_ci))
    lines.append("")
    lines.extend(["## Expectancy & Confidence Interval", ""])
    lines.extend(_ci_callout(n_trades, low_ci, high_ci))
    lines.extend(["## Trade Log", ""])
    lines.extend(_trade_log_rows(result.trades))
    lines.extend([
        "",
        "## Risk & Execution Audit",
        "",
        f"- **Rejected Orders**: {result.account.get('rejected_orders', 0)}",
        f"- **Failed Fills**: {result.account.get('failed_fills', 0)}",
        f"- **Total Fills**: {result.account.get('fills', 0)}",
        "",
    ])

    return "\n".join(lines)


def build_track_record(
    settings: Settings | None = None,
    days: float = 2.0,
    synthetic: bool = False,
    universe: int = 60,
    capital: float = 10.0,
) -> tuple[BacktestResult, str]:
    """Run paper-trading evaluation and return (BacktestResult, markdown_content)."""
    from botsensai.backtest.engine import Backtester
    from botsensai.cli import _backtest_tapes

    s = settings or get_settings()
    tester = Backtester(s)
    end = utcnow()
    start = end - timedelta(days=days)

    tapes, is_synth = _backtest_tapes(
        s, start, end, days=days, synthetic=synthetic, universe=universe
    )

    result = tester.run(tapes, starting_native=capital, synthetic=is_synth)
    markdown = generate_track_record_markdown(result, as_of=end)
    return result, markdown


__all__ = [
    "build_track_record",
    "generate_track_record_markdown",
]
