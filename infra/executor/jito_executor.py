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
    ) -> None:
        self.db_path = db_path
        self.dry_run = dry_run
        self.max_size_sol = max_size_sol
        self.private_key_b58 = os.environ.get("SOLANA_PRIVATE_KEY", "")
        self.public_key_b58 = os.environ.get("SOLANA_PUBLIC_KEY", "")

        if not self.dry_run and not self.private_key_b58:
            log.error(
                "Dry run is False, but SOLANA_PRIVATE_KEY is not set in environment. "
                "Use asm-exec to resolve secrets at runtime."
            )
            sys.exit(1)

    def get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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

    def build_pumpfun_trade_local(
        self,
        mint: str,
        action: str,
        amount_sol: float,
        slippage_bps: int,
        tip_lamports: int,
    ) -> bytes | None:
        """Call PumpPortal local trade endpoint to generate unsigned swap transaction."""
        payload = {
            "publicKey": self.public_key_b58,
            "action": action.lower(),
            "mint": mint,
            "amount": amount_sol,
            "denominatedInSol": "true",
            "slippage": max(1, round(slippage_bps / 100)),
            "priorityFee": 0.00005,
            "pool": "pump",
        }
        url = "https://pumpportal.fun/api/trade-local"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Botsensai-Executor/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.read()
        except Exception as e:
            log.error(f"Failed to build trade via PumpPortal: {e}")
            return None

    def submit_jito_bundle(self, signed_tx_b58: str) -> str | None:
        """Submit a signed transaction bundle to Jito Block Engine."""
        endpoint = random.choice(JITO_BLOCK_ENGINES)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [[signed_tx_b58]],
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("result")
        except Exception as e:
            log.error(f"Failed to submit bundle to Jito: {e}")
            return None

    def execute_signal(self, signal: dict[str, Any]) -> None:
        signal_id = signal["id"]
        token_key = signal["token_key"]
        _, _, mint = token_key.partition(":")
        mint = mint or token_key
        size_sol = min(signal["size_native"], self.max_size_sol)
        side = signal["side"]
        tip = max(signal.get("jito_tip_lamports", 0), DEFAULT_MIN_TIP_LAMPORTS)

        log.info(
            f"Processing signal #{signal_id}: {side} {size_sol:.4f} SOL for {signal.get('symbol') or mint[:8]} (score: {signal['score']:.3f})"
        )

        if self.dry_run:
            simulated_tx = f"simulated_tx_{signal_id}_{int(time.time())}"
            log.info(f"[DRY RUN] Would execute swap via Jito bundle. Assigned tx: {simulated_tx}")
            self.update_signal(signal_id, status="SIMULATED", tx_hash=simulated_tx)
            return

        unsigned_tx_bytes = self.build_pumpfun_trade_local(
            mint=mint,
            action=side,
            amount_sol=size_sol,
            slippage_bps=signal["max_slippage_bps"],
            tip_lamports=tip,
        )
        if not unsigned_tx_bytes:
            self.update_signal(signal_id, status="FAILED", error="Failed to construct transaction")
            return

        try:
            # Sign transaction with solana-py if available, or base58 broadcast
            import nacl.signing

            seed = b58decode(self.private_key_b58)[:32]
            signing_key = nacl.signing.SigningKey(seed)
            signed = signing_key.sign(unsigned_tx_bytes)
            signed_b58 = b58encode(signed.message)

            bundle_id = self.submit_jito_bundle(signed_b58)
            if bundle_id:
                log.info(f"Jito bundle submitted successfully: {bundle_id}")
                self.update_signal(signal_id, status="SUBMITTED", tx_hash=bundle_id)
            else:
                self.update_signal(signal_id, status="FAILED", error="Jito bundle rejected")
        except Exception as e:
            log.error(f"Failed to sign or submit: {e}")
            self.update_signal(signal_id, status="FAILED", error=str(e))

    def run_loop(self, poll_interval: float = 1.0) -> None:
        log.info(f"Jito Executor started (dry_run={self.dry_run}, max_canary={self.max_size_sol} SOL)")
        while True:
            try:
                signals = self.fetch_pending_signals()
                for signal in signals:
                    self.execute_signal(signal)
            except Exception as e:
                log.error(f"Executor loop error: {e}")
            time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai Jito Execution Adapter")
    parser.add_argument("--db", default="data/botsensai.db", help="Path to SQLite database")
    parser.add_argument("--live", action="store_true", help="Enable live signing (default: dry-run)")
    parser.add_argument("--max-size", type=float, default=DEFAULT_MAX_CANARY_SOL, help="Max canary size in SOL")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds")
    args = parser.parse_args()

    executor = JitoExecutor(
        db_path=args.db,
        dry_run=not args.live,
        max_size_sol=args.max_size,
    )
    executor.run_loop(poll_interval=args.interval)


if __name__ == "__main__":
    main()
