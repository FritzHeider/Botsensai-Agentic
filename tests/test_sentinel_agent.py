"""Unit and integration tests for Botsensai Autonomous Sentinel Agent."""

import json
import pytest
from botsensai.agents.schemas import LiquidityMetrics, TokenAuthorities, TokenRiskReport
from botsensai.agents.sentinel import BotsensaiSentinelAgent
from botsensai.agents.tools import (
    check_wallet_reserve_floor,
    dexscreener_get_pairs,
    helius_get_asset,
    quarantine_dust_token,
)

SPAM_MINT = "GNhCphYjduivkJvzSqWiwTyjvJsZmzUtrSKVjrhFpump"


def test_pydantic_schema_validation():
    """Verifies that TokenRiskReport Pydantic schema properly validates."""
    report = TokenRiskReport(
        mint=SPAM_MINT,
        symbol="SPAM",
        name="Switch to PumpDev.io",
        verdict="QUARANTINE_DUST",
        risk_score=1.0,
        conviction_score=0.0,
        veto_reasons=["permanent_delegate_live", "promotional_dust_spam"],
        authorities=TokenAuthorities(
            mint_authority="sWicth...",
            freeze_authority="sWicth...",
            permanent_delegate="sWicth...",
            is_token_2022=True,
            is_dangerous=True,
        ),
        liquidity=LiquidityMetrics(
            dex_id="pumpfun",
            price_native_sol=0.0,
            liquidity_usd=0.0,
            volume_5m_usd=0.0,
            volume_1h_usd=0.0,
        ),
        narrative_analysis="Promotional spam dust airdrop with weaponized Token-2022 permanent delegate.",
        execution_recommendation="Quarantine token immediately; zero trading interaction.",
    )

    dumped = report.model_dump()
    assert dumped["verdict"] == "QUARANTINE_DUST"
    assert dumped["risk_score"] == 1.0
    assert dumped["authorities"]["is_token_2022"] is True
    assert dumped["authorities"]["permanent_delegate"] == "sWicth..."


def test_tools_helius_das_live():
    """Tests Helius DAS getAsset query against the known spam token."""
    raw = helius_get_asset(SPAM_MINT)
    data = json.loads(raw)

    assert "error" not in data, f"Helius DAS returned error: {data}"
    assert data["mint"] == SPAM_MINT
    assert data["is_token_2022"] is True
    assert data["permanent_delegate"] is not None
    assert "sWicth" in data["permanent_delegate"]
    assert data["has_weaponized_authority"] is True
    assert data["is_spam_advertisement"] is True


def test_tools_wallet_reserve_floor():
    """Tests that wallet reserve check properly respects Rule 2 (0.0100 SOL operational gas floor)."""
    raw = check_wallet_reserve_floor()
    data = json.loads(raw)

    assert "error" not in data
    assert data["hard_floor_sol"] == 0.010000
    assert data["min_safety_buffer_sol"] == 0.003000
    assert "balance_sol" in data
    assert "available_buffer_sol" in data


def test_tools_quarantine_dust():
    """Tests that quarantine_dust_token writes to quarantine records."""
    raw = quarantine_dust_token(SPAM_MINT, "unit_test_quarantine")
    data = json.loads(raw)

    assert data["status"] == "QUARANTINED"
    assert data["mint"] == SPAM_MINT


def test_sentinel_agent_initialization():
    """Verifies that BotsensaiSentinelAgent correctly configures the AGY SDK."""
    agent = BotsensaiSentinelAgent()
    assert agent.config is not None
    assert len(agent.config.tools) == 4
    assert "TokenRiskReport" in str(agent.config.response_schema)


@pytest.mark.asyncio
async def test_sentinel_agent_audit_spam_token():
    """End-to-end audit test of the spam token via the Google Antigravity SDK."""
    agent = BotsensaiSentinelAgent()
    report = await agent.audit_token(SPAM_MINT)

    assert isinstance(report, TokenRiskReport)
    assert report.mint == SPAM_MINT
    assert report.verdict in ("QUARANTINE_DUST", "VETO")
    assert report.risk_score >= 0.85
    assert report.authorities.is_token_2022 is True
    assert report.authorities.is_dangerous or report.authorities.has_weaponized_authority or report.authorities.permanent_delegate is not None
