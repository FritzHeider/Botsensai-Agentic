#!/usr/bin/env python3
"""
Botsensai — Pump.fun Top-100 Wallet Seeder
===========================================
Fetches the top 100 profit-generating wallets from Pump.fun and seeds them
into the ``top_traders`` table for the copy-trade pipeline to act on.

Strategy (in priority order):
  1. Helius DAS ``searchAssets`` — find wallets with the most pump.fun buys
     in the last 7 days via the transaction history API.
  2. Load existing ``data/top_500_wallets.json`` if present (snapshot fallback).
  3. Merge DB rows (never wipe existing entries, only update/add).

Run periodically (e.g., every 6 hours via cron or SSM) to keep the leaderboard
fresh without blocking the trading daemon.

Usage:
    python3 scripts/seed_top_wallets.py [--limit 100] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Bootstrap path so we can import the package from the repo root ──────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

# ── Config ───────────────────────────────────────────────────────────────────
HELIUS_API_KEY = (
    os.getenv("HELIUS_API_KEY")
    or os.getenv("BOTSENSAI_HELIUS_API_KEY")
    or "4cb5eb58-aaf8-482a-ba63-fc8ff63c270e"
)
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_DAS_URL = f"https://api.helius.xyz/v0"

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPFUN_API_BASE = "https://frontend-api.pump.fun"

TOP_WALLET_JSON = REPO_ROOT / "data" / "top_500_wallets.json"
SCHEMA_TOP_TRADERS_JSON = REPO_ROOT / "src" / "botsensai" / "schemas" / "top_traders.json"
DB_PATH_DEFAULT = Path(
    os.getenv("BOTSENSAI_DB_PATH")
    or (
        str(REPO_ROOT / "data" / "botsensai_remote.db")
        if (REPO_ROOT / "data" / "botsensai_remote.db").exists()
        else str(REPO_ROOT / "data" / "botsensai.db")
    )
)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url: str, timeout: float = 10.0) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Botsensai/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [GET] {url[:80]}  →  {e}")
        return None


def _post(url: str, payload: dict, timeout: float = 10.0) -> dict | None:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Botsensai/2.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [POST] {url[:80]}  →  {e}")
        return None


# ── Source 1: Pump.fun leaderboard API ───────────────────────────────────────

def fetch_pumpfun_leaderboard(limit: int = 100) -> list[dict]:
    """Query pump.fun leaderboard for top PnL wallets."""
    print(f"[seed] Fetching pump.fun top-{limit} leaderboard...")
    traders: list[dict] = []

    # Pump.fun exposes a coins endpoint — use it to get recent traders from
    # high-graduation tokens and build a frequency/PnL map.
    # Try their king-of-the-hill / top traders endpoint first.
    endpoints = [
        f"{PUMPFUN_API_BASE}/leaderboard?limit={limit}&offset=0&timeframe=7d",
        f"{PUMPFUN_API_BASE}/traders?limit={limit}&sort=profit&timeframe=7d",
        f"{PUMPFUN_API_BASE}/top-traders?limit={limit}",
    ]
    for url in endpoints:
        data = _get(url, timeout=8.0)
        if data and isinstance(data, list) and len(data) > 0:
            for i, item in enumerate(data[:limit]):
                wallet = item.get("wallet") or item.get("address") or item.get("user")
                if not wallet:
                    continue
                traders.append({
                    "wallet": wallet,
                    "rank": i + 1,
                    "winrate": float(item.get("winRate", item.get("win_rate", 0)) or 0),
                    "profit_7d": float(item.get("realizedPnl", item.get("profit", 0)) or 0),
                    "txs_1d": int(item.get("tradesCount", item.get("txs", 0)) or 0),
                    "sol_balance": 0.0,
                    "tags": [],
                    "source": "pump_fun_api",
                })
            if traders:
                print(f"  ✅ pump.fun API returned {len(traders)} traders")
                return traders

    print("  ⚠️  pump.fun API returned no structured leaderboard — falling back to Helius tx scan")
    return []


# ── Source 2: Helius transaction scan (fallback) ──────────────────────────────

def fetch_helius_top_traders(limit: int = 100) -> list[dict]:
    """Use Helius to find wallets with most pump.fun BUY transactions in 7d."""
    print(f"[seed] Scanning Helius for top pump.fun traders (last 7d)...")

    # Get recent pump.fun program signatures (last ~2000 txns)
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            PUMP_FUN_PROGRAM,
            {"limit": 1000, "commitment": "finalized"}
        ]
    }
    res = _post(HELIUS_RPC_URL, payload, timeout=15.0)
    if not res or "result" not in res:
        print("  ❌ Helius RPC getSignaturesForAddress failed")
        return []

    sigs = [s["signature"] for s in res["result"] if not s.get("err")][:500]
    print(f"  Got {len(sigs)} recent pump.fun tx signatures")

    # Parse transactions in batches to find buyer wallets
    wallet_counts: dict[str, int] = {}
    batch_size = 25
    for i in range(0, min(len(sigs), 200), batch_size):
        batch = sigs[i:i + batch_size]
        tx_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransactions",
            "params": [batch, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        tx_res = _post(HELIUS_RPC_URL, tx_payload, timeout=20.0)
        if not tx_res or "result" not in tx_res:
            continue
        for tx in tx_res["result"]:
            if not tx or tx.get("meta", {}).get("err"):
                continue
            # The fee payer / first signer is the trader
            accs = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            if accs:
                signer = accs[0].get("pubkey") if isinstance(accs[0], dict) else str(accs[0])
                if signer and signer != PUMP_FUN_PROGRAM:
                    wallet_counts[signer] = wallet_counts.get(signer, 0) + 1
        time.sleep(0.1)

    # Sort by frequency and return top-N
    sorted_wallets = sorted(wallet_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    traders = []
    for rank, (wallet, tx_count) in enumerate(sorted_wallets, start=1):
        traders.append({
            "wallet": wallet,
            "rank": rank,
            "winrate": 0.0,  # unknown from tx scan alone
            "profit_7d": 0.0,
            "txs_1d": tx_count,
            "sol_balance": 0.0,
            "tags": ["pump_fun_active"],
            "source": "helius_tx_scan",
        })

    print(f"  ✅ Helius scan found {len(traders)} active pump.fun traders")
    return traders


# ── Source 3: Existing JSON snapshot (static fallback) ───────────────────────

def load_json_snapshot() -> list[dict]:
    """Load existing top_500_wallets.json as a fallback data source."""
    if not TOP_WALLET_JSON.exists():
        return []
    try:
        data = json.loads(TOP_WALLET_JSON.read_text())
        if not isinstance(data, list):
            return []
        traders = []
        for i, item in enumerate(data):
            wallet = item.get("address") or item.get("wallet")
            if not wallet:
                continue
            traders.append({
                "wallet": wallet,
                "rank": item.get("rank", i + 1),
                "winrate": float(item.get("winrate", 0) or 0),
                "profit_7d": float(item.get("profit_7d", 0) or 0),
                "txs_1d": int(item.get("txs_1d", 0) or 0),
                "sol_balance": float(item.get("sol_balance", 0) or 0),
                "tags": item.get("tags", []),
                "source": "json_snapshot",
            })
        print(f"  ✅ JSON snapshot: {len(traders)} wallets from {TOP_WALLET_JSON.name}")
        return traders
    except Exception as e:
        print(f"  ⚠️  JSON snapshot load failed: {e}")
        return []


# ── Source 4: JSON Schema snapshot (src/botsensai/schemas/top_traders.json) ──

def load_schema_snapshot() -> list[dict]:
    """Load src/botsensai/schemas/top_traders.json as a high-conviction seed source."""
    if not SCHEMA_TOP_TRADERS_JSON.exists():
        return []
    try:
        data = json.loads(SCHEMA_TOP_TRADERS_JSON.read_text())
        traders_raw = (
            data.get("traders", [])
            if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )
        traders = []
        for i, item in enumerate(traders_raw):
            wallet = item.get("wallet") or item.get("mint") or item.get("address")
            if not wallet:
                continue
            wr = float(item.get("winrate") or item.get("mid_rise_score") or (float(item.get("score", 0)) * 100) or 0)
            traders.append({
                "wallet": wallet,
                "mint": item.get("mint") or wallet,
                "symbol": item.get("symbol", ""),
                "name": item.get("name") or f"Trader_{i+1}",
                "rank": item.get("rank", i + 1),
                "winrate": wr,
                "mid_rise_score": float(item.get("mid_rise_score", 0) or wr),
                "score": float(item.get("score", 0) or (wr / 100.0)),
                "profit_7d": float(item.get("profit_7d", 0) or 0),
                "txs_1d": int(item.get("txs_1d", 0) or 0),
                "sol_balance": float(item.get("sol_balance", 0) or 0),
                "tags": item.get("tags", ["schema_top_traders"]),
                "source": "schema_json",
            })
        print(f"  ✅ Schema snapshot: {len(traders)} items from {SCHEMA_TOP_TRADERS_JSON.name}")
        return traders
    except Exception as e:
        print(f"  ⚠️  Schema snapshot load failed: {e}")
        return []


# ── Merge & deduplicate ───────────────────────────────────────────────────────

def merge_traders(sources: list[list[dict]], limit: int = 100) -> list[dict]:
    """Merge multiple trader lists, dedup by wallet, re-rank."""
    seen: dict[str, dict] = {}
    for source in sources:
        for t in source:
            wallet = t.get("wallet", "")
            if not wallet:
                continue
            if wallet not in seen:
                seen[wallet] = t
            else:
                existing = seen[wallet]
                if float(t.get("winrate", 0)) > float(existing.get("winrate", 0)):
                    seen[wallet] = t

    merged = list(seen.values())[:limit]
    for i, t in enumerate(merged):
        t["rank"] = i + 1

    return merged


# ── DB write ──────────────────────────────────────────────────────────────────

def seed_to_db(traders: list[dict], db_path: Path, dry_run: bool = False) -> int:
    if dry_run:
        print(f"[seed] DRY RUN — would write {len(traders)} traders to {db_path}")
        for t in traders[:5]:
            print(f"  #{t.get('rank', 0):3d}  {t.get('wallet', '')[:12]}...  wr={t.get('winrate', 0):.1f}%  mid_rise={t.get('mid_rise_score', 0)}  src={t.get('source', '')}")
        if len(traders) > 5:
            print(f"  ... and {len(traders) - 5} more")
        return len(traders)

    import sqlite3
    now = time.time()
    conn = sqlite3.connect(str(db_path))

    # 1. Ensure table exists with core columns
    conn.execute("""
        CREATE TABLE IF NOT EXISTS top_traders (
            wallet TEXT PRIMARY KEY,
            skill_score REAL NOT NULL DEFAULT 0.70,
            win_rate REAL NOT NULL DEFAULT 0.0,
            mean_multiple REAL NOT NULL DEFAULT 1.0,
            typical_size_sol REAL NOT NULL DEFAULT 0.015,
            total_trades INTEGER NOT NULL DEFAULT 0,
            winning_trades INTEGER NOT NULL DEFAULT 0,
            runner_count INTEGER NOT NULL DEFAULT 0,
            last_trade_at REAL,
            status TEXT DEFAULT 'ACTIVE',
            updated_at REAL NOT NULL DEFAULT 0
        )
    """)

    # 2. Check and migrate any missing schema columns
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(top_traders)").fetchall()}
    needed_cols = {
        "rank": "INTEGER DEFAULT 999",
        "profit_7d": "REAL DEFAULT 0.0",
        "txs_1d": "INTEGER DEFAULT 0",
        "sol_balance": "REAL DEFAULT 0.0",
        "tags": "TEXT DEFAULT '[]'",
        "source": "TEXT DEFAULT 'pump_fun'",
        "refreshed_at": "REAL DEFAULT 0",
        "mint": "TEXT",
        "mid_rise_score": "REAL DEFAULT 0.0",
        "name": "TEXT DEFAULT ''",
        "score": "REAL DEFAULT 0.0",
    }
    for col, col_def in needed_cols.items():
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE top_traders ADD COLUMN {col} {col_def}")
            except Exception:
                pass

    # 3. Create top_traders_mints table for direct mint-based queries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS top_traders_mints (
            mint TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            mid_rise_score REAL,
            score REAL,
            source TEXT,
            refreshed_at REAL
        )
    """)

    written = 0
    for t in traders:
        wallet = t.get("wallet") or t.get("mint")
        if not wallet:
            continue
        wr = float(t.get("winrate", 0))
        if wr > 1.0:
            wr = wr / 100.0
        mint = t.get("mint") or wallet
        mid_rise = float(t.get("mid_rise_score") or (wr * 100))
        score = float(t.get("score") or wr)
        total_tx = int(t.get("txs_1d", 10) or 10)
        win_tx = max(1, int(round(total_tx * wr)))

        conn.execute("""
            INSERT INTO top_traders
                (wallet, rank, win_rate, profit_7d, txs_1d, sol_balance,
                 skill_score, mean_multiple, typical_size_sol, total_trades, winning_trades,
                 runner_count, tags, status, source, refreshed_at, updated_at,
                 mint, mid_rise_score, name, score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE',?,?,?,?,?,?,?)
            ON CONFLICT(wallet) DO UPDATE SET
                rank=COALESCE(excluded.rank, top_traders.rank),
                win_rate=excluded.win_rate,
                profit_7d=excluded.profit_7d,
                txs_1d=excluded.txs_1d,
                skill_score=excluded.skill_score,
                mean_multiple=excluded.mean_multiple,
                typical_size_sol=excluded.typical_size_sol,
                total_trades=excluded.total_trades,
                winning_trades=excluded.winning_trades,
                tags=excluded.tags,
                status='ACTIVE',
                source=excluded.source,
                refreshed_at=excluded.refreshed_at,
                updated_at=excluded.updated_at,
                mint=COALESCE(excluded.mint, top_traders.mint),
                mid_rise_score=COALESCE(excluded.mid_rise_score, top_traders.mid_rise_score),
                name=COALESCE(excluded.name, top_traders.name),
                score=COALESCE(excluded.score, top_traders.score)
        """, (
            wallet, t.get("rank", 999), wr, t.get("profit_7d", 0.0),
            t.get("txs_1d", 0), t.get("sol_balance", 0.0),
            min(1.0, max(0.5, score)), float(t.get("mean_multiple", 1.05)), 0.015,
            total_tx, win_tx, 0,
            json.dumps(t.get("tags", [])),
            t.get("source", "pump_fun"),
            now, now, mint, mid_rise, t.get("name", ""), score
        ))

        if t.get("mint"):
            conn.execute("""
                INSERT OR REPLACE INTO top_traders_mints
                    (mint, symbol, name, mid_rise_score, score, source, refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                t["mint"], t.get("symbol", ""), t.get("name", ""),
                mid_rise, score, t.get("source", "schema"), now
            ))
        written += 1

    conn.commit()
    conn.close()
    print(f"[seed] ✅ Seeded {written} top traders into {db_path}")
    return written


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed pump.fun top-100 wallets into top_traders table")
    parser.add_argument("--limit", type=int, default=100, help="Number of wallets to seed (default: 100)")
    parser.add_argument("--db", type=Path, default=DB_PATH_DEFAULT, help="Path to botsensai.db")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Botsensai — Pump.fun Top-{args.limit} Wallet Seeder")
    print(f"{'='*60}\n")

    all_sources: list[list[dict]] = []

    # 1. Load JSON Schema snapshot if present
    schema_traders = load_schema_snapshot()
    if schema_traders:
        all_sources.append(schema_traders)

    # 2. Try pump.fun leaderboard API
    pf_traders = fetch_pumpfun_leaderboard(args.limit)
    if pf_traders:
        all_sources.append(pf_traders)

    # 3. Helius tx scan (supplement/fallback)
    helius_traders = fetch_helius_top_traders(args.limit)
    if helius_traders:
        all_sources.append(helius_traders)

    # 4. JSON snapshot fallback
    json_traders = load_json_snapshot()
    if json_traders:
        all_sources.append(json_traders)

    if not all_sources:
        print("\n❌ No trader data available from any source. Aborting.")
        sys.exit(1)

    # Merge & deduplicate
    traders = merge_traders(all_sources, limit=args.limit)
    print(f"\n[seed] Merged {len(traders)} unique pump.fun top traders\n")

    # Write to DB
    seed_to_db(traders, args.db, dry_run=args.dry_run)

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
