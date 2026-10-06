"""Firehose listener for pump.fun events.

This module connects to the Helius Firehose WebSocket endpoint and streams
transactions in real‑time. It filters for instructions that target the pump.fun
or pumpswap program IDs and forwards the relevant data to the scanner for
mid‑rise scoring.

The listener runs as an async task and is launched alongside the existing
Orchestrator and Monitor agents via `scripts/run_agents.py`.
"""

import os
import json
import asyncio
import logging
from typing import Any, Dict, List

import websockets

log = logging.getLogger("firehose")

# ---------------------------------------------------------------------------
# Configuration – pull the Helius API key from the environment (standard for
# the Botsensai codebase).
# ---------------------------------------------------------------------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY") or os.getenv("BOTSENSAI_HELIUS_API_KEY")
if not HELIUS_API_KEY:
    raise RuntimeError("HELIUS_API_KEY is required for the firehose listener")

FIREHOSE_URL = (
    f"wss://solana-mainnet.helius-rpc.com/v0/firehose?api-key={HELIUS_API_KEY}"
)

# Pump.fun and PumpSwap program IDs (taken from the token‑flow docs). Adjust if
# the IDs ever change.
PUMP_PROGRAM_IDS = {
    "pumpfun1111111111111111111111111111111111",
    "pumpswap111111111111111111111111111111111",
}

# ---------------------------------------------------------------------------
# Helper – forward a candidate to the scanner's in‑memory ranking queue.
# The scanner already exposes `ingest_candidate(candidate: Dict[str, Any])` –
# we import it lazily to avoid circular imports.
# ---------------------------------------------------------------------------
async def _forward_to_scanner(candidate: Dict[str, Any]) -> None:
    try:
        from botsensai.agents.scanner import ingest_candidate  # type: ignore
    except Exception:
        log.debug("Scanner ingest not available yet – skipping forward")
        return
    try:
        ingest_candidate(candidate)
    except Exception as exc:
        log.exception("Failed to forward firehose candidate to scanner: %s", exc)

# ---------------------------------------------------------------------------
# Core listener coroutine.
# ---------------------------------------------------------------------------
async def listen() -> None:
    """Connect to Helius Firehose and process pump.fun transactions.

    The firehose payload contains a list of *transactions*; each transaction has
    a list of *instructions*.  We look for any instruction whose `programId`
    matches one of the pump.fun IDs. When we find a match we extract a minimal
    candidate dict and forward it to the scanner.
    """
    while True:
        try:
            async with websockets.connect(FIREHOSE_URL) as ws:
                log.info("Connected to Helius Firehose")
                while True:
                    raw_msg = await ws.recv()
                    data = json.loads(raw_msg)
                    for tx in data.get("transactions", []):
                        for ix in tx.get("instructions", []):
                            prog_id = ix.get("programId")
                            if prog_id not in PUMP_PROGRAM_IDS:
                                continue
                            # Very basic extraction – most pump.fun swaps include the
                            # mint address as the first account.  This is sufficient
                            # for the scanner which will later enrich the data via
                            # DexScreener if needed.
                            accounts: List[str] = ix.get("accounts", [])
                            if not accounts:
                                continue
                            candidate = {
                                "mint": accounts[0],
                                "programId": prog_id,
                                "signature": tx.get("signature"),
                                "slot": tx.get("slot"),
                            }
                            await _forward_to_scanner(candidate)
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning("Firehose connection closed (%s); reconnecting in 5s", exc)
            await asyncio.sleep(5)
        except Exception as exc:
            log.exception("Unexpected error in firehose listener: %s", exc)
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# Entry point for the async runner.
# ---------------------------------------------------------------------------
async def run_firehose_listener() -> None:
    await listen()
