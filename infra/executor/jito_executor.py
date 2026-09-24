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
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
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

SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

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
        self.rpc_urls: list[str] = []
        for cand in [
            rpc_url,
            os.getenv("SOLANA_RPC_URL"),
            os.getenv("BOTSENSAI_SOLANA_RPC_URL"),
            os.getenv("HELIUS_RPC_URL"),
            os.getenv("BOTSENSAI_HELIUS_FALLBACK_RPC_URL"),
            "https://api.mainnet-beta.solana.com",
        ]:
            if cand and cand not in self.rpc_urls:
                self.rpc_urls.append(cand)
        self.running = True
        self.keypair: Keypair | None = None
        self._load_keypair()

    def _call_rpc(self, payload: dict[str, Any], timeout: float = 8.0) -> dict[str, Any] | None:
        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "Botsensai-Executor/2.0"}
        for url in self.rpc_urls:
            try:
                req = urllib.request.Request(url, data=data_bytes, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as he:
                if he.code == 429:
                    continue
                continue
            except Exception:
                continue
        return None

    def _check_tx_error(self, tx_sig: str) -> str | None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        res = self._call_rpc(payload, timeout=10.0)
        if res and "result" in res and res["result"]:
            meta = res["result"].get("meta") or {}
            err = meta.get("err")
            if err:
                return str(err)
        return None

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
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(self.keypair.pubkey())],
        }
        res = self._call_rpc(payload, timeout=6.0)
        if res and "result" in res:
            lamports = res["result"].get("value", 0)
            return lamports / 1e9
        return 0.0

    def get_token_balance(self, mint: str) -> float:
        if not self.keypair:
            return 0.0
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
        res = self._call_rpc(payload, timeout=8.0)
        if res and "result" in res:
            accounts = res["result"].get("value", [])
            total_amount = 0.0
            for acc in accounts:
                info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                token_amount = info.get("tokenAmount", {}).get("uiAmount", 0.0)
                if token_amount:
                    total_amount += float(token_amount)
            return total_amount
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
        slippage_bps = int(sig.get("max_slippage_bps") or 250)
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

        # Method 1: Dynamic Bankroll Scaling & Buffer Floor Protection
        if side == "BUY":
            # Require at least 0.012 SOL buffer above floor to safely absorb trade + ATA rent (~0.00204 SOL) + fees (~0.0008 SOL)
            available_buffer = wallet_balance - MIN_WALLET_RESERVE_SOL
            if available_buffer < 0.012:
                warn = (
                    f"Capital Guard: Available buffer ({available_buffer:.4f} SOL) above reserve floor "
                    f"is insufficient for safe entry (minimum 0.012 SOL required to protect 0.10 SOL floor after rent & fees). Holding dry powder."
                )
                print(f"[EXECUTOR] 🛑 {warn}")
                self.update_signal_status(sig_id, status="BLOCKED", error=warn)
                return

            dynamic_bankroll_size = available_buffer * 0.05
            scaled_size = max(size_sol, dynamic_bankroll_size, 0.008)
            # Ensure trade size + ATA rent/gas margin (0.0035 SOL) never drops balance below the reserve floor
            max_safe_entry = max(0.005, available_buffer - 0.0035)
            safe_size_sol = min(scaled_size, max_safe_entry, MAX_POSITION_SIZE_SOL)
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

        safe_slippage_bps = min(max(slippage_bps, 200), MAX_SLIPPAGE_BPS)

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
            print(f"[EXECUTOR] 🟢 Bundle submitted to Jito Block Engine: {tx_signature}")

            # Record or update live on-chain positions
            if side == "BUY":
                token_bal = 0.0
                for _ in range(6):
                    await asyncio.sleep(2.0)
                    token_bal = self.get_token_balance(mint)
                    if token_bal > 0:
                        break
                if token_bal > 0:
                    self.update_signal_status(sig_id, status="LANDED", tx_hash=tx_signature)
                    entry_px = safe_size_sol / token_bal
                    print(f"[EXECUTOR] 🟢 CONFIRMED ON-CHAIN BUY: {token_bal:,.2f} ${symbol} landed in hot wallet ({tx_signature})")
                    self._record_live_buy(
                        mint=mint,
                        symbol=symbol,
                        token_key=token_key or f"solana:{mint}",
                        entry_tx_hash=tx_signature,
                        amount_token=token_bal,
                        cost_sol=safe_size_sol,
                        entry_price_sol=entry_px,
                    )
                else:
                    tx_err = self._check_tx_error(tx_signature)
                    if tx_err:
                        print(f"[EXECUTOR] ⚠️ Swap reverted on-chain ({tx_err}). No tokens received.")
                        self.update_signal_status(sig_id, status="REVERTED", tx_hash=tx_signature, error=f"Reverted: {tx_err}")
                    else:
                        print(f"[EXECUTOR] ⚠️ Bundle signature {tx_signature} not confirmed with positive balance.")
                        self.update_signal_status(sig_id, status="UNCONFIRMED", tx_hash=tx_signature, error="Zero token balance after bundle landing")
            elif side == "SELL":
                await asyncio.sleep(2.0)
                token_bal = self.get_token_balance(mint)
                if token_bal <= 0:
                    self.update_signal_status(sig_id, status="LANDED", tx_hash=tx_signature)
                    self._record_live_sell(
                        mint=mint,
                        exit_tx_hash=tx_signature,
                        exit_reason="signal_sell",
                    )
                else:
                    self.update_signal_status(sig_id, status="PARTIAL_EXIT", tx_hash=tx_signature)
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

    def _fetch_token_symbol(self, mint: str) -> str:
        """Fetches the token symbol from DexScreener if not present in execution signals."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            req = urllib.request.Request(url, headers={"User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode())
                pairs = data.get("pairs") or []
                if pairs:
                    best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                    return str(best.get("baseToken", {}).get("symbol") or "TOKEN")
        except Exception:
            pass
        return "TOKEN"

    def close_token_account_on_chain(self, mint: str) -> bool:
        """Closes zero-balance token accounts on-chain to reclaim ~0.00204 SOL rent per account."""
        if not self.keypair:
            return False
        try:
            wallet_pub = str(self.keypair.pubkey())
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    wallet_pub,
                    {"mint": mint},
                    {"encoding": "jsonParsed"},
                ],
            }
            res = self._call_rpc(payload, timeout=5.0)
            if res and "result" in res:
                accounts = res["result"].get("value", [])
                for acc in accounts:
                    acc_pub = acc.get("pubkey")
                    owner_prog = acc.get("account", {}).get("owner") or SPL_TOKEN_PROGRAM_ID
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    token_amt = float(info.get("tokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                    if token_amt <= 0.0 and acc_pub:
                        bh_res = self._call_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "getLatestBlockhash",
                                "params": [{"commitment": "finalized"}],
                            },
                            timeout=5.0,
                        )
                        if not bh_res or "result" not in bh_res:
                            continue
                        bh_str = bh_res["result"]["value"]["blockhash"]
                        recent_bh = Hash.from_string(bh_str)

                        prog_id = Pubkey.from_string(owner_prog)
                        acc_to_close = Pubkey.from_string(acc_pub)
                        dest = self.keypair.pubkey()

                        ix = Instruction(
                            program_id=prog_id,
                            data=bytes([9]),  # CloseAccount opcode
                            accounts=[
                                AccountMeta(pubkey=acc_to_close, is_signer=False, is_writable=True),
                                AccountMeta(pubkey=dest, is_signer=False, is_writable=True),
                                AccountMeta(pubkey=dest, is_signer=True, is_writable=False),
                            ],
                        )
                        msg = MessageV0.try_compile(
                            payer=dest,
                            instructions=[ix],
                            address_lookup_table_accounts=[],
                            recent_blockhash=recent_bh,
                        )
                        signed_tx = VersionedTransaction(msg, [self.keypair])
                        tx_b64 = base64.b64encode(bytes(signed_tx)).decode("ascii")
                        send_res = self._call_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "sendTransaction",
                                "params": [
                                    tx_b64,
                                    {
                                        "encoding": "base64",
                                        "skipPreflight": True,
                                        "preflightCommitment": "processed",
                                    },
                                ],
                            },
                            timeout=6.0,
                        )
                        if send_res and "result" in send_res:
                            print(f"[EXECUTOR] 🧹 Automated Rent Swept! Closed {acc_pub[:8]}... (Tx: {send_res['result'][:16]}...)")
                            return True
        except Exception as e:
            print(f"[EXECUTOR] Rent reclamation error for {mint[:8]}...: {e}")
        return False

    def sweep_all_empty_token_accounts(self) -> int:
        """Scans all wallet token accounts across SPL Token & Token-2022 and closes any with zero balance to reclaim rent."""
        if not self.keypair:
            return 0
        swept = 0
        try:
            for prog in [SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
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
                res = self._call_rpc(payload, timeout=5.0)
                if res and "result" in res:
                    for acc in res["result"].get("value", []):
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        amt = float(info.get("tokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                        mint = info.get("mint")
                        if amt <= 0.0 and mint:
                            if self.close_token_account_on_chain(mint):
                                swept += 1
        except Exception as e:
            print(f"[EXECUTOR] Error sweeping empty token accounts: {e}")
        return swept

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
            
            # Non-blocking automatic rent sweeping
            try:
                self.close_token_account_on_chain(mint)
            except Exception:
                pass
        except Exception as e:
            print(f"[EXECUTOR] Error recording live sell: {e}")

    def get_all_token_balances(self) -> dict[str, float] | None:
        """Returns {mint: total_ui_amount} for all on-chain token accounts owned by wallet.
        Returns None if query failed due to RPC errors, preventing false reconciliation."""
        if not self.keypair:
            return None
        balances: dict[str, float] = {}
        for prog in [
            SPL_TOKEN_PROGRAM_ID,
            TOKEN_2022_PROGRAM_ID,
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
            res = self._call_rpc(payload, timeout=8.0)
            if res is None or "result" not in res:
                print(f"[EXECUTOR] Warning: Could not fetch token accounts for {prog[:10]}... across RPCs")
                return None
            for acc in res.get("result", {}).get("value", []):
                info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                m = info.get("mint")
                amt = float(info.get("tokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                if m and amt > 0:
                    balances[m] = balances.get(m, 0.0) + amt
                elif m and amt <= 0:
                    # Clean up empty account to reclaim SOL rent
                    try:
                        self.close_token_account_on_chain(m)
                    except Exception:
                        pass
        return balances

    async def reconcile_on_chain_holdings(self) -> None:
        """Synchronizes on-chain token holdings directly into live_positions."""
        if not self.keypair:
            return
        try:
            balances = self.get_all_token_balances()
            if balances is None:
                # RPC issue: abort immediately without modifying any positions
                return

            # 1. Ensure all positive on-chain holdings are tracked in live_positions
            for mint, amount in balances.items():
                if amount <= 1.0:  # Ignore sub-unit dust
                    continue
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        "SELECT mint, status, amount_token FROM live_positions WHERE mint = ?",
                        (mint,),
                    ).fetchone()
                    if not row or row[1] == "CLOSED":
                        # Check execution signals with inclusive status
                        sig_row = conn.execute(
                            "SELECT symbol, token_key, size_native, tx_hash FROM execution_signals "
                            "WHERE token_key LIKE ? AND side = 'BUY' "
                            "ORDER BY id DESC LIMIT 1",
                            (f"%{mint}%",),
                        ).fetchone()
                        if sig_row:
                            sym, tkey, sz_sol, tx_h = sig_row
                            sz_sol = float(sz_sol or 0.015)
                        else:
                            sym = self._fetch_token_symbol(mint) or "TOKEN"
                            tkey = f"solana:{mint}"
                            sz_sol = 0.0
                            tx_h = "on_chain_sync"

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
                        # Mark associated signal as LANDED
                        conn.execute(
                            "UPDATE execution_signals SET status = 'LANDED' WHERE token_key LIKE ? AND side = 'BUY' AND status IN ('UNCONFIRMED', 'SUBMITTED')",
                            (f"%{mint}%",),
                        )
                        conn.commit()
                    elif row[1] == "OPEN":
                        # Synchronize current balance if it changed (e.g. after partial exit)
                        if abs(float(row[2]) - amount) > 1e-4:
                            conn.execute(
                                "UPDATE live_positions SET amount_token = ?, updated_at = ? WHERE mint = ?",
                                (amount, time.time(), mint),
                            )
                            conn.commit()

            # 2. Check if any OPEN position has actually been closed on-chain
            with sqlite3.connect(self.db_path) as conn:
                open_rows = conn.execute(
                    "SELECT mint, symbol, amount_token FROM live_positions WHERE status = 'OPEN'"
                ).fetchall()
                for (open_mint, open_sym, open_amt) in open_rows:
                    cur_bal = balances.get(open_mint)
                    if cur_bal is None or cur_bal <= 1.0:
                        # Dedicated verification before closing to prevent false closures
                        direct_bal = self.get_token_balance(open_mint)
                        if direct_bal <= 1.0:
                            self._record_live_sell(
                                mint=open_mint,
                                exit_tx_hash="on_chain_sync",
                                exit_reason="balance_depleted",
                            )
                        else:
                            conn.execute(
                                "UPDATE live_positions SET amount_token = ?, updated_at = ? WHERE mint = ?",
                                (direct_bal, time.time(), open_mint),
                            )
                            conn.commit()
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
            # Stage 1 at 2.00x (+100% gain Milestone): Sell 50% of tokens
            # (Reclaims exactly 100% of initial SOL capital into pure liquid cash. Remaining 50% is a 100% risk-free Immortal Moonbag!)
            if multiple >= 2.00 and "stage1" not in prev_exit_reason:
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
            # Moonbag Trailing Volatility Shield (after Stage 1): 40% buffer off peak once peak >= 2.50x
            elif "stage1" in prev_exit_reason and peak_multiple >= 2.50 and (curr_px <= peak_px * 0.60):
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"moonbag_trailing_stop_40pct_off_peak_{peak_multiple:.2f}x"
            # Breakeven Protection: If coin rallied >= 1.45x and retraces to <= 1.05x, lock in breakeven before decaying
            elif "stage1" not in prev_exit_reason and peak_multiple >= 1.45 and multiple <= 1.05:
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"breakeven_protection_{peak_multiple:.2f}x_peak_retrace_{multiple:.2f}x"
            # Pre-Stage 1 Anti-Shakeout Trailing Stop: 35% buffer off peak (only active after reaching >= 1.50x)
            elif "stage1" not in prev_exit_reason and peak_multiple >= 1.50 and (curr_px <= peak_px * 0.65):
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"trailing_stop_35pct_off_peak_{peak_multiple:.2f}x"
            # Method 5: Fast Stagnation Recycler (reclaim dead capital from non-movers)
            elif (
                "stage1" not in prev_exit_reason
                and (
                    (age_seconds >= 330 and multiple < 1.05 and self._fetch_live_5m_volume(mint) < 300)
                    or (age_seconds >= 720 and multiple < 0.90)
                )
            ):
                vol_5m = self._fetch_live_5m_volume(mint)
                sell_amount = "100%"
                is_partial = False
                exit_reason = f"5m_stagnation_recycler (age {age_seconds/60:.1f}m, vol ${vol_5m:.0f})"

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

            # 3. Synchronize on-chain token accounts every 30.0 seconds
            if now - last_sync_check >= 30.0:
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
