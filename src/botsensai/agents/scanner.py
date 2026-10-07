"""
Botsensai ScannerAgent — DexScreener top-100 mid-rise momentum scanner.

Leaf subagent (depth 1). Called by OrchestratorAgent each sweep cycle.
Fetches the top Solana tokens from DexScreener across multiple search vectors,
applies the MID-RISE momentum algorithm, and returns a ranked candidate list
for the Orchestrator to route through the Sentinel.

Mid-Rise Definition:
  A token is "mid-rise" when it has sustained upward price momentum (h24 > 15%,
  h1 > 3%), volume is accelerating relative to its 24h baseline (last 1h vol >
  baseline), it has NOT gone parabolic in the last 5 min (m5 < 30%), and it
  has NOT been pumping for more than 48h (avoiding exhausted trends). The goal
  is to enter after initial price discovery but before the main crowd push.
"""

from __future__ import annotations

import json
import time
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

# ── Scanner constants ─────────────────────────────────────────────────────────

MIN_LIQUIDITY_USD = 15_000.0   # hard floor (GEMINI.md fix #10)
MAX_LIQUIDITY_USD = 600_000.0  # above this = institutional / harder to move
MIN_MID_RISE_SCORE = 40        # minimum score to pass to Sentinel
TOP_N_CANDIDATES = 10

# ── Firehose integration ───────────────────────────────────────────────────────
# Global buffer for candidate dicts received from the Helius Firehose listener.
# The listener will call `ingest_candidate` to add enriched token data here.
FIREHOSE_BUFFER: list[dict] = []

def ingest_candidate(candidate: dict) -> None:
    """Add a firehose‑derived candidate to the global buffer.

    The candidate dict is expected to have the same schema as the entries
    produced by `fetch_dexscreener_top_solana`.  We simply append it; the
    scanner will merge the buffer into the DexScreener results before scoring.
    """
    global FIREHOSE_BUFFER
    # Basic validation – ensure required keys exist.
    if not candidate.get("mint"):
        return
    FIREHOSE_BUFFER.append(candidate)


# Search vectors: broad keyword coverage of pump.fun meme ecosystem
_SEARCH_VECTORS = [
    "pump",    # pump.fun tokens directly
    "meme",    # meme category
    "dog",     # dog-themed (high pump.fun volume)
    "cat",     # cat-themed
    "ai",      # AI narrative tokens
    "pepe",    # pepe variants
    "sol",     # SOL-themed
    "inu",     # inu/dog variants
    "trump",   # political memes
    "baby",    # baby variants
]


# ── DexScreener fetcher ───────────────────────────────────────────────────────

