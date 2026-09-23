"""Botsensai Gemini Live Audio & Voice Trading Copilot.

Powered by `gemini-3.1-flash-live-preview` via Google GenAI SDK.
Enables real-time bidirectional voice dialogue, autonomous audio callouts
for take-profits, moonbag triggers, and smart money confluence alerts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import urllib.request
from typing import Any

from google import genai
from google.genai import types

HOT_WALLET_PUBKEY = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
DEFAULT_DB_PATH = "data/botsensai.db"
STARTING_SOL_BASELINE = 0.420004
MIN_RESERVE_FLOOR = 0.10


class BotsensaiLiveCopilot:
    """Real-time voice and audio copilot for Botsensai live trading."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, api_key: str | None = None) -> None:
        self.db_path = db_path
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")
        self.client = genai.Client(api_key=self.api_key)
        self.running = True

    # -- Trading State Query Tools -- #

    def get_wallet_metrics(self) -> dict[str, Any]:
        """Queries on-chain balance for the live hot wallet."""
        bal_sol = 0.0
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [HOT_WALLET_PUBKEY],
            }
            req = urllib.request.Request(
                "https://api.mainnet-beta.solana.com",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode())
                bal_sol = data.get("result", {}).get("value", 0) / 1e9
        except Exception as e:
            return {"error": f"Failed to fetch on-chain wallet balance: {e}"}

        net_growth = bal_sol - STARTING_SOL_BASELINE
        growth_pct = (net_growth / STARTING_SOL_BASELINE) * 100.0
        floor_buffer = bal_sol - MIN_RESERVE_FLOOR

        return {
            "wallet_address": HOT_WALLET_PUBKEY,
            "liquid_sol_balance": round(bal_sol, 6),
            "reserve_floor_sol": MIN_RESERVE_FLOOR,
            "safety_buffer_above_floor_sol": round(floor_buffer, 6),
            "net_cash_growth_sol": round(net_growth, 6),
            "net_cash_growth_pct": round(growth_pct, 2),
            "status": "HEALTHY" if floor_buffer > 0.05 else "NEAR_FLOOR",
        }

    def get_open_runners(self) -> dict[str, Any]:
        """Queries all currently active open positions in live_positions."""
        if not os.path.exists(self.db_path):
            return {"open_positions": [], "total_open": 0}

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT mint, symbol, amount_token, cost_sol, entry_price_sol,
                           peak_price_sol, last_price_sol, exit_reason, opened_at
                    FROM live_positions
                    WHERE status = 'OPEN' AND amount_token > 0
                    ORDER BY opened_at DESC
                    """
                ).fetchall()

                positions = []
                now = time.time()
                for r in rows:
                    entry_px = float(r["entry_price_sol"])
                    curr_px = float(r["last_price_sol"] or entry_px)
                    peak_px = float(r["peak_price_sol"] or entry_px)
                    multiple = curr_px / entry_px if entry_px > 0 else 1.0
                    peak_multiple = peak_px / entry_px if entry_px > 0 else 1.0
                    age_min = (now - float(r["opened_at"])) / 60.0

                    is_moonbag = "stage1" in str(r["exit_reason"] or "").lower()

                    positions.append(
                        {
                            "symbol": r["symbol"] or "TOKEN",
                            "mint": r["mint"],
                            "tokens_held": round(float(r["amount_token"]), 2),
                            "cost_sol": round(float(r["cost_sol"]), 4),
                            "entry_price_sol": entry_px,
                            "current_multiple": f"{multiple:.2f}x",
                            "peak_multiple": f"{peak_multiple:.2f}x",
                            "age_minutes": round(age_min, 1),
                            "is_free_roll_moonbag": is_moonbag,
                            "exit_stage": r["exit_reason"] or "ENTRY",
                        }
                    )
                return {"open_positions": positions, "total_open": len(positions)}
        except Exception as e:
            return {"error": f"Failed to query open runners: {e}"}

    def get_realized_profits(self) -> dict[str, Any]:
        """Queries confirmed on-chain closed trades."""
        if not os.path.exists(self.db_path):
            return {"closed_trades": [], "total_closed": 0}

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT symbol, mint, cost_sol, realized_pnl_sol, exit_reason, exit_tx_hash, closed_at
                    FROM live_positions
                    WHERE status = 'CLOSED'
                    ORDER BY closed_at DESC
                    LIMIT 10
                    """
                ).fetchall()

                trades = []
                for r in rows:
                    trades.append(
                        {
                            "symbol": r["symbol"] or "TOKEN",
                            "mint": r["mint"],
                            "cost_sol": round(float(r["cost_sol"]), 4),
                            "realized_pnl_sol": round(float(r["realized_pnl_sol"]), 4),
                            "exit_reason": r["exit_reason"],
                            "exit_tx_hash": r["exit_tx_hash"],
                        }
                    )
                return {"closed_trades": trades, "total_closed": len(trades)}
        except Exception as e:
            return {"error": f"Failed to query realized profits: {e}"}

    def get_smart_money_confluence(self) -> dict[str, Any]:
        """Queries recent alpha wallet copy-trade convergence events."""
        if not os.path.exists(self.db_path):
            return {"confluence_events": [], "total_events": 0}

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT symbol, mint, trader_wallet, trader_skill, conviction_boost, recommended_size_sol, created_at
                    FROM copy_trade_events
                    ORDER BY created_at DESC
                    LIMIT 8
                    """
                ).fetchall()

                events = []
                for r in rows:
                    events.append(
                        {
                            "symbol": r["symbol"] or "TOKEN",
                            "mint": r["mint"],
                            "trader_wallet": r["trader_wallet"][:8] + "...",
                            "trader_skill": r["trader_skill"],
                            "conviction_boost": r["conviction_boost"],
                            "recommended_size_sol": r["recommended_size_sol"],
                        }
                    )
                return {"confluence_events": events, "total_events": len(events)}
        except Exception as e:
            return {"error": f"Failed to query confluence events: {e}"}

    # -- Tool Dispatcher -- #

    def execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatches tool calls from Gemini Live to local methods."""
        if name == "get_wallet_metrics":
            return self.get_wallet_metrics()
        elif name == "get_open_runners":
            return self.get_open_runners()
        elif name == "get_realized_profits":
            return self.get_realized_profits()
        elif name == "get_smart_money_confluence":
            return self.get_smart_money_confluence()
        return {"error": f"Unknown tool: {name}"}

    def _get_connect_config(self) -> types.LiveConnectConfig:
        system_instruction = (
            "You are Botsensai Live Copilot, the real-time voice-activated trading copilot for Botsensai 2.0 on Solana. "
            "You speak concisely, like a confident and sharp Wall Street/crypto prop desk trader. "
            "You have direct real-time access to our Solana mainnet hot wallet, active token positions, "
            "6 profit maximization methods (dynamic bankroll scaling, 2.5x partial 60% shave with moonbag, "
            "multi-pool routing, conviction-tiered tips, 15m stagnation recycler, and smart money confluence), "
            "and closed trade profits ($Cateoween +163.5%, $FOREX +101.5%). "
            "Always use the available tools to fetch live on-chain truth before answering questions about balances or positions. "
            "Keep spoken responses punchy, direct, and under 3 sentences unless asked for an in-depth breakdown."
        )

        tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="get_wallet_metrics",
                        description="Fetches current Solana hot wallet balance, reserve floor buffer, and net cash growth.",
                        parameters=types.Schema(type="OBJECT", properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_open_runners",
                        description="Fetches all currently open live token positions, entry prices, current multipliers, and moonbag states.",
                        parameters=types.Schema(type="OBJECT", properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_realized_profits",
                        description="Fetches all closed trades with confirmed on-chain transaction hashes, net SOL banked, and ROI percentages.",
                        parameters=types.Schema(type="OBJECT", properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_smart_money_confluence",
                        description="Fetches recent multi-wallet smart trader copy-trade confluence alerts.",
                        parameters=types.Schema(type="OBJECT", properties={}),
                    ),
                ]
            )
        ]

        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
            tools=tools,
        )

    # -- Interactive Voice & Audio Session -- #

    async def start_voice_session(self) -> None:
        """Starts an interactive real-time voice and audio session with Gemini Live API."""
        config = self._get_connect_config()

        print("\n" + "=" * 65)
        print("🎙  BOTSENSAI GEMINI LIVE VOICE COPILOT INITIALIZED")
        print("   Model: gemini-3.1-flash-live-preview (WebSocket Audio)")
        print("   Hot Wallet: " + HOT_WALLET_PUBKEY)
        print("=" * 65 + "\n")

        async with self.client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
            print("🟢 Connected to Gemini Live API. Initializing briefing...\n")

            # Send initial briefing prompt
            await session.send_realtime_input(
                text="Sensai is here. Give a sharp 2-sentence opening greeting and summarize our hot wallet status."
            )

            async for response in session.receive():
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        fn_name = fc.name
                        fn_args = fc.args or {}
                        print(f"\n[COPILOT TOOL] 🛠 Calling {fn_name}({fn_args})...")
                        res = self.execute_tool(fn_name, fn_args)
                        print(f"[COPILOT TOOL] ✓ Result: {res}")
                        await session.send_tool_response(
                            function_responses=[
                                types.FunctionResponse(
                                    name=fn_name,
                                    id=fc.id,
                                    response=res,
                                )
                            ]
                        )

                server_content = response.server_content
                if server_content:
                    if server_content.output_transcription and server_content.output_transcription.text:
                        print(server_content.output_transcription.text, end="", flush=True)

                    if server_content.turn_complete:
                        print("\n")
                        break

    # -- Autonomous Audio Alert Daemon -- #

    async def run_alert_daemon(self, check_interval: float = 5.0) -> None:
        """Runs in background, monitoring DB and vocalizing live trade alerts via Gemini Live API."""
        config = self._get_connect_config()
        print("\n" + "=" * 65)
        print("🚨 BOTSENSAI GEMINI LIVE AUDIO ALERT DAEMON ACTIVE")
        print("   Model: gemini-3.1-flash-live-preview")
        print("   Monitoring live trades & smart money alerts...")
        print("=" * 65 + "\n")

        last_known_exits: set[str] = set()
        last_known_confluences: set[int] = set()

        # Seed initial known state
        if os.path.exists(self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                for row in conn.execute("SELECT exit_tx_hash FROM live_positions WHERE exit_tx_hash IS NOT NULL").fetchall():
                    last_known_exits.add(row[0])
                for row in conn.execute("SELECT id FROM copy_trade_events").fetchall():
                    last_known_confluences.add(row[0])

        async with self.client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
            while self.running:
                try:
                    if os.path.exists(self.db_path):
                        with sqlite3.connect(self.db_path) as conn:
                            conn.row_factory = sqlite3.Row
                            # 1. Check for fresh exits / take profits / recycling
                            exit_rows = conn.execute(
                                """
                                SELECT symbol, exit_reason, exit_tx_hash, realized_pnl_sol
                                FROM live_positions
                                WHERE status = 'CLOSED' AND exit_tx_hash IS NOT NULL
                                ORDER BY closed_at DESC LIMIT 3
                                """
                            ).fetchall()
                            for r in exit_rows:
                                tx = r["exit_tx_hash"]
                                if tx not in last_known_exits:
                                    last_known_exits.add(tx)
                                    sym = r["symbol"] or "TOKEN"
                                    reason = r["exit_reason"] or "Exit"
                                    pnl = float(r["realized_pnl_sol"] or 0.0)
                                    alert_msg = (
                                        f"ALERT TO ANNOUNCE: Live exit confirmed on ${sym}. "
                                        f"Reason: {reason}. Realized PnL: {pnl:+.4f} SOL. "
                                        "Announce this alert to Sensai right now in one sharp sentence."
                                    )
                                    print(f"\n[DAEMON] 🚨 Triggering voice alert for ${sym}: {reason}")
                                    await session.send_realtime_input(text=alert_msg)
                                    async for resp in session.receive():
                                        if resp.server_content and resp.server_content.output_transcription:
                                            print(resp.server_content.output_transcription.text, end="", flush=True)
                                        if resp.server_content and resp.server_content.turn_complete:
                                            print("\n")
                                            break

                            # 2. Check for fresh copy-trade confluence events
                            conf_rows = conn.execute(
                                """
                                SELECT id, symbol, trader_wallet, recommended_size_sol
                                FROM copy_trade_events
                                ORDER BY id DESC LIMIT 3
                                """
                            ).fetchall()
                            for r in conf_rows:
                                cid = r["id"]
                                if cid not in last_known_confluences:
                                    last_known_confluences.add(cid)
                                    sym = r["symbol"] or "TOKEN"
                                    size = float(r["recommended_size_sol"] or 0.02)
                                    conf_msg = (
                                        f"ALERT TO ANNOUNCE: Smart trader confluence detected on ${sym}! "
                                        f"Tracked alpha wallets entered. Scaled position to {size:.3f} SOL. "
                                        "Announce this alert to Sensai in one punchy sentence."
                                    )
                                    print(f"\n[DAEMON] 🚨 Triggering voice alert for confluence on ${sym}!")
                                    await session.send_realtime_input(text=conf_msg)
                                    async for resp in session.receive():
                                        if resp.server_content and resp.server_content.output_transcription:
                                            print(resp.server_content.output_transcription.text, end="", flush=True)
                                        if resp.server_content and resp.server_content.turn_complete:
                                            print("\n")
                                            break

                except Exception as e:
                    print(f"[DAEMON] Warning: {e}")

                await asyncio.sleep(check_interval)


if __name__ == "__main__":
    copilot = BotsensaiLiveCopilot()
    asyncio.run(copilot.start_voice_session())
