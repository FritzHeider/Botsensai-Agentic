"""
Botsensai OrchestratorAgent — root multi-agent coordinator.

Runs a periodic 300-second sweep cycle:
  1. Spawns ScannerAgent → gets ranked candidate list
  2. For each candidate with momentum_score >= 0.65:
     a. Spawns SentinelAgent → security audit
     b. If APPROVE → signals pipeline.execute_signal()
  3. Reports sweep summary

Subagent hierarchy (max depth=2):
  Orchestrator (depth 0)
    └── sentinel (depth 1, leaf)
    └── scanner  (depth 1, leaf)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

try:
    from google.antigravity import Agent, LocalAgentConfig, types
    from google.antigravity.triggers import every, TriggerContext
    from google.antigravity.hooks import hooks
    HAS_AGY_SDK = True
except ImportError:
    Agent = None  # type: ignore[assignment]
    LocalAgentConfig = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    every = None  # type: ignore[assignment]
    TriggerContext = None  # type: ignore[assignment]
    hooks = None  # type: ignore[assignment]
    HAS_AGY_SDK = False

from botsensai.agents.scanner import (
    build_scanner_subagent_config,
    fetch_dexscreener_top_solana,
    filter_and_rank_candidates,
)
from botsensai.agents.sentinel import (
    BotsensaiSentinelAgent,
    build_sentinel_subagent_config,
)
from botsensai.agents.tools import check_wallet_reserve_floor

log = logging.getLogger("orchestrator")

SWEEP_INTERVAL_SECONDS = 300  # 5 minutes — matches existing pipeline cadence
MIN_MOMENTUM_SCORE = 0.65     # Minimum scanner score to trigger sentinel audit

# ── Orchestrator tools (visible to root agent only) ───────────────────────────

def get_wallet_status() -> str:
    """Check live hot wallet balance and trading headroom.

    Returns:
        JSON with balance_sol, trading_allowed, max_position_size_sol.
    """
    return check_wallet_reserve_floor()


def signal_pipeline_entry(
    mint: str,
    symbol: str,
    momentum_score: float,
    conviction_score: float,
    liquidity_usd: float,
    sentiment: str = "ORCHESTRATOR_SIGNAL",
) -> str:
    """Signal the Botsensai pipeline to execute a buy entry for a validated token.

    Only call this AFTER the sentinel returns verdict=APPROVE.
    The pipeline enforces all capital rails (gas floor, max position size, slippage).

    Args:
        mint: Solana mint address of the token.
        symbol: Token symbol for logging.
        momentum_score: Scanner momentum score (0.0–1.0).
        conviction_score: Sentinel conviction score (0.0–1.0).
        liquidity_usd: Pool liquidity in USD at time of signal.
        sentiment: Label for this signal source.

    Returns:
        JSON with status and any pipeline error.
    """
    try:
        # Import pipeline lazily to avoid circular import
        from botsensai.pipeline import Pipeline  # noqa: PLC0415

        pipeline = Pipeline.get_instance()
        if pipeline is None:
            return json.dumps({
                "status": "PIPELINE_UNAVAILABLE",
                "mint": mint,
                "note": "Pipeline not running — signal logged but not executed",
            })

        combined_score = (momentum_score * 0.4) + (conviction_score * 0.6)
        result = pipeline.orchestrator_signal(
            mint=mint,
            symbol=symbol,
            composite_score=combined_score,
            liquidity_usd=liquidity_usd,
            source=sentiment,
        )
        return json.dumps({"status": "SIGNALED", "mint": mint, "result": str(result)})
    except Exception as exc:
        log.warning(f"Pipeline signal failed for {mint}: {exc}")
        return json.dumps({"status": "ERROR", "mint": mint, "error": str(exc)})


# ── Orchestrator system instructions ─────────────────────────────────────────

ORCHESTRATOR_SYSTEM = """
You are the BotsensaiOrchestratorAgent — the root trading coordinator for the
Botsensai autonomous Solana meme token trading system.

You run a continuous sweep cycle every 5 minutes.

SWEEP CYCLE PROTOCOL (mandatory execution order):
1. Call get_wallet_status() — abort cycle if trading_allowed=false.
2. Use the 'scanner' subagent to fetch and rank the top Solana opportunities.
   Prompt: "Run a full top-50 Solana scan and return ranked candidates JSON."
3. Parse the candidates list. For each candidate with momentum_score >= 0.65:
   a. Use the 'sentinel' subagent to audit the mint.
      Prompt: f"Audit this token mint: {mint}"
   b. Parse the sentinel JSON verdict.
   c. If verdict == "APPROVE" AND conviction_score >= 0.80:
      Call signal_pipeline_entry(mint, symbol, momentum_score, conviction_score, liquidity_usd).
4. Log a sweep summary: candidates_scanned, sentinel_approved, entries_signaled.

