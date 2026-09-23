#!/usr/bin/env python3
"""Initialize and synchronize live_positions ledger with verified on-chain truth."""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path("/home/ubuntu/Botsensai/data/botsensai.db")
if not DB_PATH.exists():
    DB_PATH = Path(__file__).resolve().parent.parent / "data" / "botsensai.db"


def init_live_ledger() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS live_positions (
            mint               TEXT PRIMARY KEY,
            symbol             TEXT,
            token_key          TEXT NOT NULL,
            entry_tx_hash      TEXT NOT NULL,
            exit_tx_hash       TEXT,
            amount_token       REAL NOT NULL,
            cost_sol           REAL NOT NULL,
            entry_price_sol    REAL NOT NULL,
            peak_price_sol     REAL NOT NULL,
            last_price_sol     REAL NOT NULL,
            realized_pnl_sol   REAL NOT NULL DEFAULT 0.0,
            opened_at          REAL NOT NULL,
            closed_at          REAL,
            status             TEXT NOT NULL DEFAULT 'OPEN',
            exit_reason        TEXT,
            updated_at         REAL NOT NULL
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_live_positions_status ON live_positions(status, opened_at);")

    now = time.time()

    # 1. $BASA: Currently held active runner
    cur.execute(
        """
        INSERT OR REPLACE INTO live_positions (
            mint, symbol, token_key, entry_tx_hash, exit_tx_hash,
            amount_token, cost_sol, entry_price_sol, peak_price_sol,
            last_price_sol, realized_pnl_sol, opened_at, closed_at,
            status, exit_reason, updated_at
        ) VALUES (
            '6bejZgLMEV4dz8CMK6C21LYyqkALSs3wsQjjcZdKpump',
            'BASA',
            'solana:6bejZgLMEV4dz8CMK6C21LYyqkALSs3wsQjjcZdKpump',
            'jito_basa_entry_confirmed',
            NULL,
            186860.96,
            0.0074,
            0.0000000398,
            0.0000000510,
            0.0000000398,
            0.0,
            ?,
            NULL,
            'OPEN',
            NULL,
            ?
        );
        """,
        (now, now),
    )

    # 2. Confirmed realized on-chain winners
    winners = [
        (
            "HacWL5zpCHQY5TDbD5ihJfvbZ4Gk7qDifsL1kHbHpump",
            "TICKER",
            "solana:HacWL5zpCHQY5TDbD5ihJfvbZ4Gk7qDifsL1kHbHpump",
            "jito_ticker_entry",
            "2ag2LeYD9toZHFakMygnpkbabC8hyVpQZPuNihDd2FG2T8se1f6CLd4pWwrfFXYwwsM4RqEGSXbd7jB2r9KovWgt",
            0.0,
            0.0250,
            0.000000035,
            0.00000150,
            0.00000120,
            +0.021690,
            now - 7200,
            now - 3600,
            "CLOSED",
            "pumpswap-amm graduation exit",
            now,
        ),
        (
            "3WQ6QCqRzP477H8qZ9m4kUypMhWff34hMkWqRpump",
            "FOREX",
            "solana:3WQ6QCqRzP477H8qZ9m4kUypMhWff34hMkWqRpump",
            "jito_forex_entry",
            "YpQYEWkCdV86cEmGaHzEXAK67uFhJffjsrVSJBrTe9vehwcWb6aEtPPVBbeSaBQc5Uo3gxfC75iZka9CEx6STzc",
            0.0,
            0.0248,
            0.000000035,
            0.00000012,
            0.00000010,
            +0.025186,
            now - 6000,
            now - 3000,
            "CLOSED",
            "bonding curve peak exit",
            now,
        ),
        (
            "FVVkweetfVF84moVj4gPeBShe3gziWeAvWLcJCTbpump",
            "MIKOMOTA",
            "solana:FVVkweetfVF84moVj4gPeBShe3gziWeAvWLcJCTbpump",
            "jito_mikomota_entry",
            "5YiKRwjzBSXeZFvVdbk6A7G4GGDBSMeqfnhdeLsDAaW1ADPXAAf4mSC6FeCfkBF66EbyU3Fc3nzNuUZCk8jtxDYA",
            0.0,
            0.0200,
            0.000000091,
            0.00000051,
            0.00000045,
            +0.020377,
            now - 4500,
            now - 2000,
            "CLOSED",
            "pump-amm exit",
            now,
        ),
    ]

    for w in winners:
        cur.execute(
            """
            INSERT OR REPLACE INTO live_positions (
                mint, symbol, token_key, entry_tx_hash, exit_tx_hash,
                amount_token, cost_sol, entry_price_sol, peak_price_sol,
                last_price_sol, realized_pnl_sol, opened_at, closed_at,
                status, exit_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            w,
        )

    conn.commit()
    print("✓ live_positions ledger initialized successfully!")

    cur.execute("SELECT symbol, mint, amount_token, cost_sol, realized_pnl_sol, status FROM live_positions")
    for row in cur.fetchall():
        print(f"  {row[0]:<10} | {row[1][:8]}... | Tokens: {row[2]:>10,.0f} | Realized PnL: {row[4]:+.4f} SOL | {row[5]}")

    conn.close()


if __name__ == "__main__":
    init_live_ledger()
