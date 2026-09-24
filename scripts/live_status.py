#!/usr/bin/env python3
"""
Botsensai Live Trading System & Health Status Checker
Queries systemd services, hot wallet balance, database metrics, and latest trade activity.
"""

import json
import os
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

HOT_WALLET = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
DB_PATH = "/home/ubuntu/Botsensai/data/botsensai.db"
RPC_URL = os.getenv("SOLANA_RPC_URL") or os.getenv("BOTSENSAI_SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"

SERVICES = [
    "botsensai-daemon",
    "botsensai-executor",
    "botsensai-serve",
    "botsensai-telemetry-publisher",
]


def check_services():
    print("=" * 65)
    print(" 1. SYSTEMD SERVICES STATUS")
    print("=" * 65)
    for svc in SERVICES:
        try:
            status = subprocess.check_output(
                ["systemctl", "is-active", svc], stderr=subprocess.STDOUT
            ).decode().strip()
        except Exception:
            status = "inactive / failed"
        icon = "🟢 ACTIVE" if status == "active" else "🔴 INACTIVE"
        print(f" {icon:<12} | {svc:<30}")


def check_wallet():
    print("\n" + "=" * 65)
    print(" 2. HOT WALLET (LIVE ON-CHAIN)")
    print("=" * 65)
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [HOT_WALLET]
    }).encode("utf-8")
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Botsensai/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            lamports = data.get("result", {}).get("value", 0)
            sol = lamports / 1e9
            print(f" Address : {HOT_WALLET}")
            print(f" Balance : {sol:.6f} SOL ({lamports:,} lamports)")
    except Exception as e:
        print(f" RPC Query Error: {e}")


