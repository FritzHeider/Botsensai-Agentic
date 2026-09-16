╭───────────────────────────── botsensai ─────────────────────────────╮
│ mode: paper   metrics: 34   db: data/botsensai.db   env: virtualenv │
╰─────────────────────────────────────────────────────────────────────╯
           paper-trading track record summary           
┏━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ metric            ┃                            value ┃
┡━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ evaluated tokens  │                              200 │
│ entered positions │                               20 │
│ completed trades  │                               20 │
│ win rate          │                            0.00% │
│ total return      │                           -0.60% │
│ expectancy        │              -0.001988 SOL/trade │
│ bootstrap 95% CI  │ [-0.002023, -0.001943] SOL/trade │
└───────────────────┴──────────────────────────────────┘

wrote track record to docs/TRACK_RECORD.md
# Botsensai Paper-Trading Track Record

*Generated at: 2026-09-15 15:47:44 UTC*

## Executive Summary

| Metric | Value |
| :--- | :--- |
| **Trading Mode** | Paper Trading (Real Store) |
| **Evaluated Universe** | 60 tokens |
| **Tokens Evaluated** | 200 |
| **Positions Entered** | 20 |
| **Completed Trades** | 20 |
| **Win Rate** | 0.00% |
| **Starting Capital** | 10.0000 SOL |
| **Ending Equity** | 9.9400 SOL |
| **Realized PnL** | -0.0398 SOL |
| **Total Return** | -0.60% |
| **Expectancy** | -0.001988 SOL / trade |
| **Bootstrap 95% Confidence Interval** | **[-0.002023, -0.001943]** SOL / trade |

## Expectancy & Confidence Interval

> [!NOTE]
> Bootstrap 95% confidence interval on expectancy over 20 trades: **[-0.002023, -0.001943]** SOL per trade.

## Trade Log

| Token | Symbol | Entry Time | Score | Exit Time | Hold (min) | Exit Reason | Size (SOL) | PnL (SOL) | Multiple |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `solana:BurE7` | `RCHAD` | 2026-09-06 15:51 | 0.734 | 2026-09-06 15:52 | 1.0 | stop loss at 0.37x | 0.001 | -0.0018 | 1.32x |
| `solana:7DCfx` | `TRUMP⁠` | 2026-09-06 15:53 | 0.690 | 2026-09-06 15:54 | 1.0 | stop loss at 0.24x | 0.000 | -0.0018 | 1.38x |
| `solana:CYN3f` | `E-BIKE` | 2026-09-06 15:54 | 0.802 | 2026-09-06 15:56 | 2.0 | stop loss at 0.48x | 0.001 | -0.0017 | 1.22x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 15:53 | 0.872 | 2026-09-06 15:58 | 5.0 | stop loss at 0.51x | 0.002 | -0.0020 | 1.02x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 15:58 | 0.733 | 2026-09-06 15:59 | 1.0 | stop loss at 0.27x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 15:59 | 0.735 | 2026-09-06 16:00 | 1.0 | stop loss at 0.27x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:00 | 0.736 | 2026-09-06 16:01 | 1.0 | stop loss at 0.27x | 0.001 | -0.0020 | 0.96x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:01 | 0.720 | 2026-09-06 16:02 | 1.0 | stop loss at 0.24x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:02 | 0.720 | 2026-09-06 16:03 | 1.0 | stop loss at 0.24x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:03 | 0.716 | 2026-09-06 16:04 | 1.0 | stop loss at 0.23x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:04 | 0.720 | 2026-09-06 16:05 | 1.0 | stop loss at 0.24x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:05 | 0.721 | 2026-09-06 16:06 | 1.0 | stop loss at 0.24x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:06 | 0.722 | 2026-09-06 16:07 | 1.0 | stop loss at 0.28x | 0.001 | -0.0020 | 0.97x |
| `solana:AG9XS` | `TRENCHBRAIN` | 2026-09-06 16:07 | 0.724 | 2026-09-06 16:08 | 1.0 | stop loss at 0.29x | 0.001 | -0.0020 | 0.97x |
| `solana:GrsZ5` | `PENGU⁠` | 2026-09-06 15:50 | 0.713 | 2026-09-06 16:10 | 20.2 | end of backtest window | 0.001 | -0.0020 | 0.97x |
| `solana:yJnLa` | `Fever` | 2026-09-06 15:51 | 0.735 | 2026-09-06 16:10 | 19.4 | end of backtest window | 0.001 | -0.0020 | 0.96x |
| `solana:Da1c3` | `$NIGHT` | 2026-09-06 15:52 | 0.677 | 2026-09-06 16:10 | 18.2 | end of backtest window | 0.000 | -0.0020 | 0.97x |
| `solana:25mmM` | `CLANCY` | 2026-09-06 15:52 | 0.702 | 2026-09-06 16:10 | 17.8 | end of backtest window | 0.000 | -0.0020 | 0.97x |
| `solana:Dz5qD` | `LEVELUP` | 2026-09-06 15:53 | 0.691 | 2026-09-06 16:10 | 17.4 | end of backtest window | 0.000 | -0.0020 | 0.97x |
| `solana:DTfw8` | `MASTER⁠` | 2026-09-06 15:54 | 0.727 | 2026-09-06 16:10 | 15.7 | end of backtest window | 0.001 | -0.0020 | 0.97x |

## Risk & Execution Audit

- **Rejected Orders**: 11
- **Failed Fills**: 0
- **Total Fills**: 40