def fetch_dexscreener_top_solana(limit: int = 100) -> str:
    """Fetch top Solana pump.fun tokens across multiple search vectors.

    Queries DexScreener across 10 search vectors to build a broad universe
    of pump.fun and pumpswap tokens, deduplicates by mint address, and returns
    up to `limit` unique pairs sorted by 24h volume descending.

    Filters to Solana-native DEXes only (pumpfun, pumpswap, raydium).

    Args:
        limit: Maximum unique pairs to return.

    Returns:
        JSON string with a list of token pairs, each enriched with mid-rise
        fields: age_hours, vol_1h_to_baseline_ratio, is_pump_native.
    """
    now_ms = time.time() * 1000
    seen: dict[str, dict[str, Any]] = {}

    for keyword in _SEARCH_VECTORS:
        url = (
            f"https://api.dexscreener.com/latest/dex/search"
            f"?q={keyword}&chainId=solana"
        )
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Botsensai-Scanner/2.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for p in data.get("pairs", []):
                # Solana-native DEXes only (no cross-chain noise)
                dex = p.get("dexId", "")
                if dex not in ("pumpfun", "pumpswap", "raydium", "orca", "meteora"):
                    continue
                mint = p.get("baseToken", {}).get("address", "")
                if not mint or mint in seen:
                    continue
                seen[mint] = p
        except Exception:
            continue

    # Fetch top boosted and latest boosted tokens from DexScreener (top movers & trending winners)
    boost_urls = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]
    boost_tokens: list[str] = []
    for b_url in boost_urls:
        try:
            req = urllib.request.Request(
                b_url, headers={"User-Agent": "Botsensai-Scanner/2.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                for item in data:
                    if item.get("chainId") == "solana":
                        addr = item.get("tokenAddress", "")
                        if addr and addr not in seen and addr not in boost_tokens:
                            boost_tokens.append(addr)
        except Exception:
            continue

    # Batch query boosted tokens in chunks of 30
    for i in range(0, min(len(boost_tokens), 60), 30):
        chunk = boost_tokens[i:i + 30]
        if not chunk:
            continue
        try:
            batch_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
            req = urllib.request.Request(
                batch_url, headers={"User-Agent": "Botsensai-Scanner/2.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                bdata = json.loads(resp.read().decode("utf-8"))
            for p in bdata.get("pairs", []):
                dex = p.get("dexId", "")
                if dex in ("pumpfun", "pumpswap", "raydium", "orca", "meteora"):
                    mint = p.get("baseToken", {}).get("address", "")
                    if mint and mint not in seen:
                        seen[mint] = p
        except Exception:
            continue

    # Simplify and enrich each pair
    simplified = []
    for p in seen.values():
        created_at = p.get("pairCreatedAt") or 0
        age_h = (now_ms - created_at) / 3_600_000 if created_at else 999.0

        vol_24h = float(p.get("volume", {}).get("h24") or 0)
        vol_1h = float(p.get("volume", {}).get("h1") or 0)
        # How active is the last 1h vs the 24h baseline hourly average?
        baseline_1h = vol_24h / 24.0 if vol_24h > 0 else 0.0
        vol_ratio = (vol_1h / baseline_1h) if baseline_1h > 0 else (1.0 if vol_1h > 0 else 0.0)

        dex = p.get("dexId", "")
        simplified.append({
            "mint": p.get("baseToken", {}).get("address", ""),
            "symbol": p.get("baseToken", {}).get("symbol", ""),
            "name": p.get("baseToken", {}).get("name", ""),
            "dex_id": dex,
            "is_pump_native": dex in ("pumpfun", "pumpswap"),
            "liquidity_usd": float(p.get("liquidity", {}).get("usd") or 0),
            "volume_24h_usd": vol_24h,
            "volume_6h_usd": float(p.get("volume", {}).get("h6") or 0),
            "volume_1h_usd": vol_1h,
            "volume_5m_usd": float(p.get("volume", {}).get("m5") or 0),
            "price_change_24h": float(p.get("priceChange", {}).get("h24") or 0),
            "price_change_6h": float(p.get("priceChange", {}).get("h6") or 0),
            "price_change_1h": float(p.get("priceChange", {}).get("h1") or 0),
            "price_change_5m": float(p.get("priceChange", {}).get("m5") or 0),
            "txns_5m_buys": p.get("txns", {}).get("m5", {}).get("buys", 0),
            "txns_5m_sells": p.get("txns", {}).get("m5", {}).get("sells", 0),
            "txns_1h_buys": p.get("txns", {}).get("h1", {}).get("buys", 0),
            "txns_1h_sells": p.get("txns", {}).get("h1", {}).get("sells", 0),
            "market_cap_usd": float(p.get("marketCap") or 0),
            "age_hours": round(age_h, 2),
            "vol_1h_to_baseline_ratio": round(vol_ratio, 2),
        })

    # Sort by 24h volume descending, take top limit
    simplified.sort(key=lambda x: x["volume_24h_usd"], reverse=True)
    return json.dumps(simplified[:limit])


# ── Mid-rise scoring ─────────────────────────────────────────────────────────

def filter_and_rank_candidates(pairs_json: str) -> str:
    """Score and rank pairs using the Mid-Rise Momentum Algorithm.

    Mid-Rise Scoring (100 points max):
      +30  24h gain in sweet spot (15–800%) — rising but not exhausted
      +25  h1 gain > 3% — still actively moving up
      +15  m5 < 30% AND m5 > -5% — not blow-off top, not crashing
      +20  vol_1h_to_baseline_ratio > 1.2x — volume accelerating
      +15  liquidity $15k–$600k — tradeable size
      +10  age 0.5h–36h — past launch dump, before exhaustion
      -20  h24 > 800% — likely near peak / already pumped
      -30  h1 < -10% — actively dumping
      -10  m5 > 50% — parabolic blow-off top signal
      -50  liquidity < $15k — below DEX floor
      -15  age > 48h — trend likely exhausted

    Additional pump.fun bonus:
      +5   is_pump_native (pumpfun or pumpswap DEX) — on-chain pedigree

    Gates (must pass ALL to qualify):
      - liquidity_usd >= 15,000
      - mint address present
      - NOT actively dumping (h1 >= -20%)
      - Total mid_rise_score >= MIN_MID_RISE_SCORE (40)

    Args:
        pairs_json: JSON string from fetch_dexscreener_top_solana.

    Returns:
        JSON with keys: candidates (list), total_scanned, total_qualified, top_n.
        Each candidate includes mid_rise_score and score_breakdown.
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
        mint = p.get("mint", "")
        h24 = p.get("price_change_24h", 0)
        h1 = p.get("price_change_1h", 0)
        m5 = p.get("price_change_5m", 0)
        vol_ratio = p.get("vol_1h_to_baseline_ratio", 0)
        age_h = p.get("age_hours", 999)

        # Hard gates
        if liq < MIN_LIQUIDITY_USD:
            continue
        if not mint:
            continue
        if h1 < -20:  # actively dumping
            continue

        # Mid-rise scoring
        score = 0
        breakdown: dict[str, int] = {}

        # 24h gain sweet spot
        if 15 <= h24 <= 800:
            score += 30; breakdown["h24_gain"] = 30
        elif h24 > 800:
            score -= 20; breakdown["h24_too_high"] = -20
        # else flat/negative — no bonus

        # h1 momentum
        if h1 > 3:
            score += 25; breakdown["h1_rising"] = 25
        elif h1 < -10:
            score -= 30; breakdown["h1_dumping"] = -30

        # m5 not blow-off
        if -5 < m5 < 30:
            score += 15; breakdown["m5_stable"] = 15
        elif m5 > 50:
            score -= 10; breakdown["m5_parabolic"] = -10

        # Volume accelerating
        if vol_ratio > 1.5:
            score += 20; breakdown["vol_accel_strong"] = 20
        elif vol_ratio > 1.2:
            score += 15; breakdown["vol_accel"] = 15
        elif vol_ratio > 0.8:
            score += 8; breakdown["vol_steady"] = 8

        # Liquidity sweet spot
        if MIN_LIQUIDITY_USD <= liq <= MAX_LIQUIDITY_USD:
            score += 15; breakdown["liq_ok"] = 15
        elif liq > MAX_LIQUIDITY_USD:
            score += 5; breakdown["liq_high"] = 5  # good but harder to move

        # Age sweet spot (past initial dump, before exhaustion)
        if 0.5 <= age_h <= 12:
            score += 10; breakdown["age_early"] = 10
        elif 12 < age_h <= 36:
            score += 5; breakdown["age_mid"] = 5
        elif age_h > 48:
            score -= 15; breakdown["age_old"] = -15

        # pump.fun native bonus
        if p.get("is_pump_native"):
            score += 5; breakdown["pump_native"] = 5

        # Score gate
        if score < MIN_MID_RISE_SCORE:
            continue

        # Buy pressure from txns
        buys_5m = p.get("txns_5m_buys", 0)
        sells_5m = p.get("txns_5m_sells", 0)
        total_5m = buys_5m + sells_5m
        buy_ratio_5m = buys_5m / total_5m if total_5m > 0 else 0.5

        buys_1h = p.get("txns_1h_buys", 0)
        sells_1h = p.get("txns_1h_sells", 0)
        total_1h = buys_1h + sells_1h
        buy_ratio_1h = buys_1h / total_1h if total_1h > 0 else 0.5

        candidates.append({
            **p,
            "mid_rise_score": score,
            "momentum_score": round(min(max(score / 100.0, 0.0), 1.0), 3),
            "score_breakdown": breakdown,
            "buy_ratio_5m": round(buy_ratio_5m, 3),
            "buy_ratio_1h": round(buy_ratio_1h, 3),
        })

    # Sort: mid_rise_score desc, then 1h vol desc
    candidates.sort(
        key=lambda x: (x["mid_rise_score"], x["volume_1h_usd"]),
        reverse=True,
    )
    top = candidates[:TOP_N_CANDIDATES]

    return json.dumps({
        "candidates": top,
        "total_scanned": len(pairs),
        "total_qualified": len(candidates),
        "top_n": len(top),
        "algorithm": "mid_rise_v2",
    })


# ── Scanner system prompt ─────────────────────────────────────────────────────

SCANNER_SYSTEM = """
You are the BotsensaiScannerAgent — a mid-rise momentum scanner for Solana
pump.fun tokens. Your goal is to find tokens that are IN THE MIDDLE of their
rise: past the initial launch dump, volume accelerating, price still climbing,
liquidity healthy, not yet parabolic or exhausted.

Your ONLY job: return a ranked candidate list using the Mid-Rise algorithm.

Protocol:
1. Call fetch_dexscreener_top_solana(limit=100) — scans 10 search vectors.
2. Call filter_and_rank_candidates(pairs_json) — applies Mid-Rise scoring.
3. Return the JSON from filter_and_rank_candidates ONLY. No prose.

Mid-Rise criteria (target tokens):
  - 24h gain 15–800% (rising but not exhausted)
  - h1 > 3% (still actively moving up right now)
  - m5 < 30% (not blow-off parabolic top)
  - Volume last 1h > baseline hourly average (accelerating, not fading)
  - Liquidity $15k–$600k (tradeable, not institutional)
  - Age 0.5h–36h (past launch dump, before trend exhaustion)
  - pump.fun or pumpswap DEX preferred

Rules:
- Do NOT fabricate mints or prices.
- Do NOT call any tool other than the two above.
- Output ONLY the JSON from filter_and_rank_candidates.
""".strip()


# ── Subagent config ───────────────────────────────────────────────────────────

def build_scanner_subagent_config() -> "types.SubagentConfig":
    """Return SubagentConfig for use inside OrchestratorAgent."""
    if types is None:
        raise ImportError("google-antigravity SDK not installed")
    return types.SubagentConfig(
        name="scanner",
        description=(
            "DexScreener top-100 mid-rise momentum scanner for Solana pump.fun tokens. "
            "Scans 10 search vectors, scores each token for mid-rise characteristics "
            "(h24 gain, h1 momentum, volume acceleration, age, liquidity), and returns "
            "a ranked JSON list of candidate mints. Call each sweep cycle."
        ),
        capabilities=types.SubagentCapabilities(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
        ),
    )


# ── Standalone runner ─────────────────────────────────────────────────────────

async def run_scanner_standalone() -> dict:
    """Run ScannerAgent standalone (for testing / CLI use)."""
    if not HAS_AGY_SDK:
        pairs_json = fetch_dexscreener_top_solana(100)
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
        response = await agent.chat(
            "Run a full top-100 Solana mid-rise scan and return the ranked candidates."
        )
        text = await response.text()
        try:
            import re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            return json.loads(m.group()) if m else {"candidates": [], "error": text}
        except Exception:
            return {"candidates": [], "error": text}


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(run_scanner_standalone())
    candidates = result.get("candidates", [])
    total = result.get("total_scanned", "?")
    qualified = result.get("total_qualified", "?")
    print(f"Scanned {total} tokens → {qualified} qualified → top {len(candidates)}:")
    print()
    for i, c in enumerate(candidates, 1):
        print(
            f"  {i:2d}. [{c['mid_rise_score']:3d}] {c['symbol']:12s} "
            f"{c['mint'][:10]}  "
            f"h24={c['price_change_24h']:+6.0f}%  "
            f"h1={c['price_change_1h']:+5.0f}%  "
            f"m5={c['price_change_5m']:+4.1f}%  "
            f"liq=${c['liquidity_usd']/1000:.0f}k  "
            f"age={c['age_hours']:.1f}h  "
            f"vol_ratio={c['vol_1h_to_baseline_ratio']:.1f}x  "
            f"dex={c['dex_id']}"
        )
        print(f"       breakdown: {c['score_breakdown']}")
