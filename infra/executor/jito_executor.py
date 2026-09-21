#!/usr/bin/env python3
"""Botsensai Standalone Jito Execution Adapter.

Runs in the control plane or as a dedicated execution daemon.
Polls `execution_signals` from the Botsensai database, builds MEV-protected
swap transactions, attaches Jito tips, and submits bundles directly to the
Jito Block Engine.

Secret Safety:
  In AWS, the wallet private key MUST be injected at runtime using `asm-exec`
  from AWS Secrets Manager (per AGENTS.md), e.g.:
    asm-exec --secret-id "botsensai/solana-wallet" -- python infra/executor/jito_executor.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import random
import sqlite3
import sys
import time
from typing import Any
import urllib.error
import urllib.request

# Pure Python Base58 implementation (zero external dependency)
B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    chars = []
    while n > 0:
        n, r = divmod(n, 58)
        chars.append(B58_ALPHABET[r : r + 1])
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return (b"1" * pad + b"".join(reversed(chars))).decode("ascii") or "1"


def b58decode(s: str) -> bytes:
    b = s.encode("ascii")
    n = 0
    for char in b:
        idx = B58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"Invalid Base58 character: {chr(char)}")
        n = n * 58 + idx
    res = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big") if n > 0 else b""
    pad = 0
    for char in b:
        if char == ord("1"):
            pad += 1
        else:
            break
    return b"\x00" * pad + res

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("botsensai.executor")

# Verified Jito Block Engine endpoints
JITO_BLOCK_ENGINES = [
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
]

# Verified public Jito Tip Accounts (randomized per bundle)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

DEFAULT_MAX_CANARY_SOL = 0.05
DEFAULT_MIN_TIP_LAMPORTS = 100_000  # 0.0001 SOL


class JitoExecutor:
    def __init__(
        self,
        db_path: str = "data/botsensai.db",
        dry_run: bool = True,
        max_size_sol: float = DEFAULT_MAX_CANARY_SOL,
        keyfile: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.dry_run = dry_run
        self.max_size_sol = max_size_sol
        self.keyfile = keyfile or os.environ.get("SOLANA_KEYFILE")
        self.private_key_b58 = os.environ.get("SOLANA_PRIVATE_KEY", "")
        self.public_key_b58 = os.environ.get("SOLANA_PUBLIC_KEY", "")
        self._keypair = None

        self._load_keypair()
        if not self.dry_run and self._keypair is None:
            log.error(
                "Dry run is False, but no signing keypair found. "
                "Provide SOLANA_PRIVATE_KEY, SOLANA_KEYFILE, or ensure ~/.config/solana/id.json exists."
            )
            sys.exit(1)

    def _load_keypair(self) -> None:
        try:
            from solders.keypair import Keypair
        except ImportError:
            log.debug("solders not installed; keypair loading skipped.")
            return

        if self.private_key_b58:
            try:
                val = self.private_key_b58.strip()
                if val.startswith("["):
                    self._keypair = Keypair.from_bytes(bytes(json.loads(val)))
                else:
                    self._keypair = Keypair.from_base58_string(val)
                self.public_key_b58 = str(self._keypair.pubkey())
                log.info(f"Loaded hot wallet keypair from environment: {self.public_key_b58}")
                return
            except Exception as e:
                log.error(f"Failed to load keypair from SOLANA_PRIVATE_KEY: {e}")

        # Check explicit or default keyfiles
        candidates = []
        if self.keyfile:
            candidates.append(Path(self.keyfile).expanduser())
        candidates.append(Path.home() / ".config" / "solana" / "id.json")

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                try:
                    data = json.loads(candidate.read_text())
                    self._keypair = Keypair.from_bytes(bytes(data))
                    self.public_key_b58 = str(self._keypair.pubkey())
                    log.info(f"Loaded hot wallet keypair from {candidate} ({self.public_key_b58})")
                    return
                except Exception as e:
                    log.warning(f"Could not load keypair from {candidate}: {e}")

    def get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                token_key         TEXT NOT NULL,
                symbol            TEXT,
                side              TEXT NOT NULL,
                size_native       REAL NOT NULL,
                max_slippage_bps  INTEGER NOT NULL,
                jito_tip_lamports INTEGER NOT NULL,
                score             REAL NOT NULL,
                as_of             REAL NOT NULL,
                status            TEXT NOT NULL DEFAULT 'PENDING',
                tx_hash           TEXT,
                error             TEXT,
                created_at        REAL NOT NULL,
                executed_at       REAL
            );
            """
        )
        conn.commit()
        return conn

    def fetch_pending_signals(self) -> list[dict[str, Any]]:
        with self.get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, token_key, symbol, side, size_native, max_slippage_bps,
                       jito_tip_lamports, score, as_of, status, created_at
                FROM execution_signals
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                LIMIT 10
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def update_signal(
        self, signal_id: int, status: str, tx_hash: str | None = None, error: str | None = None
    ) -> None:
        with self.get_db() as conn:
            conn.execute(
                """
                UPDATE execution_signals
                SET status = ?, tx_hash = ?, error = ?, executed_at = ?
                WHERE id = ?
                """,
                (status, tx_hash, error, time.time() if status != "PENDING" else None, signal_id),
            )
            conn.commit()

    def build_pumpfun_jito_bundle(
        self,
        mint: str,
        action: str,
        amount_sol: float,
        slippage_bps: int,
        tip_lamports: int,
    ) -> list[str] | None:
        """Call PumpPortal trade-local with bundle list format to receive unsigned transactions."""
        tip_sol = max(tip_lamports / 1_000_000_000.0, 0.0001)
        payload = [
            {
                "publicKey": self.public_key_b58,
                "action": action.lower(),
                "mint": mint,
                "denominatedInSol": "true" if action.lower() == "buy" else "false",
                "amount": amount_sol if action.lower() == "buy" else "100%",
                "slippage": max(5, round(slippage_bps / 100)),
                "priorityFee": tip_sol,
                "pool": "pump",
            }
        ]
        url = "https://pumpportal.fun/api/trade-local"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Botsensai-Executor/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and "error" in data:
                    log.error(f"PumpPortal bundle error: {data['error']}")
                    return None
                log.error(f"Unexpected PumpPortal response: {data}")
                return None
        except Exception as e:
            log.error(f"Failed to build trade bundle via PumpPortal: {e}")
            return None

    def submit_jito_bundle(self, encoded_signed_txs: list[str]) -> str | None:
        """Submit a signed transaction bundle to Jito Block Engine endpoints in parallel."""
        import concurrent.futures

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [encoded_signed_txs],
        }
        data = json.dumps(payload).encode("utf-8")
        last_errors: list[str] = []

        def _send(ep: str) -> str | None:
            req = urllib.request.Request(
                ep,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "Botsensai-Executor/1.0"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    bundle_id = result.get("result")
                    if bundle_id:
                        return bundle_id
                    if "error" in result:
                        err_msg = str(result["error"])
                        last_errors.append(f"{ep}: {err_msg}")
                        log.warning(f"Jito endpoint {ep} returned error: {err_msg}")
            except Exception as e:
                last_errors.append(f"{ep}: {e}")
                log.warning(f"Failed to submit bundle to Jito endpoint {ep}: {e}")
            return None

        for attempt in range(2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(JITO_BLOCK_ENGINES)) as executor:
                future_to_ep = {executor.submit(_send, ep): ep for ep in JITO_BLOCK_ENGINES}
                for future in concurrent.futures.as_completed(future_to_ep):
                    bid = future.result()
                    if bid:
                        log.info(f"Bundle successfully accepted by Jito Block Engine ({future_to_ep[future]}): {bid}")
                        return bid
            if attempt == 0 and any("429" in err for err in last_errors):
                time.sleep(1.5)

        err_summary = "; ".join(last_errors[-2:]) if last_errors else "unknown"
        log.error(f"All Jito Block Engine endpoints failed to accept bundle: {err_summary}")
        return None

    def execute_signal(self, signal: dict[str, Any]) -> None:
        signal_id = signal["id"]
        token_key = signal["token_key"]
        _, _, mint = token_key.partition(":")
        mint = mint or token_key
        size_sol = min(signal["size_native"], self.max_size_sol)
        side = signal["side"]
        
        tip = signal.get("jito_tip_lamports", 0)
        if not tip or tip <= DEFAULT_MIN_TIP_LAMPORTS:
            try:
                from botsensai.execution.jito_tips import JitoTipEngine
                tip = JitoTipEngine.get_instance().calculate_tip_lamports(
                    conviction_score=signal.get("score", 0.70),
                    trade_size_sol=size_sol,
                )
            except Exception:
                tip = DEFAULT_MIN_TIP_LAMPORTS
        else:
            tip = max(tip, DEFAULT_MIN_TIP_LAMPORTS)

        now = time.time()
        age = now - signal["created_at"]
        if age > 60.0:
            log.warning(
                f"Signal #{signal_id} is {age:.1f}s old (> 60.0s max age). Expiring to protect capital."
            )
            self.update_signal(signal_id, status="EXPIRED", error=f"Signal expired ({age:.1f}s old)")
            return

        log.info(
            f"Processing signal #{signal_id}: {side} {size_sol:.4f} SOL for {signal.get('symbol') or mint[:8]} (score: {signal['score']:.3f})"
        )

        if self.dry_run:
            simulated_tx = f"simulated_bundle_{signal_id}_{int(time.time())}"
            log.info(f"[DRY RUN] Would execute swap via Jito bundle. Assigned tx: {simulated_tx}")
            self.update_signal(signal_id, status="SIMULATED", tx_hash=simulated_tx)
            return

        unsigned_tx_b58_list = self.build_pumpfun_jito_bundle(
            mint=mint,
            action=side,
            amount_sol=size_sol,
            slippage_bps=signal["max_slippage_bps"],
            tip_lamports=tip,
        )
        if not unsigned_tx_b58_list:
            self.update_signal(signal_id, status="FAILED", error="Failed to construct transaction bundle")
            return

        try:
            from solders.transaction import VersionedTransaction

            if self._keypair is None:
                self.update_signal(signal_id, status="FAILED", error="Keypair not loaded for live signing")
                return

            keypair = self._keypair
            encoded_signed_txs: list[str] = []
            first_sig: str = ""

            for raw_b58 in unsigned_tx_b58_list:
                raw_bytes = b58decode(raw_b58)
                msg = VersionedTransaction.from_bytes(raw_bytes).message
                signed_tx = VersionedTransaction(msg, [keypair])
                encoded_signed_txs.append(b58encode(bytes(signed_tx)))
                if not first_sig:
                    first_sig = str(signed_tx.signatures[0])

            bundle_id = self.submit_jito_bundle(encoded_signed_txs)
            if bundle_id:
                log.info(f"Jito bundle submitted successfully! Bundle ID: {bundle_id}, Tx Sig: {first_sig}")
                self.update_signal(signal_id, status="SUBMITTED", tx_hash=bundle_id)
            else:
                self.update_signal(signal_id, status="FAILED", error="Jito bundle rejected")
        except Exception as e:
            log.error(f"Failed to sign or submit bundle: {e}")
            self.update_signal(signal_id, status="FAILED", error=str(e))

    def run_loop(self, poll_interval: float = 1.0) -> None:
        log.info(f"Jito Executor started (dry_run={self.dry_run}, max_canary={self.max_size_sol} SOL)")
        while True:
            try:
                signals = self.fetch_pending_signals()
                for signal in signals:
                    self.execute_signal(signal)
                    time.sleep(0.5)
            except Exception as e:
                log.error(f"Executor loop error: {e}")
            time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai Jito Execution Adapter")
    parser.add_argument("--db", default="data/botsensai.db", help="Path to SQLite database")
    parser.add_argument("--live", action="store_true", help="Enable live signing (default: dry-run)")
    parser.add_argument("--keyfile", default=None, help="Path to Solana keypair JSON file (default: ~/.config/solana/id.json)")
    parser.add_argument("--max-size", type=float, default=DEFAULT_MAX_CANARY_SOL, help="Max canary size in SOL")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds")
    args = parser.parse_args()

    executor = JitoExecutor(
        db_path=args.db,
        dry_run=not args.live,
        max_size_sol=args.max_size,
        keyfile=args.keyfile,
    )
    executor.run_loop(poll_interval=args.interval)


if __name__ == "__main__":
    main()
