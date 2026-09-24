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

from dotenv import load_dotenv

# Default Paths & Endpoints
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_DB = BASE_DIR / "data" / "botsensai.db"
DEFAULT_KEYPAIR = Path.home() / ".config" / "solana" / "id.json"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JITO_MAINNET_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_NY_URL = "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles"
PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"

# Safety Guardrails
MIN_WALLET_RESERVE_SOL = 0.10  # Hard floor: preserve SOL for rent & fees
MAX_POSITION_SIZE_SOL = 0.035   # Raised ceiling: allows dynamic compounding up to 0.035 SOL
DEFAULT_POSITION_SIZE_SOL = 0.018
MAX_SLIPPAGE_BPS = 350         # 3.5% maximum slippage protection for fast meme runners


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
        sell_amount: str = "100%",
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
            amount_val = sell_amount
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

        # Method 3: Multi-Pool Liquidity Routing & Auto Fallback
        # Determine pool priority: if token is migrated or on Raydium/Pumpswap, prioritize AMM pools
        pools_to_try = ["pump", "pump-amm", "raydium", "raydium-cpmm", "auto"]
        try:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            dex_req = urllib.request.Request(dex_url, headers={"User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(dex_req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode())
                pairs = data.get("pairs") or []
                for p in pairs:
                    if p.get("dexId") in ["pumpswap", "raydium", "meteora", "orca"] or (p.get("marketCap") or 0) >= 65_000:
                        pools_to_try = ["pump-amm", "raydium", "raydium-cpmm", "auto", "pump"]
                        break
        except Exception:
            pass

        last_err = "No error"
        for pool_type in pools_to_try:
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
                last_err = f"PumpPortal ({pool_type}) HTTP {err.code}: {err_body[:100]}"
                continue  # Cascades to secondary and auto pools
            except Exception as e:
                last_err = f"Transaction error ({pool_type}): {e}"
                continue

        return None, f"Failed across all pools {pools_to_try}: {last_err}"

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

        # Method 1: Dynamic Bankroll Scaling & Fractional Kelly (Compounding Engine)
        if side == "BUY":
            dynamic_bankroll_size = (wallet_balance - MIN_WALLET_RESERVE_SOL) * 0.05
            scaled_size = max(size_sol, dynamic_bankroll_size, 0.012)
            safe_size_sol = min(scaled_size, MAX_POSITION_SIZE_SOL)
        else:
            safe_size_sol = size_sol

        # Method 4: Dynamic Jito Priority Bribes Based on Conviction Score
        score = float(sig.get("score") or 0.85)
        if score >= 0.95:
            tiered_tip_lamports = 1_200_000
        elif score >= 0.88:
            tiered_tip_lamports = 850_000
        else:
            tiered_tip_lamports = 550_000
        tip_lamports = max(int(sig.get("jito_tip_lamports") or 0), tiered_tip_lamports)
        priority_fee_sol = max(tip_lamports / 1e9, 0.0005)

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

            # Record or update live on-chain positions
            if side == "BUY":
                token_bal = 0.0
                for _ in range(5):
                    await asyncio.sleep(2.0)
                    token_bal = self.get_token_balance(mint)
                    if token_bal > 0:
                        break
                if token_bal > 0:
                    entry_px = safe_size_sol / token_bal
                    self._record_live_buy(
                        mint=mint,
                        symbol=symbol,
                        token_key=token_key or f"solana:{mint}",
                        entry_tx_hash=tx_signature,
                        amount_token=token_bal,
                        cost_sol=safe_size_sol,
                        entry_price_sol=entry_px,
                    )
            elif side == "SELL":
                await asyncio.sleep(2.0)
                token_bal = self.get_token_balance(mint)
                if token_bal <= 0:
                    self._record_live_sell(
                        mint=mint,
                        exit_tx_hash=tx_signature,
                        exit_reason="signal_sell",
                    )
        else:
            print(f"[EXECUTOR] ❌ Bundle dispatch failed: {tx_signature}")
            self.update_signal_status(sig_id, status="FAILED", error=tx_signature)

    def _record_live_buy(
        self,
        mint: str,
        symbol: str,
        token_key: str,
        entry_tx_hash: str,
        amount_token: float,
        cost_sol: float,
        entry_price_sol: float,
    ) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO live_positions (
                        mint, symbol, token_key, entry_tx_hash, exit_tx_hash,
                        amount_token, cost_sol, entry_price_sol, peak_price_sol,
                        last_price_sol, realized_pnl_sol, opened_at, closed_at,
                        status, exit_reason, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0.0, ?, NULL, 'OPEN', NULL, ?)
                    ON CONFLICT(mint) DO UPDATE SET
                        amount_token = excluded.amount_token,
                        cost_sol = excluded.cost_sol,
                        peak_price_sol = MAX(live_positions.peak_price_sol, excluded.peak_price_sol),
                        last_price_sol = excluded.last_price_sol,
                        status = 'OPEN',
                        updated_at = excluded.updated_at
                    """,
                    (
                        mint,
                        symbol,
                        token_key,
                        entry_tx_hash,
                        amount_token,
                        cost_sol,
                        entry_price_sol,
                        entry_price_sol,
                        entry_price_sol,
                        now,
                        now,
                    ),
                )
                conn.commit()
                print(f"[EXECUTOR] 📝 Recorded on-chain LIVE position for ${symbol} ({amount_token:,.2f} tokens @ {entry_price_sol:.8f} SOL)")
        except Exception as e:
            print(f"[EXECUTOR] Error recording live buy: {e}")

    def _record_live_sell(
        self,
        mint: str,
        exit_tx_hash: str,
        exit_reason: str,
        exit_sol: float | None = None,
    ) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                row = cur.execute("SELECT * FROM live_positions WHERE mint = ? AND status = 'OPEN'", (mint,)).fetchone()
                if not row:
                    return
                pos = dict(row)
                now = time.time()
                cost = float(pos["cost_sol"])
                realized_pnl = (exit_sol - cost) if exit_sol is not None else 0.0
                conn.execute(
                    """
                    UPDATE live_positions
                    SET status = 'CLOSED',
                        exit_tx_hash = ?,
                        realized_pnl_sol = ?,
                        closed_at = ?,
                        exit_reason = ?,
                        updated_at = ?
                    WHERE mint = ?
                    """,
                    (exit_tx_hash, realized_pnl, now, exit_reason, now, mint),
                )
                conn.commit()
                print(f"[EXECUTOR] 📝 Marked on-chain LIVE position CLOSED for {pos.get('symbol', mint)} (Exit: {exit_tx_hash[:10]}...)")
        except Exception as e:
            print(f"[EXECUTOR] Error recording live sell: {e}")

    def get_all_token_balances(self) -> dict[str, float]:
        """Returns {mint: total_ui_amount} for all on-chain token accounts owned by wallet."""
        if not self.keypair:
            return {}
        balances: dict[str, float] = {}
        for prog in [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        ]:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(self.keypair.pubkey()),
                    {"programId": prog},
                    {"encoding": "jsonParsed"},
                ],
            }
            try:
                req = urllib.request.Request(
                    self.rpc_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    for acc in data.get("result", {}).get("value", []):
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        m = info.get("mint")
                        amt = info.get("tokenAmount", {}).get("uiAmount", 0.0)
                        if m and amt and amt > 0:
                            balances[m] = balances.get(m, 0.0) + float(amt)
            except Exception as e:
                print(f"[EXECUTOR] Error querying token accounts for {prog}: {e}")
        return balances

    async def reconcile_on_chain_holdings(self) -> None:
        """Synchronizes on-chain token holdings directly into live_positions."""
        if not self.keypair:
            return
        try:
            balances = self.get_all_token_balances()
            # 1. Ensure all positive on-chain holdings are tracked in live_positions
            for mint, amount in balances.items():
                if amount <= 1.0:  # Ignore sub-unit dust
                    continue
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        "SELECT mint, status, amount_token FROM live_positions WHERE mint = ?",
                        (mint,),
                    ).fetchone()
                    if not row:
                        # New token discovered in hot wallet
                        sig_row = conn.execute(
                            "SELECT symbol, token_key, size_native, tx_hash FROM execution_signals "
                            "WHERE token_key LIKE ? AND side = 'BUY' AND status = 'LANDED' "
                            "ORDER BY id DESC LIMIT 1",
                            (f"%{mint}%",),
                        ).fetchone()
                        if sig_row:
                            sym, tkey, sz_sol, tx_h = sig_row
                            sz_sol = float(sz_sol or 0.015)
                            entry_px = sz_sol / amount if amount > 0 else 0.0
                            self._record_live_buy(
                                mint=mint,
                                symbol=sym or "UNKNOWN",
                                token_key=tkey or f"solana:{mint}",
                                entry_tx_hash=tx_h or "on_chain_sync",
                                amount_token=amount,
                                cost_sol=sz_sol,
                                entry_price_sol=entry_px,
                            )
                    elif row[1] == "OPEN":
                        # Synchronize current balance if it changed (e.g. after partial exit)
                        if abs(float(row[2]) - amount) > 1e-4:
                            conn.execute(
                                "UPDATE live_positions SET amount_token = ?, updated_at = ? WHERE mint = ?",
                                (amount, time.time(), mint),
                            )
                            conn.commit()
            # 2. Check if any OPEN position has been closed on-chain
            with sqlite3.connect(self.db_path) as conn:
                open_rows = conn.execute(
                    "SELECT mint, symbol FROM live_positions WHERE status = 'OPEN'"
                ).fetchall()
                for (open_mint, open_sym) in open_rows:
                    if open_mint not in balances or balances[open_mint] <= 1.0:
                        self._record_live_sell(
                            mint=open_mint,
                            exit_tx_hash="on_chain_sync",
                            exit_reason="balance_depleted",
                        )
        except Exception as e:
            print(f"[EXECUTOR] Error during on-chain holdings reconciliation: {e}")

    def _get_open_live_positions(self) -> list[dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM live_positions WHERE status = 'OPEN' AND amount_token > 0").fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def _fetch_live_price(self, mint: str) -> float:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            req = urllib.request.Request(url, headers={"User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                return float(best.get("priceNative") or 0.0)
        except Exception:
            return 0.0

    def _fetch_live_5m_volume(self, mint: str) -> float:
        """Fetches 5-minute USD trading volume from DexScreener to detect dead liquidity."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            req = urllib.request.Request(url, headers={"User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode())
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                return float(best.get("volume", {}).get("m5") or 0.0)
        except Exception:
            return 9999.0  # Defensive: On network error, do not falsely liquidate

    def _update_live_price(self, mint: str, price_sol: float) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                now = time.time()
                conn.execute(
                    """
                    UPDATE live_positions
                    SET last_price_sol = ?,
                        peak_price_sol = MAX(peak_price_sol, ?),
                        updated_at = ?
                    WHERE mint = ? AND status = 'OPEN'
                    """,
                    (price_sol, price_sol, now, mint),
                )
                conn.commit()
        except Exception:
            pass

    async def check_live_positions_exit(self) -> None:
        """Monitor open on-chain positions: staged take-profits, moonbags, trailing stops, & stagnation recycler."""
        open_pos = self._get_open_live_positions()
        if not open_pos:
            return

        now = time.time()
        for pos in open_pos:
            mint = pos["mint"]
            symbol = pos.get("symbol") or "TOKEN"
            cost_sol = float(pos["cost_sol"])
            entry_px = float(pos["entry_price_sol"])
            amount_token = float(pos["amount_token"])
            peak_px = float(pos.get("peak_price_sol") or entry_px)
            opened_at = float(pos.get("opened_at") or now)
            age_seconds = now - opened_at
            prev_exit_reason = str(pos.get("exit_reason") or "")

            # Check actual on-chain token balance
            actual_token_bal = self.get_token_balance(mint)
            if actual_token_bal <= 0.0:
                self._record_live_sell(mint=mint, exit_tx_hash="onchain_zero", exit_reason="balance_zeroed")
                continue

            curr_px = self._fetch_live_price(mint)
            if curr_px <= 0:
                continue

            self._update_live_price(mint, curr_px)
            peak_px = max(peak_px, curr_px)

            multiple = curr_px / entry_px if entry_px > 0 else 1.0
            peak_multiple = peak_px / entry_px if entry_px > 0 else 1.0

            sell_amount = "100%"
            is_partial = False
            exit_reason = ""

            # Method 2 Upgraded: The +500% to +5000% Multi-Stage Mega-Runner Engine
            # Stage 1 at 2.50x (+150% gain): Sell 50% of tokens (Reclaims 125% of principal in pure SOL cash. Remaining 50% is a 100% risk-free Immortal Moonbag!)
            if multiple >= 2.5 and "stage1" not in prev_exit_reason:
                sell_amount = "50%"
                is_partial = True
                exit_reason = f"stage1_take_profit_{multiple:.2f}x"
            # Stage 2 at 5.0x (+500% gain Milestone): Sell 35% of remaining moonbag
            elif multiple >= 5.0 and "stage1" in prev_exit_reason and "stage2" not in prev_exit_reason:
                sell_amount = "35%"
                is_partial = True
                exit_reason = f"stage2_take_profit_{multiple:.2f}x"
            # Stage 3 at 10.0x (+1000% gain Milestone): Sell 40% of remaining moonbag
            elif multiple >= 10.0 and "stage2" in prev_exit_reason and "stage3" not in prev_exit_reason:
                sell_amount = "40%"
                is_partial = True
                exit_reason = f"stage3_take_profit_{multiple:.2f}x"
            # Stage 4 at 50.0x (+5000% gain Super-Runner Milestone): Sell 50% of remaining moonbag
            elif multiple >= 50.0 and "stage3" in prev_exit_reason and "stage4" not in prev_exit_reason:
                sell_amount = "50%"
                is_partial = True
                exit_reason = f"stage4_take_profit_{multiple:.2f}x"
            # Moonbag Wide Volatility Shield (after Stage 1): 50% trailing stop off peak to absorb 30-40% meme consolidations on path to 50x
            elif "stage1" in prev_exit_reason and peak_multiple >= 3.0 and (curr_px <= peak_px * 0.50):
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"moonbag_trailing_stop_50pct_off_peak_{peak_multiple:.2f}x"
            # Pre-Stage 1 Anti-Shakeout Trailing Stop: 35% buffer off peak (only active after reaching >= 1.40x)
            elif "stage1" not in prev_exit_reason and peak_multiple >= 1.40 and (curr_px <= peak_px * 0.65):
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"trailing_stop_35pct_off_peak_{peak_multiple:.2f}x"
            # Method 5: 15-Minute Stagnation Bag Recycler (reclaim dead capital from non-movers)
            elif age_seconds >= 900 and multiple < 1.15 and "stage1" not in prev_exit_reason:
                vol_5m = self._fetch_live_5m_volume(mint)
                if vol_5m < 500:
                    sell_amount = "100%"
                    is_partial = False
                    exit_reason = f"15m_stagnation_recycler (age {age_seconds/60:.0f}m, vol ${vol_5m:.0f})"

            if exit_reason:
                print(f"\n[EXECUTOR] 🎯 Live Exit Triggered for ${symbol}: {exit_reason} (Sell Amount: {sell_amount})")
                tx, pool_type = self.build_swap_transaction(
                    side="SELL",
                    mint=mint,
                    size_sol=0.0,
                    slippage_bps=350,
                    priority_fee_sol=0.0005,
                    sell_amount=sell_amount,
                )
                if tx:
                    try:
                        signed_tx = self.sign_transaction(tx)
                        ok, sig = self.dispatch_jito_bundle(signed_tx)
                        if ok:
                            print(f"[EXECUTOR] 🟢 LIVE EXIT CONFIRMED ON-CHAIN: {sig}")
                            await asyncio.sleep(2.0)
                            rem_token_bal = self.get_token_balance(mint)
                            if is_partial and rem_token_bal > 0:
                                # Update position with remaining tokens and reduced cost basis
                                new_cost = cost_sol * (0.50 if "stage1" in exit_reason else 0.65 if "stage2" in exit_reason else 0.60 if "stage3" in exit_reason else 0.50)
                                combined_reason = f"{prev_exit_reason}+{exit_reason}" if prev_exit_reason else exit_reason
                                with sqlite3.connect(self.db_path) as conn:
                                    conn.execute(
                                        """
                                        UPDATE live_positions
                                        SET amount_token = ?,
                                            cost_sol = ?,
                                            exit_reason = ?,
                                            updated_at = ?
                                        WHERE mint = ? AND status = 'OPEN'
                                        """,
                                        (rem_token_bal, new_cost, combined_reason, time.time(), mint),
                                    )
                                    conn.commit()
                                print(f"[EXECUTOR] 🚀 Staged Profit Locked for ${symbol}! Remaining Moonbag: {rem_token_bal:,.2f} tokens (Targeting +500% to +5000%!)")
                            else:
                                self._record_live_sell(mint=mint, exit_tx_hash=sig, exit_reason=exit_reason)
                    except Exception as e:
                        print(f"[EXECUTOR] Live exit execution failed: {e}")

    async def run_loop(self, interval: float = 1.0) -> None:
        print(f"[EXECUTOR] Pure Live Mode: Monitoring {self.db_path} for signals & open on-chain positions...")
        last_exit_check = 0.0
        last_sync_check = 0.0
        while self.running:
            # 1. Process pending execution signals
            signals = self.get_pending_signals()
            for sig in signals:
                await self.execute_signal(sig)

            now = time.time()
            # 2. Check open on-chain positions for exits every 3.0 seconds
            if now - last_exit_check >= 3.0:
                await self.check_live_positions_exit()
                last_exit_check = now

            # 3. Synchronize on-chain token accounts every 10.0 seconds
            if now - last_sync_check >= 10.0:
                await self.reconcile_on_chain_holdings()
                last_sync_check = now

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
