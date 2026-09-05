"""The synthetic metric-coverage gate (P2-05).

Coverage is the binding constraint on this system, and it is the one that rots
silently. A collector that stops producing an input does not fail a test: the
metric that needs it returns `(None, 0, reason)` exactly as it is supposed to,
the composite is computed from what is left, and the backtest still prints a
table. Nothing goes red. So this file replays a *pinned* synthetic cohort and
holds the resulting per-metric coverage against the figure recorded in
`docs/RESULTS.md`.

Three things had to be true before that number could be a gate rather than a
reading, and each of them is pinned by a test here.

**The fixture had to become reproducible.** It was not.
`generate_token` seeded its RNG with `seed * 7919 + hash(archetype) % 10_000`,
and `hash` of a `str` is salted by `PYTHONHASHSEED` — so a generator documented
as seeded produced different tokens in every process. `_wallet_ages` iterated
`{t.wallet for t in trades}`, a set of strings, which drew from the RNG in a
per-process order. Measured before the fix, across two hash seeds:
`derivative_remix_depth` read 0.0646 and 0.0413, a 36% relative swing on the
same command. A gate on that cannot tell drift from salt, so
`test_the_cohort_is_identical_across_processes` runs the generator in two
subprocesses under different `PYTHONHASHSEED` values — the only place the bug
is visible, since within one process it does not exist.

**Absence had to be told apart from breakage.** Four metrics read exactly 0.0
in any synthetic run — no `SocialAccount` rows, no theme memory, no wallet
skill profiles — and a metric whose collector died reads exactly 0.0 too. They
are excluded from the headline mean and recorded with the note the metric
itself emitted, and `test_absent_metrics_are_recorded_with_their_reason` keeps
them that way. `test_a_revived_metric_must_be_re_recorded` closes the other
end: once one of them starts producing values it has to be re-recorded, or it
would sit ungated and be free to drift back down.

**The mean alone is not enough.** Over 30 participating metrics a single metric
can lose 0.30 of coverage — a third of its usable evaluations — before the mean
moves by `MEAN_TOLERANCE` at all, and the mean never says which metric.
Measured, not argued: raising `holder_distribution_health`'s minimum from 8
tradeable holders to 20 takes it from 0.9833 to 0.9517 and moves the mean by
0.0010, and `pytest -k "mean_coverage_holds or each_metric_holds"` under that
mutation reports `.F` — the mean gate green, the per-metric floor red and
naming the metric. The mean gate is for the broad drift no single metric owns.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from botsensai.backtest.coverage import (
    BASELINE_CREATED_AT,
    BASELINE_UNIVERSE,
    MEAN_TOLERANCE,
    METRIC_TOLERANCE,
    REVIVAL_THRESHOLD,
    CoverageBaseline,
    measure_synthetic_coverage,
    parse_block,
    render_block,
)
from botsensai.metrics import build_registry

RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "RESULTS.md"

_RERECORD = "re-record with `python scripts/coverage_baseline.py --write`"


@pytest.fixture(scope="module")
def recorded() -> CoverageBaseline:
    """The baseline committed to docs/RESULTS.md."""
    return parse_block(RESULTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def measured() -> CoverageBaseline:
    """One replay of the pinned cohort. Module-scoped: it takes ~16 seconds."""
    return measure_synthetic_coverage()


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def test_mean_coverage_holds_the_committed_baseline(
    recorded: CoverageBaseline, measured: CoverageBaseline
) -> None:
    floor = recorded.mean_coverage - MEAN_TOLERANCE
    assert measured.mean_coverage >= floor, (
        f"mean coverage over participating metrics fell to "
        f"{measured.mean_coverage:.4f}, below the recorded "
        f"{recorded.mean_coverage:.4f} - {MEAN_TOLERANCE}. Either a collector's "
        f"input stopped arriving, or the drop is intended and you should {_RERECORD}."
    )


def test_each_metric_holds_its_own_floor(
    recorded: CoverageBaseline, measured: CoverageBaseline
) -> None:
    """The test that catches one dead input while the other 29 compensate."""
    dropped = {
        metric_id: (before, measured.coverage.get(metric_id, 0.0))
        for metric_id, before in recorded.participating.items()
        if measured.coverage.get(metric_id, 0.0) < before - METRIC_TOLERANCE
    }
    assert not dropped, f"metrics below their recorded floor: {dropped}. {_RERECORD} to accept."


def test_a_revived_metric_must_be_re_recorded(
    recorded: CoverageBaseline, measured: CoverageBaseline
) -> None:
    """An absent metric that starts working must be gated, not left at zero."""
    revived = {
        metric_id: measured.coverage.get(metric_id, 0.0)
        for metric_id in recorded.absent
        if measured.coverage.get(metric_id, 0.0) >= REVIVAL_THRESHOLD
    }
    assert not revived, (
        f"{revived} now produce values but are recorded as absent, so nothing "
        f"holds them there. {_RERECORD}."
    )


def test_every_registered_metric_is_recorded(recorded: CoverageBaseline) -> None:
    """A metric added without a recorded figure is a metric nothing gates."""
    registered = {m.id for m in build_registry()}
    assert registered == set(recorded.coverage), (
        f"registered but not recorded: {sorted(registered - set(recorded.coverage))}; "
        f"recorded but not registered: {sorted(set(recorded.coverage) - registered)}. {_RERECORD}."
    )


def test_absent_metrics_are_recorded_with_their_reason(recorded: CoverageBaseline) -> None:
    """0.0 for want of a fixture and 0.0 for a dead collector look identical."""
    assert recorded.absent, "expected the four structurally absent metrics to be recorded"
    for metric_id, reason in recorded.absent.items():
        assert reason and reason != "no reason recorded", (
            f"{metric_id} is recorded at 0.0 with no reason, so nobody can tell "
            f"a missing input from a broken metric"
        )


def test_the_mean_excludes_the_absent_metrics(recorded: CoverageBaseline) -> None:
    """Averaging structural zeroes in would understate every real metric."""
    assert set(recorded.participating).isdisjoint(recorded.absent)
    over_all = sum(recorded.coverage.values()) / len(recorded.coverage)
    assert recorded.mean_coverage > over_all


def test_the_recorded_run_is_the_run_the_gate_replays(recorded: CoverageBaseline) -> None:
    """The doc's parameters and the measurement's must not drift apart."""
    assert recorded.params["universe"] == str(BASELINE_UNIVERSE)
    assert recorded.params["created_at"] == BASELINE_CREATED_AT.isoformat()


def test_measured_evaluations_match_the_recorded_run(
    recorded: CoverageBaseline, measured: CoverageBaseline
) -> None:
    """A different number of decision points means a different measurement."""
    assert measured.params["evaluated"] == recorded.params["evaluated"]


# --------------------------------------------------------------------------- #
# the fixture the gate rests on
# --------------------------------------------------------------------------- #


_DIGEST_SOURCE = """
import hashlib, json, sys
sys.path.insert(0, %r)
from datetime import UTC, datetime
from botsensai.util.synthetic import generate_cohort