HARD RULES (from GEMINI.md — NEVER violate):
- NEVER signal an entry if get_wallet_status() returns trading_allowed=false.
- NEVER signal an entry without sentinel APPROVE verdict.
- NEVER call signal_pipeline_entry for a mint the sentinel flagged as QUARANTINE_DUST or VETO.
- Position sizes are enforced by the pipeline — do NOT set them here.
- Only route through Helius RPC (mainnet.helius-rpc.com) — never third-party proxies.
- Max 10 candidates per cycle (scanner already caps at 10).
""".strip()


# ── Stop hook: ensure cycle summary is always emitted ─────────────────────────

if HAS_AGY_SDK and hooks is not None:
    @hooks.stop
    async def ensure_summary(data: "types.StopArgs") -> "types.StopHookResult":
        """Ensure sweep summary is emitted before stopping."""
        text = data.response_text.lower() if data.response_text else ""
        if data.continuation_count == 0 and "sweep summary" not in text and "candidates_scanned" not in text:
            return types.StopHookResult(
                decision=types.StopDecision.CONTINUE,
                reason="Please emit a sweep summary with candidates_scanned, sentinel_approved, and entries_signaled before finishing.",
            )
        return types.StopHookResult(decision=types.StopDecision.ALLOW_STOP)


# ── Periodic trigger ──────────────────────────────────────────────────────────

_orchestrator_agent: "Agent | None" = None


async def sweep_trigger(ctx: "TriggerContext") -> None:
    """Fires every SWEEP_INTERVAL_SECONDS to kick off a new sweep cycle."""
    log.info(f"[Orchestrator] Sweep trigger fired — starting cycle")
    await ctx.send(
        "Execute one full sweep cycle: check wallet, scan top-50 Solana, "
        "audit viable candidates via sentinel, and signal any approved entries. "
        "End with a sweep summary."
    )


def build_orchestrator_config() -> "LocalAgentConfig":
    """Build the root OrchestratorAgent LocalAgentConfig."""
    if not HAS_AGY_SDK:
        raise ImportError("google-antigravity SDK not installed")

    sentinel_cfg = build_sentinel_subagent_config()
    scanner_cfg = build_scanner_subagent_config()

    trigger = every(SWEEP_INTERVAL_SECONDS, sweep_trigger)

    config = LocalAgentConfig(
        system_instructions=ORCHESTRATOR_SYSTEM,
        tools=[get_wallet_status, signal_pipeline_entry],
        subagents=[sentinel_cfg, scanner_cfg],
        triggers=[trigger],
        capabilities=types.CapabilitiesConfig(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            enable_subagents=True,
            max_subagent_depth=2,
            allowed_subagents=["scanner", "sentinel"],
        ),
        hooks=[ensure_summary] if HAS_AGY_SDK else [],
    )
    return config


async def run_orchestrator() -> None:
    """Start the OrchestratorAgent event loop (blocking)."""
    global _orchestrator_agent
    if not HAS_AGY_SDK:
        log.error("google-antigravity SDK not installed — running deterministic fallback loop")
        await _run_deterministic_loop()
        return

    config = build_orchestrator_config()
    log.info("[Orchestrator] Starting AGY multi-agent orchestrator")
    async with Agent(config) as agent:
        _orchestrator_agent = agent
        # Kick off the first sweep immediately
        response = await agent.chat(
            "Execute your first sweep cycle now: check wallet, scan top-50 Solana tokens, "
            "audit viable candidates, signal any approved entries, and emit a sweep summary."
        )
        log.info(f"[Orchestrator] First sweep complete: {await response.text()}")
        # Subsequent sweeps fire from the periodic trigger
        # Keep alive indefinitely
        while True:
            await asyncio.sleep(60)


async def _run_deterministic_loop() -> None:
    """Fallback deterministic loop when AGY SDK unavailable."""
    sentinel = BotsensaiSentinelAgent()
    cycle = 0
    while True:
        cycle += 1
        log.info(f"[Orchestrator] Deterministic sweep cycle {cycle}")

        wallet_raw = check_wallet_reserve_floor()
        wallet = json.loads(wallet_raw)
        if not wallet.get("trading_allowed", False):
            log.warning(f"[Orchestrator] Trading halted — insufficient capital buffer")
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            continue

        pairs_json = fetch_dexscreener_top_solana(50)
        ranked_json = filter_and_rank_candidates(pairs_json)
        ranked = json.loads(ranked_json)
        candidates: list[dict[str, Any]] = ranked.get("candidates", [])

        approved = 0
        signaled = 0
        for c in candidates:
            if c.get("momentum_score", 0) < MIN_MOMENTUM_SCORE:
                continue
            report = await sentinel.audit_token(c["mint"])
            if report.verdict == "APPROVE" and report.conviction_score >= 0.80:
                result_json = signal_pipeline_entry(
                    mint=c["mint"],
                    symbol=c["symbol"],
                    momentum_score=c["momentum_score"],
                    conviction_score=report.conviction_score,
                    liquidity_usd=c["liquidity_usd"],
                )
                result = json.loads(result_json)
                if result.get("status") == "SIGNALED":
                    signaled += 1
                approved += 1

        log.info(
            f"[Orchestrator] Sweep {cycle} summary — "
            f"candidates_scanned={len(candidates)} sentinel_approved={approved} entries_signaled={signaled}"
        )
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_orchestrator())
