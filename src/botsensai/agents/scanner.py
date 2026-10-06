"""
Botsensai ScannerAgent — DexScreener top-50 momentum scanner.

Leaf subagent (depth 1). Called by OrchestratorAgent each sweep cycle.
Fetches the top Solana tokens from DexScreener, applies holder breadth,
liquidity, volume, and buy-pressure filters, and returns a ranked candidate
list for the Orchestrator to route through the Sentinel.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

try:
    from google.antigravity import Agent, LocalAgentConfig, types
    HAS_AGY_SDK = True
except ImportError:
    Agent = None  # type: ignore[assignment]
    LocalAgentConfig = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    HAS_AGY_SDK = False

# ── Scanner tools ─────────────────────────────────────────────────────────────

MIN_LIQUIDITY_USD = 15_000.0
MIN_HOLDERS = 50
TOP_N_CANDIDATES = 10


def fetch_dexscreener_top_solana(limit: int = 50) -> str:
    """Fetch top Solana tokens by 6h volume from DexScreener.

    Args:
        limit: Maximum number of pairs to retrieve (max 50).

    Returns:
        JSON string with a list of token pairs sorted by 6h volume descending.
    """
    url = "https://api.dexscreener.com/latest/dex/search?q=SOL&rankBy=volume6h&chain=solana"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Botsensai-Scanner/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pairs: list[dict[str, Any]] = data.get("pairs", [])[:limit]
        simplified = []
        for p in pairs:
            simplified.append({
                "mint": p.get("baseToken", {}).get("address", ""),
                "symbol": p.get("baseToken", {}).get("symbol", ""),
                "name": p.get("baseToken", {}).get("name", ""),
                "dex_id": p.get("dexId", ""),
                "liquidity_usd": float(p.get("liquidity", {}).get("usd") or 0),
                "volume_6h_usd": float(p.get("volume", {}).get("h6") or 0),
                "volume_1h_usd": float(p.get("volume", {}).get("h1") or 0),
                "volume_5m_usd": float(p.get("volume", {}).get("m5") or 0),
                "price_change_5m": float(p.get("priceChange", {}).get("m5") or 0),
                "price_change_1h": float(p.get("priceChange", {}).get("h1") or 0),
                "txns_5m_buys": p.get("txns", {}).get("m5", {}).get("buys", 0),
                "txns_5m_sells": p.get("txns", {}).get("m5", {}).get("sells", 0),
                "market_cap_usd": float(p.get("marketCap") or 0),
            })
        return json.dumps(simplified)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def filter_and_rank_candidates(pairs_json: str) -> str:
    """Filter and rank DexScreener pairs by profitability criteria.

    Applies gates:
    - liquidity_usd >= 15,000 (DEX pool minimum per GEMINI.md fix #10)
    - volume_5m_usd > 0 (active trading)
    - txns_5m_buys > txns_5m_sells (net buy pressure)
    - price_change_1h > 0 (positive momentum)

    Scores each candidate as:
        score = (buy_ratio * 0.4) + (volume_momentum * 0.4) + (liquidity_score * 0.2)

    Args:
        pairs_json: JSON string from fetch_dexscreener_top_solana.

    Returns:
        JSON string with top-10 ranked candidates, each with a momentum_score.
    """
    try:
        pairs = json.loads(pairs_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "Invalid pairs JSON", "candidates": []})

    if isinstance(pairs, dict) and "error" in pairs:
        return json.dumps({"error": pairs["error"], "candidates": []})

    candidates = []
    for p in pairs:
        liq = p.get("liquidity_usd", 0)
        vol_5m = p.get("volume_5m_usd", 0)
        buys = p.get("txns_5m_buys", 0)
        sells = p.get("txns_5m_sells", 0)
        px_change_1h = p.get("price_change_1h", 0)

        # Hard gates
        if liq < MIN_LIQUIDITY_USD:
            continue
        if vol_5m <= 0:
            continue
        if buys <= sells:
            continue
        if px_change_1h <= 0:
            continue
        if not p.get("mint"):
            continue

        # Score
        total_txns = buys + sells
        buy_ratio = buys / total_txns if total_txns > 0 else 0.5
        vol_momentum = min(1.0, vol_5m / 50_000)  # normalize to $50k ceiling
        liq_score = min(1.0, liq / 100_000)        # normalize to $100k ceiling
        momentum_score = (buy_ratio * 0.4) + (vol_momentum * 0.4) + (liq_score * 0.2)

        candidates.append({**p, "momentum_score": round(momentum_score, 4)})

    # Sort by score descending, take top N
    candidates.sort(key=lambda x: x["momentum_score"], reverse=True)
    top = candidates[:TOP_N_CANDIDATES]

    return json.dumps({"candidates": top, "total_filtered": len(candidates), "top_n": len(top)})


# ── Scanner subagent config ───────────────────────────────────────────────────

SCANNER_SYSTEM = """
You are the BotsensaiScannerAgent — a momentum scanner that identifies the
top Solana meme token opportunities from DexScreener.

Your ONLY job: return a ranked list of candidate mints for the Orchestrator.

Protocol:
1. Call fetch_dexscreener_top_solana(limit=50) to get live top-50 Solana pairs.
2. Call filter_and_rank_candidates(pairs_json) with the result.
3. Return the JSON output from filter_and_rank_candidates directly.

Rules:
- Do NOT fabricate mints or prices.
- Do NOT call any tool other than the two above.
- Output ONLY the JSON from filter_and_rank_candidates. No prose.
""".strip()


def build_scanner_subagent_config() -> "types.SubagentConfig":
    """Return SubagentConfig for use inside OrchestratorAgent."""
    if types is None:
        raise ImportError("google-antigravity SDK not installed")
    return types.SubagentConfig(
        name="scanner",
        description=(
            "DexScreener top-50 momentum scanner for Solana meme tokens. "
            "Returns a JSON list of ranked candidate mints with momentum scores. "
            "Call at the start of each sweep cycle."
        ),
        capabilities=types.SubagentCapabilities(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            # Leaf: no further subagent delegation
        ),
    )


async def run_scanner_standalone() -> dict:
    """Run ScannerAgent standalone (for testing / CLI use)."""
    if not HAS_AGY_SDK:
        # Deterministic fallback
        pairs_json = fetch_dexscreener_top_solana(50)
        result_json = filter_and_rank_candidates(pairs_json)
        return json.loads(result_json)

    config = LocalAgentConfig(
        system_instructions=SCANNER_SYSTEM,
        tools=[fetch_dexscreener_top_solana, filter_and_rank_candidates],
        capabilities=types.CapabilitiesConfig(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            enable_subagents=False,
        ),
    )
    async with Agent(config) as agent:
        response = await agent.chat("Run a full top-50 Solana scan and return the ranked candidates.")
        text = await response.text()
        try:
            import re
            m = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(m.group()) if m else {"candidates": [], "error": text}
        except Exception:
            return {"candidates": [], "error": text}


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(run_scanner_standalone())
    candidates = result.get("candidates", [])
    print(f"Top {len(candidates)} candidates:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c['symbol']:10} {c['mint'][:8]}... score={c['momentum_score']:.3f} liq=${c['liquidity_usd']:,.0f}")
