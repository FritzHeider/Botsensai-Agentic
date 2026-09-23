#!/usr/bin/env python3
"""Liquidate dormant floor bags into liquid SOL via atomic Jito MEV bundles.

Preserves active runners ($BASA).
Reclaims liquid SOL from flat bonding curve positions.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Add project roots
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "src"))

from infra.executor.jito_executor import JitoLiveExecutor

DORMANT_MINTS = [
    ("CRASHCAT", "7h38uqqwLS2duteXnUSofetrNqrpX1A31y7nVqdpump"),
    ("STONE", "5RGEqBK9AnV4wkfBbocTyP3YJrn6xnsgF5NJbTcXpump"),
    ("MERROW", "PDb1Gy71LFNwKKuSXmtKp4jxuPZgp2Xne4gohqkpump"),
    ("SEED", "CJjv3RETCkM1tSvWmU2dzwgyf6oGkcM4RSh2hVADe5pv"),
    ("z/p", "617f4GKo5qMKsMDG8S368gdaXsP23DBYnVNNv85gpump"),
    ("Josie", "9zUY6AT3KoYBygSEN72A95FGgBdz7TKp8iDchwnsQamC"),
    ("CAIZO", "H4vDQVE31JhFK91chVQDWakrFsfRpN1uzyqwSzuepump"),
    ("WAFFLES", "J4yXEJyf4oPvreJzxQ42x3LjZbbxW9LZQVGdfGfhpump"),
    ("Angel", "69kxBTxN13ftKTnNgnzUmHHj229yfZtrVoRvZKkmpump"),
    ("zLaunch", "7LWqbVxqsGLBGNNTVk26JEsjoXocgeqgVh4i6XACWGSE"),
]


def run_liquidation() -> None:
    print("=" * 70)
    print("   BOTSENSAI DORMANT BAG LIQUIDATOR (JITO MEV ATOMIC BUNDLES)")
    print("=" * 70)

    executor = JitoLiveExecutor()
    if not executor.keypair:
        print("[LIQUIDATOR] ❌ Hot wallet keypair not loaded. Aborting.")
        sys.exit(1)

    initial_sol = executor.get_wallet_balance_sol()
    print(f"[LIQUIDATOR] Hot Wallet: {executor.keypair.pubkey()}")
    print(f"[LIQUIDATOR] Starting Liquid SOL: {initial_sol:.6f} SOL\n")

    liquidated_count = 0
    total_reclaimed = 0.0

    for sym, mint in DORMANT_MINTS:
        token_bal = executor.get_token_balance(mint)
        if token_bal <= 0:
            print(f"[-] {sym:10} ({mint[:8]}...): Balance is 0. Skipping.")
            continue

        print(f"\n[+] Liquidating {sym} ({token_bal:,.2f} tokens)...")
        tx, pool_type = executor.build_swap_transaction(
            side="SELL",
            mint=mint,
            size_sol=0.0,
            slippage_bps=400,  # 4% slippage for full exit
            priority_fee_sol=0.0005,
        )

        if not tx:
            print(f"  ❌ Swap generation failed for {sym}: {pool_type}")
            continue

        try:
            signed_tx = executor.sign_transaction(tx)
        except Exception as e:
            print(f"  ❌ Signing failed for {sym}: {e}")
            continue

        success, sig = executor.dispatch_jito_bundle(signed_tx)
        if success:
            print(f"  🟢 Confirmed on-chain ({pool_type}): {sig}")
            liquidated_count += 1
            time.sleep(2.5)  # Allow block confirmation
        else:
            print(f"  ❌ Bundle failed for {sym}: {sig}")

    final_sol = executor.get_wallet_balance_sol()
    diff_sol = final_sol - initial_sol
    print("\n" + "=" * 70)
    print(f"[LIQUIDATOR] Complete! Liquidated: {liquidated_count} tokens")
    print(f"[LIQUIDATOR] Prior Balance: {initial_sol:.6f} SOL")
    print(f"[LIQUIDATOR] New Balance:   {final_sol:.6f} SOL")
    print(f"[LIQUIDATOR] Net Change:    {diff_sol:+.6f} SOL")
    print("=" * 70)


if __name__ == "__main__":
    run_liquidation()
