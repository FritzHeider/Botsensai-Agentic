"""The synthetic metric-coverage baseline, and the block that records it.

Coverage is the constraint this project actually lives under: a metric that
produces a usable value one evaluation in twenty is not a signal, whatever its
weight says. This module measures coverage over a *pinned* synthetic universe
and renders it into `docs/RESULTS.md`, so a collector that quietly stops
producing an input shows up as a failing test instead of as a slightly worse
backtest nobody reads.

Two properties make the number a gate rather than a reading:

* The run is reproducible. `generate_token` is seeded, the cohort's
  `created_at` is pinned here rather than defaulted to wall-clock, and the
  universe size is fixed — coverage moves with universe size, because the
  cross-sectional metrics need peers.
* Absence is recorded with its reason. Four metrics read 0.0 in any synthetic
  run because the fixture has no accounts, no theme memory and no wallet skill
  profiles — not because they are broken. They are listed with the note the
  metric itself emitted, and excluded from the headline mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from botsensai.config import Settings

# The pinned run. Changing any of these invalidates the recorded baseline, so
# they live here rather than in the caller: the doc, the gate and the recorder
# must all describe the same run or the comparison is meaningless.
BASELINE_UNIVERSE = 40
BASELINE_CREATED_AT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

# The measurement is deterministic, so these tolerances exist for honest churn
# in the metrics themselves, not for noise. Per-metric is looser than the mean
# because one metric moving two points is ordinary; the whole population moving
# one point is drift.
METRIC_TOLERANCE = 0.02
MEAN_TOLERANCE = 0.01

# A metric recorded as absent that starts producing values must be re-recorded
# rather than left ungated, or it can drift back down unnoticed.
REVIVAL_THRESHOLD = 0.05

BEGIN_MARKER = "<!-- coverage-baseline:begin -->"
END_MARKER = "<!-- coverage-baseline:end -->"

_ABSENT_PREFIX = "absent — "
_MEASURED = "measured"
_NO_REASON = "no reason recorded"


@dataclass
class CoverageBaseline:
    """One measured (or one parsed) coverage baseline."""

    coverage: dict[str, float] = field(default_factory=dict)
    absence_reasons: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    @property
    def participating(self) -> dict[str, float]:
        """Metrics the synthetic fixture can exercise at all."""
        return {k: v for k, v in self.coverage.items() if v > 0.0}

    @property
    def absent(self) -> dict[str, str]:
        """Metrics at zero coverage, mapped to the note the metric emitted."""
        return {
            k: self.absence_reasons.get(k, _NO_REASON)
            for k, v in sorted(self.coverage.items())
            if v <= 0.0
        }

    @property
    def mean_coverage(self) -> float:
        """Mean over the participating set — see the module docstring."""
        values = list(self.participating.values())
        if not values:
            return 0.0
        return sum(values) / len(values)


def measure_synthetic_coverage(
    settings: Settings | None = None,
    *,
    universe: int = BASELINE_UNIVERSE,
    created_at: datetime = BASELINE_CREATED_AT,
) -> CoverageBaseline:
    """Replay a pinned synthetic cohort and report per-metric coverage."""
    from botsensai.backtest.engine import Backtester
    from botsensai.config import load_settings
    from botsensai.util.synthetic import generate_cohort

    # A fresh Settings rather than the process-wide singleton: an earlier test
    # that reassigned a field on the singleton must not be able to move the
    # number this baseline is compared against.
    resolved = settings if settings is not None else load_settings()
    cohort = generate_cohort(n=universe, seed=resolved.seed, created_at=created_at)
    result = Backtester(resolved).run(
        Backtester.tapes_from_synthetic(cohort), synthetic=True
    )
    return CoverageBaseline(
        coverage=dict(sorted(result.metric_coverage.items())),
        absence_reasons=dict(result.absence_reasons),
        params={
            "universe": str(universe),
            "seed": str(resolved.seed),
            "created_at": created_at.isoformat(),
            "evaluated": str(result.evaluated),
            "metrics": str(len(result.metric_coverage)),
        },
    )


# --------------------------------------------------------------------------- #
# rendering and parsing — the doc is the baseline, not a copy of it
# --------------------------------------------------------------------------- #


def render_block(baseline: CoverageBaseline) -> str:
    """The marked block recorded in `docs/RESULTS.md`."""
    params = dict(baseline.params)
    params["participating"] = str(len(baseline.participating))
    params["mean_coverage"] = f"{baseline.mean_coverage:.4f}"

    lines = [
        BEGIN_MARKER,
        "",
        "<!-- Machine-read by tests/test_coverage_gate.py. Do not hand-edit:",
        "     regenerate with `python scripts/coverage_baseline.py`. -->",
        "",
        *(f"- {k}: {v}" for k, v in params.items()),
        "",
        "| metric | coverage | status |",
        "| --- | --- | --- |",
    ]
    for metric_id, value in sorted(baseline.coverage.items()):
        status = (
            _MEASURED
            if value > 0.0
            else _ABSENT_PREFIX + baseline.absence_reasons.get(metric_id, _NO_REASON)
        )
        lines.append(f"| {metric_id} | {value:.4f} | {status} |")
    lines += ["", END_MARKER]
    return "\n".join(lines)


def parse_block(text: str) -> CoverageBaseline:
    """Read back a rendered block. Raises if the block is missing or malformed."""
    body = _extract_block(text)
    baseline = CoverageBaseline(params=_parse_params(body))
    for metric_id, value, status in _parse_rows(body):
        baseline.coverage[metric_id] = value
        if status.startswith(_ABSENT_PREFIX):
            baseline.absence_reasons[metric_id] = status[len(_ABSENT_PREFIX) :].strip()
        elif status != _MEASURED:
            raise ValueError(f"unknown status {status!r} for metric {metric_id!r}")
    if not baseline.coverage:
        raise ValueError("coverage baseline block contains no metric rows")
    return baseline


def _extract_block(text: str) -> str:
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER, start + 1)
    if start < 0 or end < 0:
        raise ValueError(
            f"no coverage baseline block found between {BEGIN_MARKER} and {END_MARKER}"
        )
    return text[start + len(BEGIN_MARKER) : end]


def _parse_params(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in body.splitlines():
        match = re.fullmatch(r"-\s+([a-z_]+):\s+(.+?)\s*", line)
        if match:
            params[match.group(1)] = match.group(2)
    return params


def _parse_rows(body: str) -> list[tuple[str, float, str]]:
    rows: list[tuple[str, float, str]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in {"metric", "---"}:
            continue
        rows.append((cells[0], float(cells[1]), cells[2]))
    return rows


def write_block(path: Path, baseline: CoverageBaseline) -> None:
    """Replace the marked block in `path`, leaving the rest of the doc alone."""
    text = path.read_text(encoding="utf-8")
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER, start + 1)
    if start < 0 or end < 0:
        raise ValueError(f"{path} has no coverage baseline block to replace")
    updated = text[:start] + render_block(baseline) + text[end + len(END_MARKER) :]
    path.write_text(updated, encoding="utf-8")


__all__ = [
    "BASELINE_CREATED_AT",
    "BASELINE_UNIVERSE",
    "BEGIN_MARKER",
    "END_MARKER",
    "MEAN_TOLERANCE",
    "METRIC_TOLERANCE",
    "REVIVAL_THRESHOLD",
    "CoverageBaseline",
    "measure_synthetic_coverage",
    "parse_block",
    "render_block",
    "write_block",
]
