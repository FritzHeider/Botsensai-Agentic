#!/usr/bin/env python3
import sqlite3
import time

conn = sqlite3.connect("/home/ubuntu/Botsensai/data/botsensai.db")
conn.row_factory = sqlite3.Row

print("=== ALL LIVE POSITIONS RECORDED IN DB ===")
positions = conn.execute(
    "SELECT mint, symbol, status, amount_token, cost_sol, realized_pnl_sol, "
    "entry_price_sol, peak_price_sol, opened_at, closed_at, exit_reason, entry_tx_hash, exit_tx_hash "
    "FROM live_positions ORDER BY opened_at ASC"
).fetchall()

for p in positions:
    entry = p["entry_price_sol"] or 1e-9
    peak = p["peak_price_sol"] or entry
    peak_mult = peak / entry if entry > 0 else 1.0
    open_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["opened_at"])) if p["opened_at"] else "N/A"
    close_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["closed_at"])) if p["closed_at"] else "OPEN"
    print(
        f"${p['symbol']} ({p['mint'][:8]}...) | Status: {p['status']} | "
        f"Cost: {p['cost_sol']:.4f} SOL | PnL: {p['realized_pnl_sol']:.4f} SOL | "
        f"Peak: {peak_mult:.2f}x | Opened: {open_ts} | Closed: {close_ts} | Reason: {p['exit_reason']}"
    )

print("\n=== CONFIRMED ON-CHAIN SELL TRANSACTIONS & STAGE EXITS ===")
signals = conn.execute(
    "SELECT id, side, symbol, mint, size_native, status, tx_hash, created_at "
    "FROM execution_signals WHERE side = 'SELL' AND status = 'CONFIRMED' ORDER BY created_at ASC"
).fetchall()

for s in signals:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s["created_at"])) if s["created_at"] else "N/A"
    print(f"Signal #{s['id']} [{ts}]: SELL ${s['symbol']} ({s['size_native']:.4f} SOL) -> {s['status']} | Tx: {s['tx_hash']}")
