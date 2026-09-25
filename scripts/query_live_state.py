import sqlite3
import json
import urllib.request
import time
from solders.keypair import Keypair

target = "/home/ubuntu/.config/solana/id.json"
with open(target) as f:
    kp = Keypair.from_bytes(bytes(json.load(f)))
pub = str(kp.pubkey())

req = urllib.request.Request(
    "https://api.mainnet-beta.solana.com",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pub]}).encode(),
    headers={"Content-Type": "application/json"}
)
bal = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())["result"]["value"] / 1e9
print(f"WALLET: {pub} | LIQUID SOL: {bal:.6f}")

conn = sqlite3.connect("/home/ubuntu/Botsensai/data/botsensai.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT mint, symbol, amount_token, cost_sol, entry_price_sol, peak_price_sol, opened_at, exit_reason FROM live_positions WHERE status = 'OPEN'").fetchall()
print(f"OPEN POSITIONS: {len(rows)}")
total_token_val_sol = 0.0
for r in rows:
    age_min = (time.time() - (r["opened_at"] or time.time())) / 60
    mint = r["mint"]
    # get live price
    px = 0.0
    try:
        d_req = urllib.request.Request(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", headers={"User-Agent": "Botsensai"})
        d_data = json.loads(urllib.request.urlopen(d_req, timeout=2.5).read().decode())
        pairs = d_data.get("pairs") or []
        if pairs:
            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
            px = float(best.get("priceNative") or 0.0)
    except Exception:
        pass
    val_sol = px * (r["amount_token"] or 0.0)
    total_token_val_sol += val_sol
    entry = r["entry_price_sol"] or 1e-9
    mult = px / entry if entry > 0 else 0
    print(f"  {r['symbol']} ({mint[:8]}...): amt={r['amount_token']:,.2f}, cost={r['cost_sol']:.4f} SOL, val={val_sol:.4f} SOL ({mult:.2f}x), age={age_min:.1f}m, reason={r['exit_reason']}")

print(f"TOTAL TOKEN EQUITY: {total_token_val_sol:.6f} SOL")
print(f"TOTAL NET WORTH: {bal + total_token_val_sol:.6f} SOL (${(bal + total_token_val_sol)*113.3:.2f} USD @ $113.3/SOL)")

print("\n--- RECENT EXECUTION SIGNALS ---")
sigs = conn.execute("SELECT id, strftime('%H:%M:%S', created_at, 'unixepoch') as ts, side, symbol, size_native, status, error, tx_hash FROM execution_signals ORDER BY id DESC LIMIT 6").fetchall()
for s in sigs:
    err = f" | {s['error'][:40]}" if s['error'] else ""
    tx = f" | {s['tx_hash'][:16]}..." if s['tx_hash'] else ""
    print(f"  #{s['id']} [{s['ts']}] {s['side']} ${s['symbol']} ({s['size_native']:.4f} SOL) -> {s['status']}{err}{tx}")
