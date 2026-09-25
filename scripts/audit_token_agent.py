#!/usr/bin/env python3
"""CLI utility to audit any Solana token using the Autonomous AI Risk & Alpha Sentinel Agent."""

import argparse
import json
import sys
from pathlib import Path

# Ensure src is in python path
src_dir = Path(__file__).resolve().parents[1] / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from botsensai.agents.sentinel import BotsensaiSentinelAgent


def main():
    parser = argparse.ArgumentParser(description="Botsensai AI Risk & Alpha Sentinel Audit")
    parser.add_argument(
        "--mint",
        type=str,
        default="GNhCphYjduivkJvzSqWiwTyjvJsZmzUtrSKVjrhFpump",
        help="Solana mint address to audit (default: GNhCphYjduiv... spam token)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted report"
    )
    args = parser.parse_args()

    print(f"\n==================================================================")
    print(f"   🤖 BOTSENSAI AI SENTINEL AGENT | GOOGLE ANTIGRAVITY SDK        ")
    print(f"==================================================================")
    print(f"Target Mint: {args.mint}\nAuditing on-chain authorities & market flow...")

    sentinel = BotsensaiSentinelAgent()
    try:
        report = sentinel.audit_token_sync(args.mint)
    except Exception as e:
        print(f"\n❌ Error during Sentinel audit: {e}")
        sys.exit(1)

    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
        return

    verdict_emoji = "🟢" if report.verdict == "APPROVE" else ("🛑" if report.verdict == "VETO" else "☣️")

    print("\n" + "=" * 66)
    print(f" {verdict_emoji} SENTINEL VERDICT: {report.verdict}")
    print("=" * 66)
    print(f" Token Name   : {report.name}")
    print(f" Token Symbol : ${report.symbol}")
    print(f" Mint Address : {report.mint}")
    print(f" Risk Score   : {report.risk_score:.2f} / 1.00")
    print(f" Conviction   : {report.conviction_score:.2f} / 1.00")
    
    if report.veto_reasons:
        print(f" Veto Reasons : {', '.join(report.veto_reasons)}")

    print("\n[On-Chain Authorities]:")
    auths = report.authorities
    print(f" • Token-2022        : {'YES' if auths.is_token_2022 else 'NO (Legacy SPL)'}")
    print(f" • Mint Authority    : {auths.mint_authority or 'REVOKED (Safe)'}")
    print(f" • Freeze Authority  : {auths.freeze_authority or 'NONE (Safe)'}")
    print(f" • Permanent Delegate: {auths.permanent_delegate or 'NONE (Safe)'}")
    print(f" • Dangerous Auths   : {'⚠️ WEAPONIZED' if auths.is_dangerous else '🟢 CLEAN'}")

    print("\n[Market & Liquidity]:")
    liq = report.liquidity
    print(f" • DEX / Pool        : {liq.dex_id}")
    print(f" • Price (SOL)       : {liq.price_native_sol:.9f} SOL")
    print(f" • Liquidity (USD)   : ${liq.liquidity_usd:,.2f}")
    print(f" • 5m Volume (USD)   : ${liq.volume_5m_usd:,.2f}")

    print("\n[Narrative & Security Analysis]:")
    print(f" {report.narrative_analysis}")

    print("\n[Execution Guidance]:")
    print(f" {report.execution_recommendation}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    main()