def check_database():
    print("\n" + "=" * 65)
    print(" 3. PIPELINE & EXECUTION METRICS")
    print("=" * 65)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cursor = conn.cursor()

        # Counts
        cursor.execute("SELECT count(*) FROM launches;")
        total_launches = cursor.fetchone()[0]

        cursor.execute("SELECT count(*) FROM scores;")
        total_scores = cursor.fetchone()[0]

        cursor.execute("SELECT count(*) FROM execution_signals;")
        total_signals = cursor.fetchone()[0]

        cursor.execute("SELECT count(*) FROM copy_trade_events;")
        total_copy_events = cursor.fetchone()[0]

        cursor.execute("SELECT count(*) FROM top_traders;")
        total_top_traders = cursor.fetchone()[0]

        print(f" Launches Discovered      : {total_launches:,}")
        print(f" Tokens Scored            : {total_scores:,}")
        print(f" Top Smart Wallets Tracked: {total_top_traders:,}")
        print(f" Copy-Trade Events Caught : {total_copy_events:,}")
        print(f" Execution Signals Logged : {total_signals:,}")

        # Recent signals
        cursor.execute(
            "SELECT id, created_at, side, symbol, size_native, jito_tip_lamports, status, tx_hash "
            "FROM execution_signals ORDER BY id DESC LIMIT 6;"
        )
        signals = cursor.fetchall()
        print("\n [Latest Execution Signals]:")
        if signals:
            print(f" {'ID':<5} {'Created At':<19} {'Side':<5} {'Symbol':<10} {'Size (SOL)':<11} {'Tip (lamports)':<14} {'Status':<10} {'Tx Hash'}")
            print(" " + "-" * 95)
            for s in signals:
                sid, cat, side, sym, size, tip, st, tx = s
                dt_str = datetime.fromtimestamp(cat, timezone.utc).strftime("%H:%M:%S") if cat else "N/A"
                sym_str = str(sym or "UNK")[:9]
                size_str = f"{float(size or 0):.4f}"
                tip_str = f"{int(tip or 0):,}"
                tx_str = str(tx or "")
                if len(tx_str) > 22:
                    tx_str = tx_str[:10] + "..." + tx_str[-8:]
                print(f" #{sid:<4} {dt_str:<19} {side:<5} {sym_str:<10} {size_str:<11} {tip_str:<14} {st:<10} {tx_str}")
        else:
            print("  (No execution signals recorded yet)")

        # Live On-Chain Positions (Cryptographically confirmed on-chain Jito landings only)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS live_positions (
                mint TEXT PRIMARY KEY, symbol TEXT, token_key TEXT,
                entry_tx_hash TEXT NOT NULL, exit_tx_hash TEXT,
                amount_token REAL NOT NULL, cost_sol REAL NOT NULL,
                entry_price_sol REAL NOT NULL, peak_price_sol REAL NOT NULL,
                last_price_sol REAL NOT NULL, realized_pnl_sol REAL NOT NULL DEFAULT 0.0,
                opened_at REAL NOT NULL, closed_at REAL, status TEXT NOT NULL DEFAULT 'OPEN',
                exit_reason TEXT, updated_at REAL NOT NULL
            );
            """
        )
        cursor.execute(
            "SELECT symbol, mint, entry_price_sol, peak_price_sol, last_price_sol, cost_sol, realized_pnl_sol, status, amount_token "
            "FROM live_positions ORDER BY opened_at DESC LIMIT 8;"
        )
        positions = cursor.fetchall()
        print(f"\n [Live On-Chain Positions (Real Mainnet Swaps Only)]:")
        if positions:
            print(f" {'Symbol':<10} {'Status':<8} {'Entry (SOL)':<14} {'Peak (SOL)':<14} {'Tokens Held':<15} {'Realized PnL':<14} {'Mint'}")
            print(" " + "-" * 95)
            total_real_pnl = 0.0
            for p in positions:
                sym, mint, ep, pp, lp, cb, rpnl, st, amt = p
                sym_str = str(sym or "UNK")[:9]
                ep_str = f"{float(ep or 0):.8f}"
                pp_str = f"{float(pp or 0):.8f}"
                amt_str = f"{float(amt or 0):,.0f}" if st == "OPEN" else "0"
                rpnl_val = float(rpnl or 0)
                if st == "CLOSED":
                    total_real_pnl += rpnl_val
                pnl_str = f"{rpnl_val:+.4f} SOL"
                mint_short = f"{mint[:6]}...{mint[-6:]}" if mint and len(mint) > 12 else str(mint)
                print(f" {sym_str:<10} {st:<8} {ep_str:<14} {pp_str:<14} {amt_str:<15} {pnl_str:<14} {mint_short}")
            print(" " + "-" * 95)
            print(f" Real On-Chain Closed PnL: {total_real_pnl:+.4f} SOL (Zero paper simulation data)")
        else:
            print("  (No live on-chain positions recorded yet)")

        conn.close()
    except Exception as e:
        print(f" Database read error: {e}")


def check_recent_logs():
    print("\n" + "=" * 65)
    print(" 4. RECENT ACTIVITY (DAEMON & EXECUTOR)")
    print("=" * 65)
    print(" --- Latest Daemon Sweep/Event ---")
    try:
        out = subprocess.check_output(
            ["tail", "-n", "8", "/home/ubuntu/Botsensai/data/daemon.log"],
            stderr=subprocess.STDOUT
        ).decode().strip()
        print(out)
    except Exception as e:
        print(f" Error reading daemon.log: {e}")

    print("\n --- Latest Jito Executor Activity ---")
    try:
        out = subprocess.check_output(
            ["tail", "-n", "8", "/home/ubuntu/Botsensai/data/executor.log"],
            stderr=subprocess.STDOUT
        ).decode().strip()
        print(out)
    except Exception as e:
        print(f" Error reading executor.log: {e}")


if __name__ == "__main__":
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n=================================================================")
    print(f"   BOTSENSAI 2.0 LIVE STATUS MONITOR | {now_str}")
    print(f"=================================================================")
    check_services()
    check_wallet()
    check_database()
    check_recent_logs()
    print("\n" + "=" * 65 + "\n")
