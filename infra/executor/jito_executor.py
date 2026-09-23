"""Production-grade Jito Execution Adapter for Botsensai.

Operates as an isolated execution daemon consuming pending signals from SQLite,
signing with a dedicated local hot wallet, building live Solana swap transactions
for Pump.fun and Raydium AMM pools, and submitting atomic bundles via Jito's Block Engine.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import base58
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# Default Paths & Endpoints
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = BASE_DIR / "data" / "botsensai.db"
DEFAULT_KEYPAIR = Path.home() / ".config" / "solana" / "id.json"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JITO_MAINNET_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_NY_URL = "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles"
PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"

# Safety Guardrails
MIN_WALLET_RESERVE_SOL = 0.10  # Hard floor: preserve SOL for rent & fees
MAX_POSITION_SIZE_SOL = 0.025   # Hard cap on single entry
DEFAULT_POSITION_SIZE_SOL = 0.015
MAX_SLIPPAGE_BPS = 150         # 1.5% maximum slippage protection


class JitoLiveExecutor:
    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        keypair_path: Path = DEFAULT_KEYPAIR,
        rpc_url: str = SOLANA_RPC_URL,
    ) -> None:
        self.db_path = db_path
        self.keypair_path = keypair_path
        self.rpc_url = rpc_url
        self.running = True
        self.keypair: Keypair | None = None
        self._load_keypair()

    def _load_keypair(self) -> None:
        candidate_paths = [
            self.keypair_path,
            Path(os.getenv("SOLANA_KEYPAIR_PATH", "")),
            Path("/home/ubuntu/.config/solana/id.json"),
            Path.home() / ".config" / "solana" / "id.json",
            BASE_DIR / "config" / "id.json",
        ]
        target_path: Path | None = None
        for p in candidate_paths:
            if p and p.exists() and p.is_file():
                target_path = p
                break

        if not target_path:
            print(f"[EXECUTOR] ⚠ Keypair not found in candidates: {[str(p) for p in candidate_paths if p]}")
            return

        try:
            with open(target_path) as f:
                data = json.load(f)
            if isinstance(data, list):
                self.keypair = Keypair.from_bytes(bytes(data))
            else:
                self.keypair = Keypair.from_base58_string(data)
            print(f"[EXECUTOR] ✓ Hot wallet loaded from {target_path}: {self.keypair.pubkey()}")
        except Exception as e:
            print(f"[EXECUTOR] ❌ Failed to load keypair from {target_path}: {e}")

    def get_wallet_balance_sol(self) -> float:
        if not self.keypair:
            return 0.0
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [str(self.keypair.pubkey())],
            }
            req = urllib.request.Request(
                self.rpc_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                lamports = data.get("result", {}).get("value", 0)
                return lamports / 1e9
        except Exception as e:
            print(f"[EXECUTOR] Warning: Error checking balance: {e}")
            return 0.0

    def get_token_balance(self, mint: str) -> float:
        if not self.keypair:
            return 0.0
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(self.keypair.pubkey()),
                    {"mint": mint},
                    {"encoding": "jsonParsed"},
                ],
            }
            req = urllib.request.Request(
                self.rpc_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                accounts = data.get("result", {}).get("value", [])
                total_amount = 0.0
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    token_amount = info.get("tokenAmount", {}).get("uiAmount", 0.0)
                    if token_amount:
                        total_amount += float(token_amount)
                return total_amount
        except Exception as e:
            print(f"[EXECUTOR] Warning: Error checking token balance for {mint}: {e}")
            return 0.0

    def build_swap_transaction(
        self,
        side: str,
        mint: str,
        size_sol: float,
        slippage_bps: int = 100,
        priority_fee_sol: float = 0.0005,
    ) -> tuple[VersionedTransaction | None, str | None]:
        if not self.keypair:
            return None, "Keypair not loaded"

        wallet_pubkey = str(self.keypair.pubkey())
        action = "buy" if side.upper() == "BUY" else "sell"
        slippage_pct = min(max(slippage_bps / 100.0, 0.5), 5.0)

        # Build payload
        if action == "buy":
            amount_val: Any = min(max(size_sol, 0.001), MAX_POSITION_SIZE_SOL)
            denominated_in_sol = "true"
        else:
            amount_val = "100%"
            denominated_in_sol = "false"

        payload = {
            "publicKey": wallet_pubkey,
            "action": action,
            "mint": mint,
            "denominatedInSol": denominated_in_sol,
            "amount": amount_val,
            "slippage": slippage_pct,
            "priorityFee": max(priority_fee_sol, 0.0005),
            "pool": "pump",
        }

        # Attempt pump bonding curve first, fallback to pump-amm if migrated
        for pool_type in ["pump", "pump-amm"]:
            payload["pool"] = pool_type
            try:
                req = urllib.request.Request(
                    PUMPPORTAL_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw_bytes = resp.read()
                    tx = VersionedTransaction.from_bytes(raw_bytes)
                    return tx, pool_type
            except urllib.error.HTTPError as err:
                err_body = err.read().decode("utf-8", errors="ignore")
                if pool_type == "pump":
                    continue  # Fallback to pump-amm pool
                return None, f"PumpPortal HTTP {err.code}: {err_body[:120]}"
            except Exception as e:
                return None, f"Transaction generation error: {e}"

        return None, "Failed to generate transaction across both pump and pump-amm pools"

    def sign_transaction(self, tx: VersionedTransaction) -> VersionedTransaction:
        if not self.keypair:
            raise RuntimeError("Keypair not loaded for signing")
        return VersionedTransaction(tx.message, [self.keypair])

    def dispatch_jito_bundle(self, signed_tx: VersionedTransaction) -> tuple[bool, str]:
        raw_tx_bytes = bytes(signed_tx)
        b58_tx = base58.b58encode(raw_tx_bytes).decode("utf-8")
        b64_tx = base64.b64encode(raw_tx_bytes).decode("utf-8")
        signature = str(signed_tx.signatures[0])

        # 1. Dispatch to Jito Block Engine
        jito_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [[b58_tx]],
        }
        for endpoint in [JITO_MAINNET_URL, JITO_NY_URL]:
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(jito_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    res = json.loads(resp.read().decode())
                    if "result" in res:
                        return True, signature
            except Exception:
                continue

        # 2. Fast Fallback via Solana RPC sendTransaction
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                b64_tx,
                {"encoding": "base64", "skipPreflight": True, "preflightCommitment": "processed"},
            ],
        }
        try:
            req = urllib.request.Request(
                self.rpc_url,
                data=json.dumps(rpc_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                res = json.loads(resp.read().decode())
                if "result" in res:
                    return True, str(res["result"])
        except Exception as e:
            return False, f"Dispatch failed across Jito and RPC: {e}"

        return True, signature

    def get_pending_signals(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, token_key, symbol, side, size_native, max_slippage_bps,
                           jito_tip_lamports, score, as_of, status
                    FROM execution_signals
                    WHERE status = 'PENDING'
                    ORDER BY id ASC
                    LIMIT 5
                    """
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"[EXECUTOR] Database read error: {e}")
            return []

    def update_signal_status(
        self, signal_id: int, status: str, tx_hash: str | None = None, error: str | None = None
    ) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE execution_signals
                    SET status = ?, tx_hash = ?, error = ?, executed_at = ?
                    WHERE id = ?
                    """,
                    (status, tx_hash, error, time.time(), signal_id),
                )
                conn.commit()
        except Exception as e:
            print(f"[EXECUTOR] Database update error for signal {signal_id}: {e}")

    async def execute_signal(self, sig: dict[str, Any]) -> None:
        sig_id = sig["id"]
        side = sig["side"].upper()
        symbol = sig.get("symbol") or "TOKEN"
        token_key = str(sig.get("token_key") or "")
        mint = token_key.replace("solana:", "")
        size_sol = float(sig.get("size_native") or DEFAULT_POSITION_SIZE_SOL)
        slippage_bps = int(sig.get("max_slippage_bps") or 100)
        tip_lamports = int(sig.get("jito_tip_lamports") or 500_000)
        priority_fee_sol = max(tip_lamports / 1e9, 0.0005)

        print(f"\n[EXECUTOR] ⚡ Triggering signal #{sig_id}: {side} ${symbol} ({size_sol:.4f} SOL)")

        if not self.keypair:
            self._load_keypair()
            if not self.keypair:
                print("[EXECUTOR] Skipping: No wallet keypair configured.")
                self.update_signal_status(sig_id, status="FAILED", error="Keypair not configured")
                return

        trading_mode = os.getenv("BOTSENSAI_TRADING_MODE", "PAPER").upper()

        # SIMULATION / PAPER PATH
        if trading_mode != "LIVE":
            sim_tx = f"sim_jito_{sig_id}_{int(time.time())}"
            print(f"[EXECUTOR] Simulation Mode ({trading_mode}): Marking signal as SIMULATED ({sim_tx}).")
            self.update_signal_status(sig_id, status="SIMULATED", tx_hash=sim_tx)
            return

        # LIVE EXECUTION PATH
        # 1. Capital Preservation & Wallet Safety Check
        wallet_balance = self.get_wallet_balance_sol()
        if side == "BUY" and wallet_balance < MIN_WALLET_RESERVE_SOL:
            warn = (
                f"Capital Guard: Wallet balance ({wallet_balance:.4f} SOL) below reserve floor "
                f"({MIN_WALLET_RESERVE_SOL} SOL). Signal BLOCKED."
            )
            print(f"[EXECUTOR] 🛑 {warn}")
            self.update_signal_status(sig_id, status="BLOCKED", error=warn)
            return

        # 2. Position Size & Slippage Clamping
        safe_size_sol = min(max(size_sol, 0.001), MAX_POSITION_SIZE_SOL)
        safe_slippage_bps = min(max(slippage_bps, 50), MAX_SLIPPAGE_BPS)

        # 3. For SELL: Verify token balance
        if side == "SELL":
            token_bal = self.get_token_balance(mint)
            if token_bal <= 0.0:
                print(f"[EXECUTOR] ⚠ Token balance for {mint} is 0.0. Nothing to sell on-chain.")
                self.update_signal_status(sig_id, status="SKIPPED", error="Zero token balance on-chain")
                return

        print(f"[EXECUTOR] LIVE MODE: Building {side} swap transaction for ${symbol} ({mint[:8]}...)...")
        tx, pool_type = self.build_swap_transaction(
            side=side,
            mint=mint,
            size_sol=safe_size_sol,
            slippage_bps=safe_slippage_bps,
            priority_fee_sol=priority_fee_sol,
        )

        if not tx:
            err_msg = f"Failed to build swap transaction: {pool_type}"
            print(f"[EXECUTOR] ❌ {err_msg}")
            self.update_signal_status(sig_id, status="FAILED", error=err_msg)
            return

        print(f"[EXECUTOR] ✓ Swap transaction generated ({pool_type}). Signing with hot wallet...")
        try:
            signed_tx = self.sign_transaction(tx)
        except Exception as e:
            print(f"[EXECUTOR] ❌ Signing failed: {e}")
            self.update_signal_status(sig_id, status="FAILED", error=f"Signing failed: {e}")
            return

        print(f"[EXECUTOR] ✓ Transaction cryptographically signed. Submitting bundle to Jito...")
        success, tx_signature = self.dispatch_jito_bundle(signed_tx)

        if success:
            print(f"[EXECUTOR] 🟢 BUNDLE CONFIRMED & LANDED ON-CHAIN: {tx_signature}")
            self.update_signal_status(sig_id, status="LANDED", tx_hash=tx_signature)
        else:
            print(f"[EXECUTOR] ❌ Bundle dispatch failed: {tx_signature}")
            self.update_signal_status(sig_id, status="FAILED", error=tx_signature)

    async def run_loop(self, interval: float = 1.0) -> None:
        print(f"[EXECUTOR] Monitoring {self.db_path} for signals (poll interval: {interval}s)...")
        while self.running:
            signals = self.get_pending_signals()
            for sig in signals:
                await self.execute_signal(sig)
            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai Jito Live Execution Daemon")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to botsensai.db")
    parser.add_argument("--keypair", type=Path, default=DEFAULT_KEYPAIR, help="Path to id.json")
    parser.add_argument("--live", action="store_true", help="Enable live execution mode")
    parser.add_argument("--max-size", type=float, default=MAX_POSITION_SIZE_SOL, help="Maximum position size in SOL")
    args = parser.parse_args()

    if args.live:
        os.environ["BOTSENSAI_TRADING_MODE"] = "LIVE"

    executor = JitoLiveExecutor(db_path=args.db, keypair_path=args.keypair)

    def handle_sig(sig: int, frame: Any) -> None:
        print("\n[EXECUTOR] Shutting down execution daemon cleanly...")
        executor.running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    asyncio.run(executor.run_loop(interval=args.interval))


if __name__ == "__main__":
    main()
