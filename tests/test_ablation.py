"""Tests for the metric ablation harness and CLI command."""

import json

from typer.testing import CliRunner

from botsensai.backtest.engine import Backtester
from botsensai.backtest.walkforward import WalkForward
from botsensai.cli import app
from botsensai.config import get_settings
from botsensai.metrics import build_registry
from botsensai.scoring.fit import AblationReport, ablation
from botsensai.util.synthetic import generate_cohort, synthetic_outcome


def test_ablation_function_returns_report_covering_all_metrics():
    """`ablation` returns an AblationReport with one row per registered metric."""
    registry = build_registry()
    settings = get_settings()
    cohort = generate_cohort(n=40, seed=1337)
    outcomes = {t.launch.token.key: synthetic_outcome(t) for t in cohort}
    tapes = Backtester.tapes_from_synthetic(cohort, outcomes=outcomes)

    wf = WalkForward(settings, registry)
    examples = wf.training_examples(tapes)
    assert len(examples) == 40

    report = ablation(examples, registry=registry, seed=1337)
    assert isinstance(report, AblationReport)
    assert len(report.rows) == len(registry)
    assert report.n_examples == 40
    assert report.n_train + report.n_holdout == 40

    # Ensure all registered metric IDs are in report rows
    metric_ids = {r.metric_id for r in report.rows}
    assert metric_ids == set(registry.ids())


def test_cli_ablate_synthetic_exits_zero_and_prints_table(tmp_path):
    """`botsensai ablate --synthetic` exits 0, prints 34 metric rows, and writes output if requested."""
    out_file = tmp_path / "ablation.json"
    result = CliRunner().invoke(app, ["ablate", "--synthetic", "--universe", "40", "--out", str(out_file)])

    assert result.exit_code == 0, result.stdout
    assert "metric ablation report (34 metrics)" in result.stdout
    assert "SYNTHETIC RUN" in result.stdout
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    assert "summary" in data
    assert len(data["rows"]) == 34
    assert isinstance(data["liabilities"], list)