cohort = generate_cohort(n=4, seed=1337, created_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
parts = []
for token in cohort:
    parts.append(token.archetype)
    parts.append(token.launch.token.mint)
    parts.append(json.dumps(token.wallet_priors, sort_keys=True))
    parts += [f"{t.wallet}:{t.side}:{t.amount_native}:{t.amount_token}" for t in token.trades]
    parts += [f"{h.wallet}:{h.balance}:{h.wallet_age_seconds}" for h in token.holders]
    parts += [p.text or "" for p in token.posts]
print(hashlib.sha256("|".join(parts).encode()).hexdigest())
"""


def _cohort_digest(hash_seed: str) -> str:
    """Digest a cohort in a fresh interpreter with PYTHONHASHSEED pinned."""
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _DIGEST_SOURCE % src],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    return proc.stdout.strip()


def test_the_cohort_is_identical_across_processes() -> None:
    """The regression that made a coverage gate impossible.

    `hash(archetype)` and set-iteration over wallet strings are both salted by
    `PYTHONHASHSEED`, and neither is visible from inside a single process — so
    this is the one test here that has to pay for two interpreters.
    """
    first = _cohort_digest("1")
    second = _cohort_digest("524287")
    assert first and first == second, (
        "the synthetic cohort differs between processes, so no coverage figure "
        "measured from it can be compared to a recorded one. Look for hash() of "
        "a str, or iteration over a set of strings, in botsensai.util.synthetic"
    )


def test_the_cohort_actually_varies_with_its_seed() -> None:
    """Guards the guard: an all-constant generator would also be 'identical'."""
    from datetime import UTC, datetime

    from botsensai.util.synthetic import generate_cohort

    at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    def digest(seed: int) -> str:
        cohort = generate_cohort(n=3, seed=seed, created_at=at)
        blob = "|".join(f"{t.archetype}:{t.launch.token.mint}" for t in cohort)
        return hashlib.sha256(blob.encode()).hexdigest()

    assert digest(1337) != digest(1338)


def test_archetypes_differ_from_one_another() -> None:
    """The three streams must stay separated after the reseeding fix."""
    from datetime import UTC, datetime

    from botsensai.util.synthetic import generate_token

    at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    mints = {a: generate_token(a, seed=5, created_at=at).launch.token.mint for a in
             ("organic", "manufactured", "rug")}
    assert len(set(mints.values())) == 3, mints


# --------------------------------------------------------------------------- #
# the record itself
# --------------------------------------------------------------------------- #


def test_render_and_parse_round_trip() -> None:
    baseline = CoverageBaseline(
        coverage={"alpha": 0.5, "beta": 0.0},
        absence_reasons={"beta": "no theme memory"},
        params={"universe": "40", "evaluated": "2400"},
    )
    back = parse_block(render_block(baseline))
    assert back.coverage == baseline.coverage
    assert back.absence_reasons == {"beta": "no theme memory"}
    assert back.params["universe"] == "40"
    assert back.params["mean_coverage"] == "0.5000"


def test_a_missing_block_is_an_error_not_an_empty_baseline() -> None:
    """Silently reading zero metrics would make every gate above vacuous."""
    with pytest.raises(ValueError, match="no coverage baseline block"):
        parse_block("# Results\n\nnothing recorded here\n")


def test_an_empty_block_is_an_error() -> None:
    with pytest.raises(ValueError, match="no metric rows"):
        parse_block("<!-- coverage-baseline:begin -->\n\n<!-- coverage-baseline:end -->")


def test_an_unknown_status_is_refused() -> None:
    """A hand-edited status must not be read as 'measured'."""
    block = (
        "<!-- coverage-baseline:begin -->\n"
        "| metric | coverage | status |\n| --- | --- | --- |\n"
        "| alpha | 0.0000 | probably fine |\n"
        "<!-- coverage-baseline:end -->"
    )
    with pytest.raises(ValueError, match="unknown status"):
        parse_block(block)


def test_the_recorded_json_survives_a_round_trip(recorded: CoverageBaseline) -> None:
    """The committed doc must parse to exactly what it renders."""
    assert parse_block(render_block(recorded)).coverage == recorded.coverage


def test_the_committed_block_is_what_the_recorder_would_write(
    recorded: CoverageBaseline,
) -> None:
    """No hand-edits: the doc must be byte-identical to a re-render of itself."""
    text = RESULTS_PATH.read_text(encoding="utf-8")
    assert render_block(recorded) in text


def test_absence_reasons_come_from_the_metrics_themselves(
    measured: CoverageBaseline, recorded: CoverageBaseline
) -> None:
    """The recorded reason is the note the metric emitted, not editorial."""
    for metric_id, reason in recorded.absent.items():
        live = measured.absence_reasons.get(metric_id)
        assert live == reason, f"{metric_id}: recorded {reason!r}, metric now says {live!r}"


def test_coverage_is_a_share_not_a_count(recorded: CoverageBaseline) -> None:
    assert all(0.0 <= v <= 1.0 for v in recorded.coverage.values())
    assert json.loads(recorded.params["evaluated"]) > 0
