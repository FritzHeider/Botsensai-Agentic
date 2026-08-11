"""Tests for the WeightFitter CLI command."""

from __future__ import annotations

from typer.testing import CliRunner

from botsensai.cli import app


def test_cli_fit_synthetic_exits_zero_and_prints_report():
    """`botsensai fit --synthetic` exits 0 and prints weight fit report."""
    result = CliRunner().invoke(app, ["fit", "--synthetic", "--universe", "40", "--min-samples", "30"])

    assert result.exit_code == 0, result.stdout
    assert "weight fit report" in result.stdout
    assert "SYNTHETIC RUN" in result.stdout
    assert "Fit summary:" in result.stdout


def test_cli_fit_insufficient_samples(tmp_path):
    """`botsensai fit --min-samples 1000` exits 0 with a clear insufficient samples message."""
    out_file = tmp_path / "weights.json"
    result = CliRunner().invoke(
        app, ["fit", "--synthetic", "--universe", "30", "--min-samples", "1000", "--out", str(out_file)]
    )

    assert result.exit_code == 0, result.stdout
    assert "Fitting skipped" in result.stdout
