#!/usr/bin/env python3
import sqlite3
import time

conn = sqlite3.connect("/home/ubuntu/Botsensai/data/botsensai.db")
conn.row_factory = sqlite3.Row
two_days_ago = time.time() - 2 * 86400

rows = conn.execute(
    "SELECT symbol, mint, status, amount_token, cost_sol, entry_price_sol, "
    "peak_price_sol, opened_at, closed_at, exit_reason "
    "FROM live_positions WHERE opened_at >= ? ORDER BY opened_at ASC",
    (two_days_ago,),
).fetchall()

runners = []
moderate_gainers = []
for r in rows:
    entry = r["entry_price_sol"] or 1e-9
    peak = r["peak_price_sol"] or entry
    mult = peak / entry if entry > 0 else 1.0
    reason = (r["exit_reason"] or "").lower()
    if mult >= 1.80 or "stage" in reason or "moonbag" in reason:
        runners.append((r, mult))
    elif mult >= 1.30:
        moderate_gainers.append((r, mult))

print("=" * 85)
print(f"BOTSENSAI LIVE POSITION AUDIT (PAST 48 HOURS)")
print(f"Window: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(two_days_ago))} UTC to Present")
print(f"Total Live Trades Taken: {len(rows)}")
print(f"Parabolic / Moonbag Runners (>= 1.80x Peak or Staged Exit): {len(runners)}")
print(f"Breakout Gainers (1.30x - 1.79x Peak): {len(moderate_gainers)}")
print("=" * 85)

print("\n--- 🚀 PARABOLIC MOONBAG RUNNERS (>= 1.80x or STAGED EXITS) ---")
for r, mult in runners:
    open_ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["opened_at"]))
    close_ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["closed_at"])) if r["closed_at"] else "ACTIVE OPEN"
    status_str = f"[{r['status']}]"
    print(f"{status_str:<8} ${r['symbol']:<12} Peak: {mult:>5.2f}x | Entry: {r['cost_sol']:.4f} SOL | Opened: {open_ts} | Exit Reason: {r['exit_reason']}")

print("\n--- 📈 BREAKOUT RUNNERS (1.30x - 1.79x Peak) ---")
for r, mult in moderate_gainers:
    open_ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["opened_at"]))
    print(f"[{r['status']}] ${r['symbol']:<12} Peak: {mult:>5.2f}x | Cost: {r['cost_sol']:.4f} SOL | Opened: {open_ts} | Exit Reason: {r['exit_reason']}")
