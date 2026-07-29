"""Scoring, veto, sizing, fill and risk tests.

These cover the parts of the system where a subtle bug costs money rather than
producing an obviously wrong number: missing data silently reading as bearish,
a veto being overridden by a high score, a fill model that is too generous, and
risk limits that advise rather than enforce.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from botsensai.config import RiskSettings, Settings, TradingMode
from botsensai.execution.broker import AccountState, PaperBroker, RiskManager, build_broker
from botsensai.execution.fills import (
    CurveState,
    FillContext,
    FillSimulator,
    estimate_impact_bps,
    max_size_within_impact,
)
from botsensai.metrics import build_registry
from botsensai.models import (
    Confidence,
    CurveStage,
    MetricValue,
    Order,
    Side,
    TokenRef,
    VetoReason,
    utcnow,
)
from botsensai.scoring import CompositeScorer, kelly_fraction, score_to_size
from botsensai.scoring.composite import VetoEngine
from botsensai.util.synthetic import generate_token
from tests.test_metrics import context_for

# --------------------------------------------------------------------------- #
# composite scoring
# --------------------------------------------------------------------------- #


def test_scorer_separates_archetypes():
    """The single most important end-to-end assertion in the suite."""
    scorer = CompositeScorer(build_registry())
    scores = {
        archetype: scorer.score(context_for(archetype, seed=13)).composite
        for archetype in ("organic", "manufactured", "rug")
    }
    assert scores["organic"] > scores["manufactured"] > scores["rug"], scores
    assert scores["organic"] - scores["rug"] > 0.2, "separation is too weak to be useful"


def test_missing_metrics_do_not_read_as_bearish():
    """Dropping a metric must not push the composite down.

    If unavailable data lowered the score, every collector outage would look
    like a market full of bad tokens, and the system would stop trading exactly
    when it was least able to tell why.
    """
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=21)
    full = scorer.score(ctx)

    degraded_values = [
        v
        for v in full.metric_values
        if v.metric_id not in ("reply_template_ratio", "engager_age_dispersion")
    ]
    degraded = scorer.score(ctx, degraded_values)

    assert degraded.coverage < full.coverage
    assert degraded.composite == pytest.approx(full.composite, abs=0.15), (
        "removing metrics should shift the composite only modestly, never collapse it"
    )


def test_coverage_gate_blocks_thin_reads():
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=4)
    values = scorer.registry.evaluate_all(ctx)
    thin = [
        MetricValue(
            metric_id=v.metric_id,
            token=v.token,
            as_of=v.as_of,
            raw=None,
            normalized=None,
            confidence=Confidence.MISSING,
        )
        for v in values[:-2]
    ] + list(values[-2:])
    result = scorer.score(ctx, thin)
    ok, reason = scorer.should_enter(result)
    assert not ok
    assert "coverage" in reason


def test_regime_classification():
    assert CompositeScorer.classify_regime({"graduation_rate_24h": 0.02}) == "hot"
    assert CompositeScorer.classify_regime({"graduation_rate_24h": 0.008}) == "normal"
    assert CompositeScorer.classify_regime({"graduation_rate_24h": 0.001}) == "dead"
    assert CompositeScorer.classify_regime({}) == "unknown"


def test_regime_reweighting_sums_to_one():
    scorer = CompositeScorer(build_registry())
    for regime in ("hot", "normal", "dead"):
        weights = scorer.effective_family_weights(regime)
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# vetoes
# --------------------------------------------------------------------------- #


def test_veto_cannot_be_outvoted_by_a_high_score():
    """A veto is a refusal, not a penalty. This is the whole point of the class."""
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=8)
    ctx.security.dev_sold = True
    result = scorer.score(ctx)
    assert VetoReason.DEV_ALREADY_SOLD in result.vetoes
    ok, _ = scorer.should_enter(result)
    assert not ok
    assert score_to_size(result, 1.0) == 0.0


def test_live_mint_authority_is_vetoed():
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=8)
    ctx.security.mint_authority_revoked = False
    result = scorer.score(ctx)
    assert VetoReason.MINT_AUTHORITY_LIVE in result.vetoes


def test_unknown_security_is_not_treated_as_a_failure():
    """`None` must mean unknown, not False. Conflating them vetoes everything."""
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=8)
    ctx.security.mint_authority_revoked = None
    ctx.security.freeze_authority_revoked = None
    result = scorer.score(ctx)
    assert VetoReason.MINT_AUTHORITY_LIVE not in result.vetoes
    assert VetoReason.FREEZE_AUTHORITY_LIVE not in result.vetoes


def test_concentration_veto_is_stage_aware():
    """Early bonding-curve tokens are structurally concentrated; that is not a rug."""
    engine = VetoEngine()
    ctx = context_for("organic", seed=8)
    ctx.security.top10_share = 0.70
    assert ctx.latest is not None and ctx.latest.stage in (
        CurveStage.BONDING,
        CurveStage.NEAR_GRADUATION,
    )
    vetoes = engine.evaluate(ctx, [])
    assert VetoReason.HOLDER_CONCENTRATION_EXTREME not in vetoes

    ctx.snapshots[-1].stage = CurveStage.GRADUATED
    vetoes = engine.evaluate(ctx, [])
    assert VetoReason.HOLDER_CONCENTRATION_EXTREME in vetoes


def test_kill_switch_short_circuits_everything():
    engine = VetoEngine()
    ctx = context_for("organic", seed=8)
    vetoes = engine.evaluate(ctx, [], kill_switch=True)
    assert vetoes == [VetoReason.KILL_SWITCH]


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #


def test_kelly_is_capped_and_never_negative():
    assert kelly_fraction(0.9, 10.0) <= 0.05
    assert kelly_fraction(0.01, 2.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0


def test_sizing_is_convex_in_score():
    """A marginal signal must take a token position, not a full one."""
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=13)
    strong = scorer.score(ctx)

    weak = strong.model_copy(update={"composite": 0.60, "vetoes": []})
    strong_size = score_to_size(strong, 0.25)
    weak_size = score_to_size(weak, 0.25)
    assert strong_size > weak_size >= 0.0
    assert strong_size <= 0.25


def test_sizing_scales_with_coverage():
    scorer = CompositeScorer(build_registry())
    ctx = context_for("organic", seed=13)
    result = scorer.score(ctx)
    confident = score_to_size(result, 0.25)
    unsure = score_to_size(result.model_copy(update={"coverage": 0.5}), 0.25)
    assert confident > unsure


# --------------------------------------------------------------------------- #
# fills
# --------------------------------------------------------------------------- #


def test_curve_price_impact_is_monotonic_and_bounded():
    curve = CurveState()
    small = estimate_impact_bps(curve, 0.1)
    large = estimate_impact_bps(curve, 5.0)
    assert 0 < small < large
    # Constant product: 5 SOL into a 30 SOL reserve is ~1667bps.
    assert 1000 < large < 3000


def test_max_size_within_impact_is_consistent():
    curve = CurveState()
    size = max_size_within_impact(curve, 500.0)
    impact = estimate_impact_bps(curve, size)
    assert impact == pytest.approx(500.0, rel=0.01)


def test_curve_reconstruction_matches_observed_price():
    """A curve whose spot price disagrees with the market charges phantom slippage."""
    token = generate_token("organic", seed=3)
    for snapshot in token.snapshots[1:]:
        curve = CurveState.from_snapshot(snapshot)
        assert curve.spot_price == pytest.approx(snapshot.price_native, rel=1e-6)


def test_fills_charge_fees_even_when_rejected():
    """A failed transaction still burns its fee. Ignoring that flatters the strategy."""
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[3]
    simulator = FillSimulator()
    simulator.settings.fail_probability = 1.0

    order = Order(
        token=token.token,
        as_of=snapshot.as_of,
        side=Side.BUY,
        size_native=0.2,
        priority_fee_lamports=1_000_000,
        jito_tip_lamports=1_000_000,
    )
    fill = simulator.simulate(order, FillContext(snapshot=snapshot, curve=CurveState.from_snapshot(snapshot)))
    assert fill.rejected
    assert fill.fee_native > 0


def test_slippage_limit_is_enforced():
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[3]
    simulator = FillSimulator()
    simulator.settings.fail_probability = 0.0
    simulator.settings.sandwich_probability = 0.0

    order = Order(
        token=token.token,
        as_of=snapshot.as_of,
        side=Side.BUY,
        size_native=500.0,  # absurd relative to the curve
        max_slippage_bps=100,
    )
    fill = simulator.simulate(order, FillContext(snapshot=snapshot, curve=CurveState.from_snapshot(snapshot)))
    assert fill.rejected
    assert "slippage" in (fill.reject_reason or "")


def test_fills_are_reproducible_under_a_fixed_seed():
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[3]
    order = Order(token=token.token, as_of=snapshot.as_of, side=Side.BUY, size_native=0.15)
    ctx = FillContext(snapshot=snapshot, curve=CurveState.from_snapshot(snapshot))

    a = FillSimulator(seed=99).simulate(order, ctx)
    b = FillSimulator(seed=99).simulate(order, ctx)
    assert a.amount_token == b.amount_token
    assert a.latency_ms == b.latency_ms


# --------------------------------------------------------------------------- #
# risk
# --------------------------------------------------------------------------- #


def test_risk_manager_enforces_position_cap():
    risk = RiskManager(RiskSettings(max_position_native=0.1))
    account = AccountState(cash_native=10.0)
    token = TokenRef(mint="A" * 32)
    order = Order(token=token, as_of=utcnow(), side=Side.BUY, size_native=5.0)
    decision = risk.check_entry(order, account, None, age_seconds=600)
    assert decision.allowed
    assert decision.adjusted_size == pytest.approx(0.1)


def test_risk_manager_blocks_duplicate_positions():
    broker = PaperBroker(starting_native=10.0)
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[3]
    first = broker.open_position(token.token, 0.1, snapshot, snapshot.as_of, age_seconds=600)
    assert first is not None and not first.rejected
    second = broker.open_position(token.token, 0.1, snapshot, snapshot.as_of, age_seconds=600)
    assert second is None


def test_risk_manager_blocks_tokens_that_are_too_young():
    risk = RiskManager(RiskSettings(min_token_age_seconds=60))
    account = AccountState(cash_native=10.0)
    order = Order(token=TokenRef(mint="A" * 32), as_of=utcnow(), side=Side.BUY, size_native=0.1)
    decision = risk.check_entry(order, account, None, age_seconds=10)
    assert not decision.allowed
    assert "young" in decision.reason


def test_daily_loss_limit_stops_trading():
    risk = RiskManager(RiskSettings(max_daily_loss_native=1.0))
    account = AccountState(cash_native=10.0, daily_loss_native=1.5)
    order = Order(token=TokenRef(mint="A" * 32), as_of=utcnow(), side=Side.BUY, size_native=0.1)
    decision = risk.check_entry(order, account, None, age_seconds=600)
    assert not decision.allowed
    assert "daily loss" in decision.reason


def test_exit_ladder_scales_out_rather_than_dumping():
    broker = PaperBroker(starting_native=10.0)
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[2]
    fill = broker.open_position(token.token, 0.2, snapshot, snapshot.as_of, age_seconds=600)
    assert fill is not None and not fill.rejected

    entry_price = fill.price_native
    broker.mark(token.token, entry_price * 2.5)
    signals = broker.exit_signals(token.token, snapshot.as_of + timedelta(minutes=5))
    assert signals, "a 2.5x move should trigger the first take-profit rung"
    fraction, reason = signals[0]
    assert 0 < fraction < 1.0, "the first rung must scale out, not close the whole position"
    assert "take profit" in reason


def test_stop_loss_closes_the_whole_position():
    broker = PaperBroker(starting_native=10.0)
    token = generate_token("organic", seed=3)
    snapshot = token.snapshots[2]
    fill = broker.open_position(token.token, 0.2, snapshot, snapshot.as_of, age_seconds=600)
    assert fill is not None and not fill.rejected

    broker.mark(token.token, fill.price_native * 0.4)
    signals = broker.exit_signals(token.token, snapshot.as_of + timedelta(minutes=5))
    assert signals and signals[0][0] == 1.0
    assert "stop loss" in signals[0][1]


# --------------------------------------------------------------------------- #
# the live-trading boundary
# --------------------------------------------------------------------------- #


def test_live_mode_cannot_be_constructed_without_double_opt_in():
    with pytest.raises(ValueError, match="I_UNDERSTAND_THE_RISK"):
        Settings(trading_mode=TradingMode.LIVE, i_understand_the_risk=False)


def test_build_broker_refuses_live_mode():
    settings = Settings(
        trading_mode=TradingMode.LIVE, i_understand_the_risk=True, dry_run=False
    )
    with pytest.raises(NotImplementedError, match="not implemented"):
        build_broker(settings)


def test_repository_contains_no_signing_code():
    """A structural assertion: this build must not be able to sign anything."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    banned = ("Keypair.from_", "sign_transaction", "send_raw_transaction", "from_bytes(secret")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"signing-capable code found: {offenders}"
