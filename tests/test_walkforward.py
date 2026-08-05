"""The rolling walk-forward harness (P3-01).

`BacktestSettings` has carried `train_days`, `test_days`, `step_days` and
`embargo_hours` since the skeleton was written and nothing consumed any of them.
A single backtest that fits weights on the same population it then scores is not
evidence of anything, so this file guards the thing that turns those four
numbers into an out-of-sample number.

The assertion the task asks for — no token key in both a fold's train and its
own test — is here (`test_no_token_appears_in_both_train_and_its_own_test`), but
on its own it is close to worthless and it is worth saying why. Fold membership
is decided by `launch.created_at`, and a fold's train and test `created_at`
ranges are disjoint *by construction*: that test cannot fail unless the window
arithmetic is broken outright. The leak this market actually has is the one
`test_the_embargo_purges_the_tail_of_the_train_window` covers: a token launched
just before `train_end` has no label until its outcome horizon has elapsed, and
that horizon lands inside the test window. Fitting on it means the fit was shown
information contemporaneous with the period it is being graded on. So the
embargo is a purge on the train side, and the test that matters is that it
removes tokens the naive split would have kept.

Two supporting shapes are pinned here for reasons that are not obvious:

* **A train tape must carry a label.** `TrainingExample.from_values` reads
  `max_realizable_multiple or max_multiple_from_t0 or 0.0`, so an unlabelled
  tape does not enter the fit as missing — it enters as a confident zero, the
  strongest bearish claim available. `test_an_unlabelled_tape_never_enters_train`
  keeps that filter in place.
* **No interval is not a narrow interval.** `bootstrap_expectancy_ci` returns
  `(0.0, 0.0)` as a sentinel below `BOOTSTRAP_MIN_TRADES` trades, and reading it
  as a real interval makes every under-sampled run print "not distinguishable
  from no edge" — which reads as a measured verdict on the strategy and is
  really a statement that nothing was measured. That was live in `summary()` and
  `test_absence_of_an_interval_is_not_an_interval_spanning_zero` is what closes
  it.

The cohort is pinned (universe 24, seed 1337, `created_at` 2026-07-01T12:00Z,
one launch an hour) so the fold table is identical run to run, and the windows
are set in hours rather than the default fourteen days because a synthetic token
lives for one hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from botsensai.backtest.engine import (
    BOOTSTRAP_MIN_TRADES,
    Backtester,
    BacktestResult,
    TokenTape,
)
from botsensai.backtest.walkforward import (
    Fold,
    FoldResult,
    WalkForward,
    WalkForwardReport,
    plan_folds,
    split_tapes,
)
from botsensai.config import BacktestSettings, Settings, load_settings
from botsensai.models import Outcome, utcnow
from botsensai.scoring.composite import Weights
from botsensai.scoring.fit import MIN_SAMPLES_TO_FIT, FitReport
from botsensai.util.synthetic import generate_cohort, synthetic_outcome

#: Pinned so the fold table below is the same table every run.
COHORT_START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
COHORT_SIZE = 24
COHORT_SPACING_SECONDS = 3600.0

#: Hours, not the default days: a synthetic token's price path lasts one hour,
#: so a fourteen-day train window would hold the entire universe and produce a
#: single fold with nothing to step to.
TRAIN_DAYS = 0.5
TEST_DAYS = 0.25
STEP_DAYS = 0.25
LABEL_HORIZON_HOURS = 1.0


def build_settings(**backtest: float) -> Settings:
    """Settings with the backtest window overridden.

    `load_settings()` rather than `get_settings()`: the latter is a process-wide
    singleton and mutating it leaks the narrow windows set here into every other
    test in the suite.
    """
    settings = load_settings()
    settings.backtest.train_days = TRAIN_DAYS
    settings.backtest.test_days = TEST_DAYS
    settings.backtest.step_days = STEP_DAYS
    settings.backtest.embargo_hours = 1.0
    for key, value in backtest.items():
        setattr(settings.backtest, key, value)
    return settings


def build_backtest(**overrides: float) -> BacktestSettings:
    """Just the backtest block, which is what `plan_folds` takes."""
    return build_settings(**overrides).backtest


def build_tapes() -> list[TokenTape]:
    """The pinned cohort as labelled tapes."""
    cohort = generate_cohort(
        n=COHORT_SIZE,
        seed=1337,
        created_at=COHORT_START,
        spacing_seconds=COHORT_SPACING_SECONDS,
    )
    outcomes = {
        token.launch.token.key: synthetic_outcome(token, LABEL_HORIZON_HOURS)
        for token in cohort
    }
    return Backtester.tapes_from_synthetic(cohort, outcomes=outcomes)


@pytest.fixture(scope="module")
def tapes() -> list[TokenTape]:
    return build_tapes()


@pytest.fixture(scope="module")
def harness() -> WalkForward:
    return WalkForward(build_settings(), label_horizon_hours=LABEL_HORIZON_HOURS)


@pytest.fixture(scope="module")
def report(harness: WalkForward, tapes: list[TokenTape]) -> WalkForwardReport:
    """One full run. Module-scoped: it replays every fold and takes ~30 seconds."""
    return harness.run(tapes, synthetic=True)


# --------------------------------------------------------------------------- #
# fold planning
# --------------------------------------------------------------------------- #


def test_folds_roll_forward_by_the_configured_step() -> None:
    settings = build_settings()
    folds = plan_folds(
        settings.backtest,
        COHORT_START,
        COHORT_START + timedelta(days=2),
        label_horizon_hours=LABEL_HORIZON_HOURS,
    )
    assert folds, "a two-day window must hold at least one 0.5+0.25 day fold"
    step = timedelta(days=STEP_DAYS)
    for i, fold in enumerate(folds):
        assert fold.index == i
        assert fold.train_end == fold.test_start, (
            "train and test are contiguous by design — the purge does the "
            "separating, not a dead calendar gap that discards data twice"
        )
        assert fold.train_end - fold.train_start == timedelta(days=TRAIN_DAYS)
        assert fold.test_end - fold.test_start == timedelta(days=TEST_DAYS)
        if i:
            assert fold.test_start - folds[i - 1].test_start == step


def test_no_fold_extends_past_the_end_of_the_window() -> None:
    """A partial test window would be reported as if it were a full one."""
    settings = build_settings()
    end = COHORT_START + timedelta(days=2)
    for fold in plan_folds(settings.backtest, COHORT_START, end, label_horizon_hours=1.0):
        assert fold.test_end <= end


def test_a_window_shorter_than_one_span_yields_no_folds() -> None:
    """An empty list is a real answer: a window this short cannot produce an
    out-of-sample number at all, and the caller has to say so rather than print
    a backtest with no folds in it."""
    settings = build_settings()
    folds = plan_folds(
        settings.backtest,
        COHORT_START,
        COHORT_START + timedelta(hours=1),
        label_horizon_hours=LABEL_HORIZON_HOURS,
    )
    assert folds == []


def test_a_non_positive_step_is_refused_rather_than_clamped() -> None:
    """Clamping to an epsilon gives every fold the same start and a loop that
    never terminates — a hang, where the truth is a misconfiguration."""
    for step in (0.0, -1.0):
        with pytest.raises(ValueError, match="step_days must be positive"):
            plan_folds(
                build_backtest(step_days=step),
                COHORT_START,
                COHORT_START + timedelta(days=2),
            )


def test_a_negative_embargo_is_refused() -> None:
    """The one configuration that turns the purge into a leak.

    `train_cutoff` is `test_start - purge`, so a negative purge pushes it past
    `train_end` and *into* the test window: the fit would be handed the tokens
    it is about to be graded on, and the result would still be labelled
    out-of-sample. It is refused at the boundary rather than guarded downstream,
    which is what lets `Fold.holds_train` drop a `train_end` term that could
    never have fired.
    """
    with pytest.raises(ValueError, match="must be non-negative"):
        plan_folds(
            build_backtest(embargo_hours=-1.0),
            COHORT_START,
            COHORT_START + timedelta(days=2),
            label_horizon_hours=LABEL_HORIZON_HOURS,
        )
    with pytest.raises(ValueError, match="must be non-negative"):
        plan_folds(
            build_backtest(),
            COHORT_START,
            COHORT_START + timedelta(days=2),
            label_horizon_hours=-1.0,
        )


def test_the_train_cutoff_never_reaches_into_the_test_window() -> None:
    """The invariant `holds_train` is allowed to rely on."""
    for embargo in (0.0, 1.0, 24.0):
        for horizon in (0.0, 6.0):
            for fold in plan_folds(
                build_backtest(embargo_hours=embargo),
                COHORT_START,
                COHORT_START + timedelta(days=3),
                label_horizon_hours=horizon,
            ):
                assert fold.train_cutoff <= fold.train_end == fold.test_start


def test_a_non_positive_train_or_test_window_is_refused() -> None:
    for field in ("train_days", "test_days"):
        with pytest.raises(ValueError, match="must be positive"):
            plan_folds(
                build_backtest(**{field: 0.0}),
                COHORT_START,
                COHORT_START + timedelta(days=2),
            )


# --------------------------------------------------------------------------- #
# leakage
# --------------------------------------------------------------------------- #


def test_no_token_appears_in_both_train_and_its_own_test(
    harness: WalkForward, tapes: list[TokenTape]
) -> None:
    """The assertion the task names.

    It is necessary and it is nowhere near sufficient — see the module
    docstring. `test_the_embargo_purges_the_tail_of_the_train_window` is the one
    that can actually catch a leak.
    """
    folds = harness.folds(tapes, None, None)
    assert folds, "the pinned cohort must produce folds or the rest proves nothing"
    for fold in folds:
        train, test = split_tapes(tapes, fold)
        shared = {t.token.key for t in train} & {t.token.key for t in test}
        assert not shared, f"fold {fold.index} has {len(shared)} tokens in both sides: {shared}"


def test_the_train_window_rolls_rather_than_expands(
    harness: WalkForward, tapes: list[TokenTape]
) -> None:
    """Membership has to honour `train_start`, not just the printed boundaries.

    Found by mutation probe: dropping the `train_start <=` term from
    `Fold.holds_train` left all nineteen other tests green. `train_days` would
    have stopped meaning anything — every fold would fit on the whole corpus to
    date — while `fold_table` went on printing a rolling window it no longer
    used. A silent switch from rolling to expanding is not a cosmetic
    difference: it is the difference between "these weights were fit on two
    weeks" and "these weights were fit on everything, and the earliest fold's
    regime is still voting".
    """
    folds = harness.folds(tapes, None, None)
    assert len(folds) >= 2, "one fold cannot show a window moving"
    for fold in folds:
        for tape in split_tapes(tapes, fold)[0]:
            assert fold.train_start <= tape.launch.created_at < fold.train_end, (
                f"{tape.token.key} launched {tape.launch.created_at.isoformat()}, "
                f"outside fold {fold.index}'s train window "
                f"{fold.train_start.isoformat()} .. {fold.train_end.isoformat()}"
            )

    # And concretely: something the first fold trained on must have aged out of
    # the last fold's window, or the assertion above never had a chance to fire.
    first = {t.token.key for t in split_tapes(tapes, folds[0])[0]}
    last = {t.token.key for t in split_tapes(tapes, folds[-1])[0]}
    assert first - last, (
        "no token aged out of the train window across the run — the fixture "
        "cannot tell a rolling window from an expanding one"
    )


def test_the_embargo_purges_the_tail_of_the_train_window(tapes: list[TokenTape]) -> None:
    """The leak that is actually reachable.

    A token launched just before `train_end` has no label until its outcome
    horizon elapses, and that instant lands inside the test window. With no
    embargo and no label horizon it is admitted to the fit; with them it must be
    gone. Both splits run over the same cohort and the same window, so the only
    difference is the purge.
    """
    window_end = COHORT_START + timedelta(days=2)
    naive = plan_folds(
        build_backtest(embargo_hours=0.0), COHORT_START, window_end, label_horizon_hours=0.0
    )
    purged = plan_folds(
        build_backtest(embargo_hours=6.0), COHORT_START, window_end, label_horizon_hours=6.0
    )
    assert len(naive) == len(purged)

    dropped_somewhere = False
    for loose, tight in zip(naive, purged, strict=True):
        assert loose.test_start == tight.test_start, "only the train cutoff may differ"
        loose_train = {t.token.key for t in split_tapes(tapes, loose)[0]}
        tight_train = {t.token.key for t in split_tapes(tapes, tight)[0]}
        assert tight_train <= loose_train, "the purge may only remove train tokens"
        dropped_somewhere = dropped_somewhere or bool(loose_train - tight_train)
        # Nothing that survives the purge may have a label that resolves after
        # the test window opens.
        for tape in split_tapes(tapes, tight)[0]:
            knowable = tape.launch.created_at + timedelta(hours=6.0)
            assert knowable <= tight.test_start, (
                f"{tape.token.key} was fit on although its label only became "
                f"knowable at {knowable.isoformat()}, inside the test window "
                f"opening {tight.test_start.isoformat()}"
            )
    assert dropped_somewhere, (
        "the embargo removed nothing from any fold — the fixture cannot "
        "exercise the purge and this test is proving nothing"
    )


def test_every_training_example_predates_the_test_window(
    harness: WalkForward, tapes: list[TokenTape]
) -> None:
    """The feature vectors themselves, not just fold membership.

    Membership is decided on `created_at`; the row is sampled at
    `created_at + train_sample_age`, which is a later instant and the one that
    could actually cross the boundary.
    """
    fold = harness.folds(tapes, None, None)[0]
    train, _ = split_tapes(tapes, fold)
    for example in harness.training_examples(train):
        assert example.as_of < fold.test_start, (
            f"{example.token_key} was sampled at {example.as_of.isoformat()}, "
            f"at or after the test window opened at {fold.test_start.isoformat()}"
        )


def test_an_unlabelled_tape_never_enters_train(tapes: list[TokenTape]) -> None:
    """`TrainingExample.from_values` reads
    `max_realizable_multiple or max_multiple_from_t0 or 0.0`, so an unlabelled
    tape does not go missing from the fit — it arrives as a confident zero.
    """
    fold = plan_folds(
        build_backtest(), COHORT_START, COHORT_START + timedelta(days=2), label_horizon_hours=1.0
    )[0]
    train, _ = split_tapes(tapes, fold)
    assert train, "the fixture must put something in train or this proves nothing"

    victim = train[0]
    unlabelled = TokenTape(
        launch=victim.launch,
        snapshots=victim.snapshots,
        trades=victim.trades,
        holders=victim.holders,
        posts=victim.posts,
        security=victim.security,
        wallet_priors=victim.wallet_priors,
        outcome=Outcome(token=victim.token, labeled_at=utcnow()),
    )
    swapped = [unlabelled if t.token.key == victim.token.key else t for t in tapes]
    after, _ = split_tapes(swapped, fold)
    assert victim.token.key not in {t.token.key for t in after}
    assert len(after) == len(train) - 1

    missing_outcome = [t for t in swapped if t.token.key != victim.token.key]
    missing_outcome.append(
        TokenTape(launch=victim.launch, snapshots=victim.snapshots, outcome=None)
    )
    assert victim.token.key not in {t.token.key for t in split_tapes(missing_outcome, fold)[0]}


def test_a_test_tape_needs_no_label(tapes: list[TokenTape]) -> None:
    """Test membership must not require an outcome: the test window is scored,
    not fitted, and dropping unlabelled tokens from it would quietly restrict
    the out-of-sample universe to tokens that happened to get labelled."""
    fold = plan_folds(
        build_backtest(), COHORT_START, COHORT_START + timedelta(days=2), label_horizon_hours=1.0
    )[0]
    before = {t.token.key for t in split_tapes(tapes, fold)[1]}
    stripped = [
        TokenTape(
            launch=t.launch,
            snapshots=t.snapshots,
            trades=t.trades,
            holders=t.holders,
            posts=t.posts,
            security=t.security,
            wallet_priors=t.wallet_priors,
            outcome=None,
        )
        for t in tapes
    ]
    assert {t.token.key for t in split_tapes(stripped, fold)[1]} == before
    assert before, "the fixture must put something in test"


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def test_the_run_produces_folds_and_reports_them_out_of_sample(
    report: WalkForwardReport,
) -> None:
    assert report.folds, "the pinned cohort must produce at least one fold"
    summary = report.summary()
    assert summary["out_of_sample"] is True
    assert summary["folds"] == len(report.folds)
    assert summary["universe_size"] == sum(f.test_size for f in report.folds), (
        "the pooled universe is the test windows only — a train token counted "
        "here would mean the headline was assembled from data the fit had seen"
    )


def test_capital_compounds_across_folds(report: WalkForwardReport) -> None:
    """Test windows are contiguous and disjoint, so resetting to the initial
    stake each fold draws an equity curve nobody could have traded."""
    for previous, current in zip(report.folds, report.folds[1:], strict=False):
        assert current.starting_native == pytest.approx(max(previous.ending_native, 0.0))


def test_an_impossible_window_reports_a_note_rather_than_a_backtest(
    tapes: list[TokenTape],
) -> None:
    """Fourteen days of train against a two-day cohort. The honest output is no
    folds and a note saying why, not a table with zeros in it."""
    harness = WalkForward(load_settings(), label_horizon_hours=LABEL_HORIZON_HOURS)
    report = harness.run(tapes, synthetic=True)
    assert report.folds == []
    assert any("shorter than one train+test span" in note for note in report.notes)

    pooled = report.pooled()
    assert pooled.evaluated == 0
    assert pooled.trades == []
    summary = report.summary()
    assert summary["folds"] == 0
    assert summary["expectancy_ci_95"] is None


def test_folds_below_the_sample_floor_are_named_as_unfitted(
    report: WalkForwardReport,
) -> None:
    """A fold that ran on default weights tests the harness, not a model, and
    the report has to say which is which rather than let a fold table full of
    `v0-default` pass for a fitted result."""
    assert report.fitted_folds == 0, (
        f"a {COHORT_SIZE}-token cohort cannot reach {MIN_SAMPLES_TO_FIT} training "
        "examples; if this passes the fit floor was lowered"
    )
    assert any(str(MIN_SAMPLES_TO_FIT) in note for note in report.notes)
    for row in report.summary()["fold_table"]:
        assert row["fitted"] is False
        assert row["weights_version"] == "v0-default"


def test_a_fold_never_inherits_the_previous_folds_fit(
    harness: WalkForward, tapes: list[TokenTape]
) -> None:
    """Below the floor `fit_fold` must return the defaults, not silently reuse
    whatever the last fold managed to fit."""
    folds = harness.folds(tapes, None, None)
    fit = harness.fit_fold(split_tapes(tapes, folds[0])[0])
    assert fit.fitted is False
    assert fit.weights.version == "v0-default"
    assert any(str(MIN_SAMPLES_TO_FIT) in w for w in fit.warnings)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def test_absence_of_an_interval_is_not_an_interval_spanning_zero() -> None:
    """`bootstrap_expectancy_ci` returns `(0.0, 0.0)` below
    `BOOTSTRAP_MIN_TRADES`. Reported as an interval it reads "not
    distinguishable from no edge" — a verdict on the strategy, where the truth
    is that nothing was measured.
    """
    thin = _report_with_trades(BOOTSTRAP_MIN_TRADES - 1)
    summary = thin.summary()
    assert summary["expectancy_ci_95"] is None
    assert "no confidence interval" in summary["warning_ci"]
    assert "not distinguishable from no edge" not in summary["warning_ci"]

    thick = _report_with_trades(BOOTSTRAP_MIN_TRADES + 20)
    summary = thick.summary()
    assert isinstance(summary["expectancy_ci_95"], list)
    low, high = summary["expectancy_ci_95"]
    assert low <= high


def test_the_summary_always_carries_its_sample_size(report: WalkForwardReport) -> None:
    """A walk-forward number quoted without how many trades produced it is not
    a result. `trades` and the interval travel with the headline or the headline
    does not go out."""
    summary = report.summary()
    assert "trades" in summary
    assert "expectancy_ci_95" in summary
    assert summary["synthetic"] is True
    assert "warning_synthetic" in summary


def test_pooled_coverage_is_weighted_by_evaluations() -> None:
    """A plain mean over folds gives a twelve-evaluation fold the same say as a
    two-hundred-evaluation one, which is how a metric that died in the big fold
    goes on reading healthy."""
    report = WalkForwardReport(
        folds=[
            _fold_result(0, evaluated=300, coverage={"m": 0.0}),
            _fold_result(1, evaluated=100, coverage={"m": 1.0}),
        ]
    )
    pooled = report.pooled()
    assert pooled.evaluated == 400
    assert pooled.metric_coverage["m"] == pytest.approx(0.25)
    assert pooled.metric_coverage["m"] != pytest.approx(0.5), "that is the plain mean"


def test_pooled_vetoes_and_absences_survive_pooling() -> None:
    report = WalkForwardReport(
        folds=[
            _fold_result(0, evaluated=10, coverage={}, vetoes={"honeypot": 2}),
            _fold_result(1, evaluated=10, coverage={}, vetoes={"honeypot": 3, "dead": 1}),
        ]
    )
    pooled = report.pooled()
    assert pooled.veto_counts == {"dead": 1, "honeypot": 5}
    assert pooled.absence_reasons == {"quiet": "no rows"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fold_result(
    index: int,
    *,
    evaluated: int,
    coverage: dict[str, float],
    vetoes: dict[str, int] | None = None,
) -> FoldResult:
    now = utcnow()
    fold = Fold(
        index=index,
        train_start=COHORT_START,
        train_end=COHORT_START + timedelta(hours=12),
        test_start=COHORT_START + timedelta(hours=12),
        test_end=COHORT_START + timedelta(hours=18),
        train_cutoff=COHORT_START + timedelta(hours=11),
    )
    result = BacktestResult(
        started_at=now,
        finished_at=now,
        window_start=fold.test_start,
        window_end=fold.test_end,
        universe_size=1,
        evaluated=evaluated,
        entered=0,
        metric_coverage=dict(coverage),
        veto_counts=dict(vetoes or {}),
        absence_reasons={"quiet": "no rows"},
    )
    return FoldResult(
        fold=fold,
        train_size=0,
        test_size=1,
        fit=FitReport(weights=Weights(version="v0-default"), n_samples=0, fitted=False),
        result=result,
        starting_native=10.0,
        ending_native=10.0,
    )


def _report_with_trades(n: int) -> WalkForwardReport:
    """A report carrying `n` out-of-sample trades and nothing else of interest."""
    from botsensai.backtest.engine import TradeRecord

    now = utcnow()
    trades = [
        TradeRecord(
            token_key=f"solana:t{i}",
            symbol=f"T{i}",
            entered_at=now,
            exited_at=now + timedelta(minutes=5),
            score=0.7,
            coverage=0.8,
            regime="normal",
            size_native=0.1,
            pnl_native=0.001 * (i - n / 2),
            multiple=1.0 + 0.01 * i,
            exit_reason="test",
            entry_slippage_bps=5.0,
            hold_seconds=300.0,
        )
        for i in range(n)
    ]
    fold_result = _fold_result(0, evaluated=100, coverage={})
    fold_result.result.trades = trades
    return WalkForwardReport(folds=[fold_result])
