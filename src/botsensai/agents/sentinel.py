"""Autonomous AI Risk & Alpha Sentinel Agent for Botsensai.

Powered by Google Antigravity SDK (AGY).
Evaluates candidate Solana tokens, audits Token-2022 authorities, checks AMM liquidity,
enforces GEMINI.md capital rails, and produces structured risk/conviction reports.
"""

import asyncio
from typing import Optional

try:
    from google.antigravity import Agent, LocalAgentConfig
    from google.antigravity.hooks import policy
    HAS_AGY_SDK = True
except ImportError:
    Agent = None
    LocalAgentConfig = None
    policy = None
    HAS_AGY_SDK = False

from botsensai.agents.schemas import LiquidityMetrics, TokenAuthorities, TokenRiskReport
from botsensai.agents.tools import (
    check_wallet_reserve_floor,
    dexscreener_get_pairs,
    helius_get_asset,
    quarantine_dust_token,
)

SENTINEL_SYSTEM_INSTRUCTIONS = """You are the Botsensai Autonomous Risk & Alpha Sentinel Agent, an expert Solana meme trader and on-chain security auditor.

Your mission is to perform strict, zero-trust pre-trade auditing on candidate Solana meme tokens before any capital is committed.

CORE OPERATIONAL RULES (MANDATORY & ABSOLUTE):
1. RULE 1 (ZERO SIMULATION DATA): Only analyze verified on-chain cryptographic facts and actual liquidity flow. Never invent or hallucinate volume or price history.
2. RULE 2 (CAPITAL PRESERVATION FLOOR): The hot wallet MUST maintain >= 0.0100 SOL (operational gas & rent floor) at all times. A minimum 0.0030 SOL buffer is required above the floor before new entries are permissible. Active entry sizing is 0.015 - 0.025 SOL (max allocation 0.035 SOL). Always call `check_wallet_reserve_floor` to verify capital headroom.
3. RULE 5 (ANTI-PHISHING & DUST QUARANTINE):
   - Unsolicited promotional ad tokens (e.g., tokens named 'SWITCH TO PUMPDEV.IO', 'SWITCH TO PUMPAPI.IO', or containing promotional copy in metadata) are dangerous phishing spam.
   - Any token with active Token-2022 `permanent_delegate` or live `freeze_authority` owned by untrusted third parties MUST be IMMEDIATELY VETOED and assigned verdict 'QUARANTINE_DUST'.
   - Call `quarantine_dust_token` when encountering these tokens.
4. HONEYPOT & RUG DETECTION:
   - Tokens with 0 USD liquidity or single-holder supply concentration (>80%) must be VETOED.
   - Genuine parabolic runners must demonstrate clean authority structure (mint authority revoked, freeze authority None, permanent delegate None) and real DexScreener buy pressure.

AUDIT WORKFLOW:
Step 1: Call `helius_get_asset` with the token mint to inspect on-chain metadata, token program, mint authority, freeze authority, and permanent delegate.
Step 2: If the token is a promotional ad spam token or contains dangerous Token-2022 delegates, call `quarantine_dust_token` and return verdict 'QUARANTINE_DUST'.
Step 3: Call `dexscreener_get_pairs` to evaluate current pool liquidity, 5m trading volume, and buy/sell transaction count.
Step 4: Call `check_wallet_reserve_floor` to ensure current hot wallet capital permits entry.
Step 5: Synthesize your findings into a comprehensive `TokenRiskReport` matching the required schema.
"""


