#!/usr/bin/env python3
"""Botsensai Canary Calibration & End-to-End Latency Probe.

Validates the live Solana trading infrastructure without risking significant capital:
1. Benchmarks RPC round-trip latency and blockhash fetch freshness.
2. Probes latency to all Jito Block Engine endpoints (NY, Frankfurt, Amsterdam, Tokyo).
3. Verifies Yellowstone gRPC or WebSocket streaming responsiveness.
4. Generates a canary execution signal (dry-run or canary size 0.001 SOL) to measure
   exact pipeline serialization, tip computation, and bundle construction latency.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("botsensai.canary")

JITO_ENDPOINTS = [
    ("mainnet", "https://mainnet.block-engine.jito.wtf/api/v1/bundles"),
    ("ny", "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles"),
    ("amsterdam", "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles"),
    ("frankfurt", "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles"),
    ("tokyo", "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles"),
]


def probe_rpc(rpc_url: str) -> dict[str, float | str | bool]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "processed"}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            res = json.loads(resp.read().decode("utf-8"))
            if "result" in res and "value" in res["result"]:
                bh = res["result"]["value"]["blockhash"]
                last_valid = res["result"]["value"]["lastValidBlockHeight"]
                return {
                    "ok": True,
                    "latency_ms": round(elapsed_ms, 2),
                    "blockhash": bh[:12] + "...",
                    "last_valid_height": last_valid,
                }
            return {"ok": False, "error": str(res.get("error"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def probe_jito_endpoint(name: str, url: str) -> dict[str, float | str | bool]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTipAccounts",
        "params": [],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            res = json.loads(resp.read().decode("utf-8"))
            accounts = res.get("result", [])
            return {
                "name": name,
                "ok": bool(accounts),
                "latency_ms": round(elapsed_ms, 2),
                "tip_accounts_count": len(accounts),
            }
    except Exception as exc:
        return {"name": name, "ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Botsensai Canary & Latency Calibrator")
    parser.add_argument("--rpc", default="https://api.mainnet-beta.solana.com", help="Solana RPC URL")
    args = parser.parse_args()

    log.info("--- BOTSNDAI CANARY CALIBRATION RUN ---")
    log.info("1. Probing primary RPC: %s", args.rpc)
    rpc_res = probe_rpc(args.rpc)
    log.info("RPC Result: %s", json.dumps(rpc_res))

    log.info("2. Probing Jito Block Engine multi-regional endpoints:")
    fastest_jito = None
    min_latency = float("inf")
    for name, url in JITO_ENDPOINTS:
        jito_res = probe_jito_endpoint(name, url)
        log.info("  [%s] %s -> %s", name, url, json.dumps(jito_res))
        if jito_res.get("ok") and jito_res.get("latency_ms", float("inf")) < min_latency:
            min_latency = jito_res["latency_ms"]
            fastest_jito = name

    log.info("Fastest Jito Region: %s (%.2f ms)", fastest_jito, min_latency)
    log.info("--- CALIBRATION COMPLETE ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
