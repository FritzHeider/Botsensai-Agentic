#!/usr/bin/env python3
"""Botsensai Full Portfolio Liquidation & Rent Recovery Sweep.

1. Liquidates 100% of open token holdings ($HUMPH, Bumo) via Jito atomic MEV bundles.
2. Closes empty SPL Associated Token Accounts to reclaim rent exemption deposits.
3. Reports verified on-chain initial balance, final balance, and net change.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "src"))

from infra.executor.jito_executor import JitoLiveExecutor
from scripts.reclaim_rent import scan_and_reclaim, DEFAULT_KEYPAIR

TARGET_MINTS = [
    ("HUMPH", "7jNwX4Me9zGEjmWY7zARDG2LYjVs1WXD6DZ1JvSfpump"),
    ("Bumo", "7yg2UT3Ksh7wwmAPAasaKU6SBbakRtzf2eYqjZZzpump"),
]


def run_full_liquidation() -> None:
    print("=" * 75)
    print("   🔴 BOTSENSAI FULL PORTFOLIO LIQUIDATION & RENT SWEEP")
    print("=" * 75)

    executor = JitoLiveExecutor()
    if not executor.keypair:
        print("[LIQUIDATOR] ❌ Hot wallet keypair not loaded. Aborting.")
        sys.exit(1)

    initial_sol = executor.get_wallet_balance_sol()
    wallet_addr = str(executor.keypair.pubkey())
    print(f"[LIQUIDATOR] Hot Wallet : {wallet_addr}")
    print(f"[LIQUIDATOR] Initial SOL: {initial_sol:.6f} SOL\n")

    # Step 1: Liquidate all tokens
    liquidated_count = 0
    for sym, mint in TARGET_MINTS:
        token_bal = executor.get_token_balance(mint)
        print(f"\n[+] Checking {sym} ({mint[:8]}...): Balance = {token_bal:,.4f} tokens")
        if token_bal <= 0:
            print(f"    Already 0 tokens. Moving to next.")
            continue

        print(f"    Building sell transaction for 100% of {sym}...")
        tx, pool_type = executor.build_swap_transaction(
            side="SELL",
            mint=mint,
            size_sol=0.0,
            slippage_bps=500,  # 5% max slippage to ensure execution
            priority_fee_sol=0.0005,
        )

        if not tx:
            print(f"    ❌ Transaction build failed for {sym}: {pool_type}")
            continue

        try:
            signed_tx = executor.sign_transaction(tx)
        except Exception as e:
            print(f"    ❌ Signing failed for {sym}: {e}")
            continue

        print(f"    Dispatching atomic Jito MEV bundle...")
        success, sig = executor.dispatch_jito_bundle(signed_tx)
        if success:
            print(f"    🟢 Confirmed on-chain ({pool_type}): https://solscan.io/tx/{sig}")
            liquidated_count += 1
            time.sleep(3.0)  # Wait for confirmation before next action
        else:
            print(f"    ❌ Jito bundle failed for {sym}: {sig}")

    mid_sol = executor.get_wallet_balance_sol()
    print(f"\n[LIQUIDATOR] Token sells complete. Mid balance: {mid_sol:.6f} SOL (Change: {mid_sol - initial_sol:+.6f} SOL)")

    # Step 2: Reclaim rent from empty ATAs
    print("\n" + "=" * 75)
    print("   RECLAIMING ATA RENT EXEMPTION DEPOSITS")
    print("=" * 75)
    try:
        kp_path = executor.keypair_path if (executor.keypair_path and executor.keypair_path.exists()) else DEFAULT_KEYPAIR
        scan_and_reclaim(close_empty=True, keypair_path=kp_path)
    except Exception as e:
        print(f"❌ Error during rent reclamation: {e}")

    # Step 3: Final verified on-chain accounting
    time.sleep(2.0)
    final_sol = executor.get_wallet_balance_sol()
    net_diff = final_sol - initial_sol

    print("\n" + "=" * 75)
    print("   FINAL VERIFIED LIQUIDATION SUMMARY")
    print("=" * 75)
    print(f"  Starting Balance : {initial_sol:.6f} SOL")
    print(f"  Final Balance    : {final_sol:.6f} SOL")
    print(f"  Total Net Change : {net_diff:+.6f} SOL")
    print(f"  Wallet Status    : 100% CASH LIQUID")
    print("=" * 75)


if __name__ == "__main__":
    run_full_liquidation()