def fast_onchain_audit(mint: str) -> TokenRiskReport:
    """Performs deterministic on-chain DAS and liquidity security audit directly.

    Evaluates Token-2022 permanent delegates, freeze authorities, spam text, and AMM liquidity.
    Guarantees 100% adherence to Rule 5 even when remote LLM endpoints are unreachable.
    """
    import json
    das_raw = helius_get_asset(mint)
    das = json.loads(das_raw) if isinstance(das_raw, str) else das_raw

    dex_raw = dexscreener_get_pairs(mint)
    dex = json.loads(dex_raw) if isinstance(dex_raw, str) else dex_raw

    name = das.get("name") or "Unknown"
    symbol = das.get("symbol") or "TOKEN"
    is_t22 = bool(das.get("is_token_2022", False))
    perm_del = das.get("permanent_delegate")
    freeze_auth = das.get("freeze_authority")
    mint_auth = das.get("mint_authority")
    is_spam = bool(das.get("is_spam_advertisement", False))
    has_weaponized = bool(das.get("has_weaponized_authority", False))

    authorities = TokenAuthorities(
        mint_authority=mint_auth,
        freeze_authority=freeze_auth,
        permanent_delegate=perm_del,
        is_token_2022=is_t22,
        is_dangerous=bool(has_weaponized or perm_del or freeze_auth),
        has_weaponized_authority=bool(has_weaponized or perm_del),
    )

    liq_usd = float(dex.get("liquidity_usd") or 0.0)
    vol_5m = float(dex.get("volume_5m_usd") or 0.0)
    vol_1h = float(dex.get("volume_1h_usd") or 0.0)
    px_sol = float(dex.get("price_native_sol") or 0.0)
    dex_id = str(dex.get("dex_id") or "unknown")

    liquidity = LiquidityMetrics(
        dex_id=dex_id,
        price_native_sol=px_sol,
        liquidity_usd=liq_usd,
        volume_5m_usd=vol_5m,
        volume_1h_usd=vol_1h,
    )

    veto_reasons = []
    if is_spam:
        veto_reasons.append("promotional_dust_spam")
    if perm_del:
        veto_reasons.append("permanent_delegate_live")
    if freeze_auth:
        veto_reasons.append("freeze_authority_live")
    if is_t22 and (perm_del or freeze_auth):
        veto_reasons.append("weaponized_token_2022")

    if veto_reasons or is_spam:
        quarantine_dust_token(mint, ", ".join(veto_reasons))
        return TokenRiskReport(
            mint=mint,
            symbol=symbol,
            name=name,
            verdict="QUARANTINE_DUST",
            risk_score=1.0,
            conviction_score=0.0,
            veto_reasons=veto_reasons,
            authorities=authorities,
            liquidity=liquidity,
            narrative_analysis=f"Malicious or unsolicited spam token flagged with on-chain risk markers: {', '.join(veto_reasons)}.",
            execution_recommendation="Quarantine token immediately; zero trading interaction allowed under Rule 5.",
        )

    if liq_usd < 200.0 and dex_id != "pumpfun":
        veto_reasons.append("insufficient_liquidity")
        return TokenRiskReport(
            mint=mint,
            symbol=symbol,
            name=name,
            verdict="VETO",
            risk_score=0.85,
            conviction_score=0.10,
            veto_reasons=veto_reasons,
            authorities=authorities,
            liquidity=liquidity,
            narrative_analysis=f"Insufficient liquidity (${liq_usd:,.2f}) on pool {dex_id}. High slippage and rug risk.",
            execution_recommendation="Veto entry: liquidity below minimum safe threshold.",
        )

    # Clean authorities and active pool
    conviction = 0.88 if (vol_5m > 1000.0 or dex_id == "pumpfun") else 0.75
    return TokenRiskReport(
        mint=mint,
        symbol=symbol,
        name=name,
        verdict="APPROVE",
        risk_score=0.20 if not mint_auth else 0.40,
        conviction_score=conviction,
        veto_reasons=[],
        authorities=authorities,
        liquidity=liquidity,
        narrative_analysis=f"Verified token with clean authorities (mint: {mint_auth or 'revoked'}, freeze: None). Active pool on {dex_id}.",
        execution_recommendation=f"Approved for live trade with dynamic allocation (0.015 - 0.025 SOL) via Jito bundle.",
    )


class BotsensaiSentinelAgent:
    """Autonomous Sentinel Agent leveraging Google Antigravity SDK with on-chain fallback."""

    def __init__(self, model: Optional[str] = None):
        self.config = None
        try:
            from google.antigravity import LocalAgentConfig
            from google.antigravity.hooks import policy

            config_kwargs = {
                "system_instructions": SENTINEL_SYSTEM_INSTRUCTIONS,
                "tools": [
                    helius_get_asset,
                    dexscreener_get_pairs,
                    check_wallet_reserve_floor,
                    quarantine_dust_token,
                ],
                "response_schema": TokenRiskReport,
                "policies": [
                    policy.confirm_run_command(),
                ],
            }
            if model:
                config_kwargs["model"] = model
            self.config = LocalAgentConfig(**config_kwargs)
        except Exception:
            self.config = None

    async def audit_token(self, mint: str) -> TokenRiskReport:
        """Audits a candidate Solana token mint asynchronously and returns a structured TokenRiskReport."""
        if self.config:
            try:
                from google.antigravity import Agent
                prompt = (
                    f"Please conduct an autonomous security and alpha audit for token mint: {mint}.\n"
                    f"Check its on-chain metadata and authorities, examine liquidity and volume, "
                    f"verify wallet reserve floor headroom, and return the structured TokenRiskReport."
                )
                async with Agent(self.config) as agent:
                    response = await agent.chat(prompt)
                    data = await response.structured_output()
                    if isinstance(data, dict):
                        return TokenRiskReport.model_validate(data)
                    elif isinstance(data, TokenRiskReport):
                        return data
            except Exception as e:
                import logging
                logging.getLogger("sentinel").warning(f"AGY Agent chat fallback to deterministic audit: {e}")

        # Deterministic on-chain audit fallback
        return fast_onchain_audit(mint)

    def audit_token_sync(self, mint: str) -> TokenRiskReport:
        """Synchronous wrapper for audit_token."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: asyncio.run(self.audit_token(mint)))
                return future.result(timeout=60)
        else:
            return asyncio.run(self.audit_token(mint))
