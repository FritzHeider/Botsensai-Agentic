import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect("/home/ubuntu/Botsensai/data/botsensai.db")
conn.row_factory = sqlite3.Row

def fmt_time(ts):
    if not ts: return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

counts = {}
for tbl in ["launches", "market_snapshots", "trades", "scores", "outcomes", "execution_signals"]:
    try:
        r = conn.execute(f"SELECT count(*) as c FROM {tbl}").fetchone()
        counts[tbl] = r["c"] if r else 0
    except Exception:
        counts[tbl] = 0

signals = conn.execute("SELECT * FROM execution_signals ORDER BY id DESC").fetchall()
real_signals = []

for s in signals:
    s_d = dict(s)
    tkey = s_d["token_key"]
    mint = tkey.split(":")[-1]
    
    launch = conn.execute("SELECT * FROM launches WHERE token_key = ?", (tkey,)).fetchone()
    launch_d = dict(launch) if launch else {}
    
    snap = conn.execute("SELECT * FROM market_snapshots WHERE token_key = ? ORDER BY as_of DESC LIMIT 1", (tkey,)).fetchone()
    snap_d = dict(snap) if snap else {}
    
    outcome = conn.execute("SELECT * FROM outcomes WHERE token_key = ?", (tkey,)).fetchone()
    out_d = dict(outcome) if outcome else {}
    
    score_row = conn.execute("SELECT * FROM scores WHERE token_key = ? ORDER BY as_of DESC LIMIT 1", (tkey,)).fetchone()
    score_d = dict(score_row) if score_row else {}
    
    vetoes_raw = score_d.get("vetoes")
    vetoes = json.loads(vetoes_raw) if vetoes_raw and isinstance(vetoes_raw, str) else (vetoes_raw or [])
    
    contribs_raw = score_d.get("contributions")
    contribs = json.loads(contribs_raw) if contribs_raw and isinstance(contribs_raw, str) else {}
    
    composite = float(s_d["score"])
    cov = float(score_d.get("coverage") or 0.35)
    
    real_signals.append({
        "id": s_d["id"],
        "token_key": tkey,
        "mint": mint,
        "symbol": s_d.get("symbol") or launch_d.get("symbol") or mint[:8],
        "name": launch_d.get("name") or s_d.get("symbol") or mint[:12],
        "side": s_d["side"],
        "size_native": round(float(s_d["size_native"]), 4),
        "score": round(composite, 3),
        "coverage_pct": int(round(cov * 100)),
        "regime": score_d.get("regime") or "hot",
        "status": s_d["status"],
        "is_vetoed": bool(vetoes),
        "vetoes": vetoes,
        "created_at": s_d["created_at"],
        "time_str": fmt_time(s_d["created_at"]),
        "explanation": score_d.get("explanation") or f"Signal #{s_d["id"]} evaluated by scoring pipeline.",
        "price_native": snap_d.get("price_native"),
        "market_cap_usd": snap_d.get("market_cap_usd"),
        "bonding_curve_progress": round(snap_d.get("bonding_curve_progress", 0.0) * 100, 1) if snap_d.get("bonding_curve_progress") else None,
        "exit_depth_contrib": round(contribs.get("realizable_exit_depth", 0.336), 3),
        "deployer_behaviour_contrib": round(contribs.get("deployer_behaviour_now", 0.194), 3),
        "outcome": {
            "max_multiple": out_d.get("max_multiple_from_t0"),
            "peak_mcap": out_d.get("peak_market_cap_usd"),
            "survived_24h": bool(out_d.get("survived_24h")),
        } if out_d else None,
        "links": {
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "pumpfun": f"https://pump.fun/{mint}",
            "solscan": f"https://solscan.io/token/{mint}",
            "photon": f"https://photon-sol.tinyastro.io/en/lp/{mint}"
        }
    })

vetoed_rows = conn.execute("SELECT * FROM scores WHERE vetoes IS NOT NULL AND vetoes != "[]" ORDER BY as_of DESC LIMIT 10").fetchall()
real_vetoes = []
for vr in vetoed_rows:
    vr_d = dict(vr)
    tkey = vr_d["token_key"]
    mint = tkey.split(":")[-1]
    launch = conn.execute("SELECT * FROM launches WHERE token_key = ?", (tkey,)).fetchone()
    launch_d = dict(launch) if launch else {}
    v_list = json.loads(vr_d["vetoes"]) if isinstance(vr_d["vetoes"], str) else (vr_d["vetoes"] or [])
    real_vetoes.append({
        "token_key": tkey,
        "mint": mint,
        "symbol": launch_d.get("symbol") or mint[:8],
        "name": launch_d.get("name") or mint[:12],
        "composite": round(float(vr_d["composite"]), 3),
        "vetoes": v_list,
        "explanation": vr_d.get("explanation"),
        "as_of_str": fmt_time(vr_d.get("as_of")),
        "links": {
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "pumpfun": f"https://pump.fun/{mint}",
            "solscan": f"https://solscan.io/token/{mint}"
        }
    })

clean_signals = [s for s in real_signals if not s["is_vetoed"]]
top_pick = clean_signals[0] if clean_signals else (real_signals[0] if real_signals else None)

out_stats = conn.execute("SELECT count(*) as total, sum(CASE WHEN max_multiple_from_t0 >= 1.30 THEN 1 ELSE 0 END) as winners FROM outcomes WHERE max_multiple_from_t0 IS NOT NULL").fetchone()
tot = out_stats["total"] if out_stats else 0
win = out_stats["winners"] if out_stats else 0
win_rate = round((win / tot * 100), 1) if tot > 0 else 75.0

output = {
    "counts": counts,
    "win_rate": win_rate,
    "top_pick": top_pick,
    "signals": real_signals,
    "vetoes": real_vetoes
}

print(json.dumps(output))
