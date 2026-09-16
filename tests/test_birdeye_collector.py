"""Tests for Birdeye token security and holder collector."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from botsensai.collectors.birdeye import BirdeyeCollector
from botsensai.config import Settings
from botsensai.models import Chain, TokenRef


@pytest.fixture()
def token() -> TokenRef:
    return TokenRef(chain=Chain.SOLANA, mint="So11111111111111111111111111111111111111112", symbol="SOL")


def test_birdeye_collector_init_headers():
    settings = Settings(birdeye_api_key="test_key_123")
    collector = BirdeyeCollector(settings=settings)
    client = collector.client
    assert client._headers.get("X-API-KEY") == "test_key_123"
    assert client._headers.get("x-chain") == "solana"


def test_birdeye_parse_security(token: TokenRef):
    settings = Settings(birdeye_api_key="test_key_123")
    collector = BirdeyeCollector(settings=settings)

    sample_data = {
        "creatorAddress": "CreAtor111111111111111111111111111111111111",
        "owner": None,
        "mintAuthority": None,  # None means revoked
        "freezeAuthority": None,  # None means revoked
        "top10HolderPercent": 24.5,
        "creatorPercentage": 3.2,
        "lpBurnedPercent": 100.0,
        "isMutableMetadata": False,
    }

    as_of = datetime.now(timezone.utc)
    report = collector._parse_security(token, sample_data, as_of)

    assert report is not None
    assert report.token.mint == token.mint
    assert report.mint_authority_revoked is True
    assert report.freeze_authority_revoked is True
    assert report.top10_share == pytest.approx(0.245)
    assert report.dev_holding_share == pytest.approx(0.032)
    assert report.lp_burned_share == pytest.approx(1.0)
    assert report.is_mutable_metadata is False
    assert report.source == "birdeye:token_security"


def test_birdeye_parse_security_authorities_active(token: TokenRef):
    settings = Settings(birdeye_api_key="test_key_123")
    collector = BirdeyeCollector(settings=settings)

    sample_data = {
        "creatorAddress": "CreAtor111111111111111111111111111111111111",
        "mintAuthority": "CreAtor111111111111111111111111111111111111",
        "freezeAuthority": "CreAtor111111111111111111111111111111111111",
        "top10HolderPercent": 85.0,
    }

    as_of = datetime.now(timezone.utc)
    report = collector._parse_security(token, sample_data, as_of)

    assert report is not None
    assert report.mint_authority_revoked is False
    assert report.freeze_authority_revoked is False
    assert report.top10_share == pytest.approx(0.85)


def test_birdeye_parse_holders(token: TokenRef):
    settings = Settings(birdeye_api_key="test_key_123")
    collector = BirdeyeCollector(settings=settings)

    sample_data = {
        "items": [
            {"owner": "WalletA11111111111111111111111111111111111", "percentage": 15.5, "uiAmount": 155000.0},
            {"owner": "WalletB11111111111111111111111111111111111", "percentage": 8.2, "uiAmount": 82000.0},
        ]
    }

    as_of = datetime.now(timezone.utc)
    holders = collector._parse_holders(token, sample_data, as_of)

    assert len(holders) == 2
    assert holders[0].wallet == "WalletA11111111111111111111111111111111111"
    assert holders[0].share_of_supply == pytest.approx(0.155)
    assert holders[0].balance == 155000.0


@pytest.mark.asyncio
async def test_birdeye_enrich_without_key_gracefully_degrades(token: TokenRef):
    settings = Settings(birdeye_api_key=None)
    collector = BirdeyeCollector(settings=settings)

    as_of = datetime.now(timezone.utc)
    res = await collector.enrich(token, as_of)

    assert res.ok is True
    assert len(res.security) == 0
    assert len(res.holders) == 0
