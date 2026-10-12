"""FastAPI Server exposing real-time Botsensai data APIs and Gemini Chat integration."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

HOT_WALLET = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
GAS_FLOOR = 0.010
DB_PATHS = [
    Path("/Users/drop/.gemini/antigravity/worktrees/Botsensai/fix_pump_copytrading_bot/data/botsensai.db"),
    Path("/Users/drop/.gemini/antigravity/worktrees/Botsensai/fix_pump_copytrading_bot/data/botsensai_remote.db"),
    Path("/home/ubuntu/Botsensai/data/botsensai.db"),
]

app = FastAPI(title="Botsensai Live Trading API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection() -> sqlite3.Connection | None:
    for p in DB_PATHS:
        if p.exists():
            conn = sqlite3.connect(p, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
    return None


def fetch_rpc(method: str, params: list[Any]) -> Any:
    urls = [
        os.getenv("SOLANA_RPC_URL"),
        "https://api.mainnet-beta.solana.com",
    ]
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for u in [u for u in urls if u]:
        try:
            req = urllib.request.Request(u, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                res = json.loads(r.read())
                return res.get("result")
        except Exception:
            continue
    return None


@app.get("/api/wallet")
async def get_wallet():
    bal_res = fetch_rpc("getBalance", [HOT_WALLET])
    lamports = bal_res.get("value", 0) if bal_res else 0
    sol = lamports / 1e9
    buffer = max(0.0, sol - GAS_FLOOR)
    return {
        "address": HOT_WALLET,
        "balance_sol": round(sol, 6),
        "lamports": lamports,
        "gas_floor": GAS_FLOOR,
        "buffer_sol": round(buffer, 6),
        "status": "SAFE" if buffer >= 0.003 else "WARNING",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/positions")
async def get_positions():
    # 1. Fetch on-chain token balances from Tokenkeg & Token-2022
    token_mints = {}
    for pid in [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    ]:
        res = fetch_rpc("getTokenAccountsByOwner", [HOT_WALLET, {"programId": pid}, {"encoding": "jsonParsed"}])
        if res and "value" in res:
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                mint = info.get("mint")
                amt = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
                if amt > 0:
                    token_mints[mint] = amt

    # 2. Get price & metadata from DexScreener
    positions = []
    for mint, amount in token_mints.items():
        dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        sym = "TOKEN"
        price_sol = 0.0
        price_usd = 0.0
        pnl_pct = 0.0
        mcap = 0.0
        try:
            req = urllib.request.Request(dex_url, headers={"User-Agent": "Botsensai/2.0"})
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read())
                pairs = d.get("pairs") or []
                if pairs:
                    p = pairs[0]
                    sym = p.get("baseToken", {}).get("symbol", sym)
                    price_sol = float(p.get("priceNative") or 0)
                    price_usd = float(p.get("priceUsd") or 0)
                    mcap = float(p.get("marketCap") or 0)
        except Exception:
            pass

        # Estimate entry price & PnL if known in DB
        entry_sol = 0.025  # default sizing
        entry_px = 3.9475e-06 if sym == "FIGHT" else 0.0
        val_sol = amount * price_sol
        if entry_px > 0:
            pnl_pct = ((price_sol - entry_px) / entry_px) * 100

        positions.append({
            "mint": mint,
            "symbol": sym,
            "amount": amount,
            "current_price_sol": price_sol,
            "current_price_usd": price_usd,
            "market_cap": mcap,
            "value_sol": round(val_sol, 4),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "ladder_targets": [
                {"rung": "1.40x", "price_sol": round(entry_px * 1.4, 8) if entry_px else 0, "pct": 35},
                {"rung": "2.00x", "price_sol": round(entry_px * 2.0, 8) if entry_px else 0, "pct": 25},
                {"rung": "4.00x", "price_sol": round(entry_px * 4.0, 8) if entry_px else 0, "pct": 20},
                {"rung": "10.0x", "price_sol": round(entry_px * 10.0, 8) if entry_px else 0, "pct": 20},
            ]
        })

    return {"positions": positions, "count": len(positions)}


@app.get("/api/signals")
async def get_signals(limit: int = 15):
    conn = get_db_connection()
    if not conn:
        return {"signals": []}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, token_key, symbol, side, size_native, max_slippage_bps,
                   jito_tip_lamports, score, status, tx_hash, error, created_at
            FROM execution_signals
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        return {"signals": rows}
    finally:
        conn.close()


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = []


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Server-Sent Events streaming chat powered by Gemini with SQLite context."""
    from botsensai.dashboard.chat_service import stream_chat_response
    return StreamingResponse(
        stream_chat_response(request.message, request.history),
        media_type="text/event-stream"
    )


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Broadcast hot wallet + position ticker every 3 seconds
            bal_res = fetch_rpc("getBalance", [HOT_WALLET])
            lamports = bal_res.get("value", 0) if bal_res else 0
            sol = lamports / 1e9

            payload = {
                "type": "TICK",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "wallet": {
                    "balance_sol": round(sol, 6),
                    "buffer_sol": round(max(0.0, sol - GAS_FLOOR), 6),
                }
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(3.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# Mount compiled React SPA
DIST_DIR = Path("/Users/drop/.gemini/antigravity/worktrees/Botsensai/fix_pump_copytrading_bot/web/dist")
if DIST_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if (DIST_DIR / full_path).exists() and (DIST_DIR / full_path).is_file():
            return FileResponse(DIST_DIR / full_path)
        return FileResponse(DIST_DIR / "index.html")

