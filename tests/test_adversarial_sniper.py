"""Unit and regression tests for the 20 sniper profitability improvements."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

import pytest

from botsensai.collectors.geyser import (
    BONDING_CURVE_DISCRIMINATOR,
    BondingCurveState,
    parse_bonding_curve_state,
)
from botsensai.config import RiskSettings, Settings
from botsensai.execution.broker import AccountState, PaperBroker, RiskManager
from botsensai.execution.bundle import BundleBuilder
from botsensai.execution.jito_tips import JitoTipEngine
from botsensai.execution.leader_router import LeaderRouter, RouteTarget
from botsensai.execution.multirail import MultiRailDispatcher
from botsensai.models import (
    Chain,
    Confidence,
    CurveStage,
    MarketSnapshot,
    Order,
    Position,
    SecurityReport,
    Side,
    TokenRef,
    Trade,
    VetoReason,
)
from botsensai.onchain.blockhash_cache import BlockhashCache
from botsensai.scoring.adversarial import AdversarialDetector
from botsensai.scoring.copycat import CopycatDetector
from botsensai.scoring.curve_stage import evaluate_curve_stage
from botsensai.scoring.wash_filter import WashTradeFilter, calculate_gini
from botsensai.smart_money import RealtimeSmartMoneyTrigger, SmartWalletProfile


def test_yellowstone_binary_struct_unpack() -> None:
    # 8 bytes discriminator + 5x uint64 + 1 byte bool
    raw_payload = BONDING_CURVE_DISCRIMINATOR + struct.pack(
        "<QQQQQ?",
        1_000_000_000 * 1_000_000,  # virtual_token_reserves
        30 * 1_000_000_000,          # virtual_sol_reserves
        800_000_000 * 1_000_000,    # real_token_reserves
        10 * 1_000_000_000,         # real_sol_reserves (10 SOL)
        1_000_000_000 * 1_000_000,  # total_supply
        False,                       # complete
    )
    state = parse_bonding_curve_state(raw_payload)
    assert state is not None
    assert state.virtual_sol_reserves == 30 * 1_000_000_000
    assert state.price_sol > 0
    assert state.curve_progress == pytest.approx(10.0 / 85.0, abs=1e-4)
    assert state.stage == CurveStage.BONDING


def test_blockhash_cache() -> None:
    cache = BlockhashCache.get_instance()
    bh = cache.get_latest_blockhash()
    assert len(bh) > 20


def test_dynamic_jito_tips() -> None:
    engine = JitoTipEngine.get_instance()
    tip_low = engine.calculate_tip_lamports(conviction_score=0.68, trade_size_sol=0.1)
    tip_high = engine.calculate_tip_lamports(conviction_score=0.90, trade_size_sol=0.1)
    assert tip_high >= tip_low
    assert tip_high <= 5_000_000 * 10  # within bounded risk limit


def test_leader_router() -> None:
    router = LeaderRouter()
    decision = router.decide_route()
    assert decision.target == RouteTarget.JITO_BUNDLE
    assert decision.is_jito_leader is True


def test_atomic_bundle_builder() -> None:
    builder = BundleBuilder()
    spec = builder.build_atomic_bundle(
        token_mint="TestMint11111111111111111111111111111111111",
        amount_sol=0.25,
        conviction_score=0.85,
    )
    assert spec.tip_lamports >= 50_000
    assert spec.tip_account in builder.select_random_tip_account() or len(spec.tip_account) > 30
    assert len(spec.endpoints) >= 2


@pytest.mark.asyncio
async def test_multirail_dispatcher() -> None:
    dispatcher = MultiRailDispatcher()
    receipt = await dispatcher.dispatch(
        token_mint="TestMint11111111111111111111111111111111111",
        amount_sol=0.10,
    )
    assert receipt.confirmed is True
    assert receipt.primary_rail == "jito_block_engine"


def test_adversarial_dev_bundler_sybil() -> None:
    detector = AdversarialDetector()
    sec_clean = SecurityReport(
        token=TokenRef(mint="Clean11111111111111111111111111111111111"),
        as_of=datetime.now(timezone.utc),
        bundled_share=0.05,
    )
    assert detector.inspect_slot0_bundle(sec_clean).veto is None

    sec_sybil = SecurityReport(
        token=TokenRef(mint="Sybil11111111111111111111111111111111111"),
        as_of=datetime.now(timezone.utc),
        bundled_share=0.45,
    )
    assert detector.inspect_slot0_bundle(sec_sybil).veto == VetoReason.DEV_BUNDLER_SYBIL


def test_adversarial_2hop_cex_cabal() -> None:
    detector = AdversarialDetector()
    veto = detector.check_2hop_funding_cabal(
        deployer_funder="FunderRoot11111111111111111111111111111111",
        early_buyer_funders=[
            "RandomWallet111111111111111111111111111111",
            "FunderRoot11111111111111111111111111111111",
        ],
    )
    assert veto == VetoReason.CEX_INSIDER_CABAL


def test_copycat_honeypot_detector() -> None:
    detector = CopycatDetector()
    fake_wif = TokenRef(
        symbol="WIF",
        mint="FakeWifMint111111111111111111111111111111111",
    )
    assert detector.check_copycat(fake_wif) == VetoReason.COPYCAT_HONEYPOT

    real_token = TokenRef(
        symbol="NEWGEM",
        mint="NewGemMint111111111111111111111111111111111",
    )
    assert detector.check_copycat(real_token) is None


def test_wash_trade_filter() -> None:
    wash = WashTradeFilter()
    now = datetime.now(timezone.utc)
    token = TokenRef(mint="WashToken1111111111111111111111111111111")
    # Circular trades between 2 wallets
    trades = [
        Trade(token=token, signature=f"sig{i}", amount_token=100.0, wallet="WalletA" if i % 2 == 0 else "WalletB", as_of=now, side=Side.BUY if i % 2 == 0 else Side.SELL, amount_native=1.0)
        for i in range(6)
    ]
    assert wash.evaluate_trades(trades) == VetoReason.WASH_TRADING_DETECTED


def test_golden_curve_stage_evaluation() -> None:
    token = TokenRef(mint="CurveToken111111111111111111111111111pump")
    now = datetime.now(timezone.utc)
    snap_early = MarketSnapshot(
        token=token, as_of=now, observed_at=now, market_cap_usd=15_000.0, liquidity_usd=8_000.0
    )
    progress, veto = evaluate_curve_stage(snap_early)
    assert veto is None
    assert progress < 0.50

    snap_late = MarketSnapshot(
        token=token, as_of=now, observed_at=now, market_cap_usd=95_000.0, liquidity_usd=45_000.0
    )
    progress_late, veto_late = evaluate_curve_stage(snap_late)
    assert veto_late == VetoReason.CURVE_STAGE_OUT_OF_BOUNDS


def test_realtime_smart_money_trigger() -> None:
    trigger = RealtimeSmartMoneyTrigger()
    trigger.update_alpha_wallets(
        [
            SmartWalletProfile(
                wallet="SmartAlphaWallet1111111111111111111111111",
                total_trades=25,
                tokens_traded=15,
                profitable_trades=20,
                win_rate=0.80,
                avg_entry_offset_seconds=12.0,
                total_sol_invested=15.0,
            )
        ]
    )
    matched, boost, wallets = trigger.evaluate_early_buyers(
        ["SmartAlphaWallet1111111111111111111111111", "RandomBuyer111111111111111111111111111"]
    )
    assert matched is True
    assert boost >= 0.10
    assert "SmartAlphaWallet1111111111111111111111111" in wallets


def test_fractional_kelly_sizing() -> None:
    settings = RiskSettings(max_position_native=0.25, use_kelly_sizing=True, kelly_fraction=0.25)
    rm = RiskManager(settings)
    order = Order(
        token=TokenRef(mint="KellyToken11111111111111111111111111111"),
        as_of=datetime.now(timezone.utc),
        side=Side.BUY,
        size_native=0.25,
    )
    size_high = rm.calculate_kelly_size(order, score=0.92)
    size_low = rm.calculate_kelly_size(order, score=0.68)
    assert size_high > size_low
    assert size_high <= 0.25


def test_momentum_stop_and_curve_exits() -> None:
    broker = PaperBroker()
    token = TokenRef(mint="ExitToken1111111111111111111111111111111")
    opened_at = datetime.now(timezone.utc) - timedelta(seconds=120)

    pos = Position(
        token=token,
        opened_at=opened_at,
        amount_token=1000.0,
        cost_basis_native=0.10,  # entry price = 0.0001
        last_price_native=0.000105,  # only +5% gain after 120s
        peak_price_native=0.000105,
    )
    broker.account.positions[token.key] = pos

    # Evaluate 90s momentum stop
    signals = broker.exit_signals(token, datetime.now(timezone.utc))
    assert len(signals) == 1
    fraction, reason = signals[0]
    assert fraction == 1.0
    assert "momentum time-decay stop" in reason

    # Evaluate 98% pre-migration curve exit
    snap_migration = MarketSnapshot(
        token=token,
        as_of=datetime.now(timezone.utc),
        observed_at=datetime.now(timezone.utc),
        market_cap_usd=70_000.0,
    )
    curve_signals = broker.exit_signals(token, datetime.now(timezone.utc), snapshot=snap_migration)
    assert len(curve_signals) == 1
    assert "bonding curve at 98%" in curve_signals[0][1]
