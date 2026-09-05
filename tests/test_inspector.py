"""Tests for universal token inspector and URL resolver."""

from __future__ import annotations

from botsensai.inspector import extract_mint_from_query


def test_extract_mint_from_direct_address():
    mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    resolved, note = extract_mint_from_query(mint)
    assert resolved == mint
    assert "direct Solana mint address" in (note or "")


def test_extract_mint_from_pumpfun_url():
    url = "https://pump.fun/coin/7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    resolved, note = extract_mint_from_query(url)
    assert resolved == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    assert "extracted from URL" in (note or "")


def test_extract_mint_from_dexscreener_url():
    url = "https://dexscreener.com/solana/7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    resolved, note = extract_mint_from_query(url)
    assert resolved == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    assert "extracted from URL" in (note or "")


def test_extract_mint_invalid_query():
    resolved, note = extract_mint_from_query("totally-invalid-format!!")
    assert resolved is None
    assert "unable to resolve" in (note or "")
