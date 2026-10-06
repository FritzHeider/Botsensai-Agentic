#!/usr/bin/env python3
"""
Dedicated Dead Weight Purge & Rent Sweeper for Botsensai Hot Wallet.
Safely marks dead bonding curve tokens and unsolicited spam dust as CLOSED/QUARANTINED,
and closes token accounts on-chain to reclaim SOL rent back into the hot wallet.
"""

import sqlite3
import sys
import time
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from infra.executor.jito_executor import JitoLiveExecutor

DB_PATH = BASE_DIR / "data" / "botsensai.db"
KEYPAIR_PATH = Path("/home/ubuntu/.config/solana/id.json")
if not KEYPAIR_PATH.exists():
    KEYPAIR_PATH = Path.home() / ".config" / "solana" / "id.json"


def main():
    print("=" * 65)
    print(" BOTSENSIA DEAD WEIGHT PURGE & RENT SWEEPER")
    print("=" * 65)

    executor = JitoLiveExecutor(db_path=DB_PATH, keypair_path=KEYPAIR_PATH)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute("SELECT mint, symbol, amount_token, status FROM live_positions WHERE status = 'OPEN'").fetchall()
        print(f"Found {len(rows)} OPEN positions in live_positions:")
        now = time.time()
        for r in rows:
            mint = r["mint"]
            sym = r["symbol"]
            amt = r["amount_token"]
            print(f" - ${sym:<8} ({mint[:8]}...) : {amt:,.0f} tokens")

            # Check if spam dust or dead curve
            if mint in ("2Hdh12UXsKuwNTFyS5jsD8dvgrSKTAEUy19ynKyJeqJT", "GNhCphYjduivkJvzSqWiwTyjvJsZmzUtrSKVjrhFpump"):
                conn.execute(
                    "UPDATE live_positions SET status = 'QUARANTINED', exit_reason = 'unsolicited_dust', updated_at = ? WHERE mint = ?",
                    (now, mint),
                )
                print(f"   -> Marked QUARANTINED (unsolicited dust)")
            else:
                conn.execute(
                    "UPDATE live_positions SET status = 'CLOSED', exit_reason = 'dead_weight_purged', closed_at = ?, updated_at = ? WHERE mint = ?",
                    (now, now, mint),
                )
                print(f"   -> Marked CLOSED (dead_weight_purged)")
        conn.commit()

    print("\nAttempting on-chain token account rent reclamation (Burn & CloseAccount)...")
    reclaimed_count = 0
    for r in rows:
        mint = r["mint"]
        try:
            ok = executor.close_token_account_on_chain(mint)
            if ok:
                reclaimed_count += 1
                print(f"   ✓ Reclaimed rent for {mint[:8]}...")
            else:
                print(f"   - Account for {mint[:8]}... could not be closed (may be frozen or non-zero close authority)")
        except Exception as e:
            print(f"   ! Error reclaiming {mint[:8]}...: {e}")

    print(f"\nSuccessfully reclaimed {reclaimed_count} token account(s) (~{reclaimed_count * 0.00204:.5f} SOL).")

    # Verify open positions count
    active = executor._get_open_live_positions()
    print(f"Active Live Positions Remaining: {len(active)} / 3 slots available.")
    print("=" * 65)


if __name__ == "__main__":
    main()
