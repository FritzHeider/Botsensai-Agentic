"""Tests for the backpressure harness in `scripts/backpressure.py`.

The script exists because two iterations were lost to a payload that claimed a
check had passed when the check had never run. These tests guard the two parts
of it that can silently produce a wrong number — the complexity parser and the
duplication analyser — and the format contract on the evidence line, which is
the thing the loop's gate actually reads.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "backpressure.py"


def _load():
    # scripts/ is not a package and is not on the path; load it by location so
    # the test exercises the file that actually runs, not a copy of it.
    spec = importlib.util.spec_from_file_location("_backpressure", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bp = _load()


# --- complexity ------------------------------------------------------------- #

RUFF_OUTPUT = """\
src/botsensai/util/synthetic.py:135:5: C901 `generate_token` is too complex (31 > 0)
src/botsensai/collectors/pumpfun_ws.py:380:15: C901 `run` is too complex (20 > 0)
src/botsensai/scoring/composite.py:191:9: C901 `evaluate` is too complex (22 > 0)
Found 3 errors.
"""


def test_parse_complexity_reads_scores_worst_first():
    scored = bp.parse_complexity(RUFF_OUTPUT)
    assert [s for s, _ in scored] == [31, 22, 20]
    assert scored[0][1] == "src/botsensai/util/synthetic.py generate_token"


def test_parse_complexity_ignores_lines_that_are_not_c901():
    noise = "src/botsensai/cli.py:1:1: F401 `os` imported but unused\nFound 1 error.\n"
    assert bp.parse_complexity(noise) == []


def test_complexity_gate_reports_the_score_not_a_verdict(monkeypatch):
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (1, RUFF_OUTPUT))
    monkeypatch.setattr(bp, "MAX_COMPLEXITY", 40)
    gate = bp.gate_complexity()
    assert gate.ok is True
    assert gate.value == "31"  # a number, because the loop's gate parses one


def test_complexity_gate_fails_when_a_function_exceeds_the_ratchet(monkeypatch):
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (1, RUFF_OUTPUT))
    monkeypatch.setattr(bp, "MAX_COMPLEXITY", 25)
    assert bp.gate_complexity().ok is False


def test_complexity_gate_is_red_when_ruff_reports_nothing(monkeypatch):
    # Silence here means the command failed or the flag stopped working. It is
    # not a repository with zero complexity, and must never read as green.
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (2, "error: unknown option\n"))
    gate = bp.gate_complexity()
    assert gate.ok is False
    assert gate.value == "0"


# --- the suite gate ---------------------------------------------------------- #

PYTEST_OUTPUT = """\
........................................................................ [ 42%]
........................................................................ [ 85%]
........................                                                 [100%]
---------- coverage: platform darwin, python 3.13.0-final-0 ----------
Name                             Stmts   Miss  Cover
----------------------------------------------------
TOTAL                             7872   2916    63%

181 passed in 141.02s (0:02:21)
"""


def test_suite_gate_reads_the_count_and_the_coverage_total(monkeypatch):
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (0, PYTEST_OUTPUT))
    tests, coverage = bp.gate_suite()
    assert (tests.ok, tests.value) == (True, "pass")
    assert "181 passed" in tests.detail
    assert (coverage.ok, coverage.value) == (True, "pass")
    assert "63%" in coverage.detail


def test_suite_gate_is_red_when_no_test_count_can_be_read(monkeypatch):
    # This is not hypothetical: `addopts = "-q"` plus a second `-q` is `-qq`,
    # which drops the summary line. pytest exits 0, the parser sees no tests,
    # and a green run would otherwise be reported off an empty measurement.
    silent = PYTEST_OUTPUT.replace("181 passed in 141.02s (0:02:21)", "")
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (0, silent))
    tests, _ = bp.gate_suite()
    assert tests.ok is False


def test_suite_gate_does_not_pass_a_second_quiet_flag(monkeypatch):
    seen: list[list[str]] = []

    def fake(cmd, timeout=1800.0):
        seen.append(cmd)
        return 0, PYTEST_OUTPUT

    monkeypatch.setattr(bp, "run", fake)
    bp.gate_suite()
    assert "-q" not in seen[0], "pytest addopts already carries -q; a second one hides the count"


def test_counts_come_from_the_summary_line_only(monkeypatch):
    # A captured "Found 1 error." from a tool the suite shelled out to is not a
    # test error, and counting it would turn a green run red for no reason.
    noisy = PYTEST_OUTPUT.replace(
        "----------------------------------------------------",
        "captured stdout: ruff said Found 1 error.",
    )
    assert bp.pytest_counts(noisy) == (181, 0, 0)
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (0, noisy))
    tests, _ = bp.gate_suite()
    assert tests.ok is True


def test_counts_read_a_real_failure(monkeypatch):
    failing = PYTEST_OUTPUT.replace(
        "181 passed in 141.02s (0:02:21)", "2 failed, 179 passed in 140.11s"
    )
    assert bp.pytest_counts(failing) == (179, 2, 0)
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (1, failing))
    tests, _ = bp.gate_suite()
    assert (tests.ok, tests.value) == (False, "fail")


def test_suite_gate_reports_missing_coverage_as_red_not_as_zero(monkeypatch):
    no_total = PYTEST_OUTPUT.replace("TOTAL                             7872   2916    63%", "")
    monkeypatch.setattr(bp, "run", lambda cmd, timeout=1800.0: (0, no_total))
    _, coverage = bp.gate_suite()
    assert coverage.ok is False
    assert "no coverage total" in coverage.detail


# --- duplication ------------------------------------------------------------ #

BLOCK = """\
def a():
    total = 0
    for i in range(10):
        total += i
    if total > 3:
        total -= 1
    return total
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_duplication_finds_a_block_pasted_between_two_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path)
    one = _write(tmp_path, "one.py", BLOCK)
    two = _write(tmp_path, "two.py", BLOCK.replace("def a(", "def b("))
    pct, lines, worst = bp.duplication_report([one, two], window=6)
    assert lines == 12  # six lines, counted in both copies
    assert pct == pytest.approx(100.0 * 12 / 14)
    assert worst and worst[0].startswith("2x ")


def test_duplication_is_zero_when_nothing_repeats(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path)
    one = _write(tmp_path, "one.py", BLOCK)
    two = _write(tmp_path, "two.py", BLOCK.replace("total", "count").replace("10", "20"))
    pct, lines, worst = bp.duplication_report([one, two], window=6)
    assert (pct, lines, worst) == (0.0, 0, [])


def test_duplication_ignores_comments_and_blank_lines(tmp_path, monkeypatch):
    # Two files whose only shared text is boilerplate comments are not clones.
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path)
    header = "# the same\n# header\n\n# on both\n\n# files\n\n"
    one = _write(tmp_path, "one.py", header + "x = 1\n")
    two = _write(tmp_path, "two.py", header + "y = 2\n")
    pct, lines, _ = bp.duplication_report([one, two], window=6)
    assert (pct, lines) == (0.0, 0)


def test_duplication_gate_fails_over_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bp, "MAX_DUPLICATION_PCT", 3.0)
    one = _write(tmp_path, "one.py", BLOCK)
    two = _write(tmp_path, "two.py", BLOCK.replace("def a(", "def b("))
    assert bp.gate_duplication([one, two]).ok is False


# --- the evidence line ------------------------------------------------------- #


def test_evidence_line_covers_every_dimension_the_gate_names():
    # These are the keys the loop's own rejection message lists. Dropping one
    # is how a build.done gets rejected for "missing backpressure evidence".
    assert bp.GATE_KEYS == [
        "tests",
        "lint",
        "typecheck",
        "audit",
        "coverage",
        "complexity",
        "duplication",
        "performance",
        "specs",
    ]


def _fake_gates(**overrides) -> list[bp.Gate]:
    values = dict.fromkeys(bp.GATE_KEYS, "pass")
    values["complexity"] = "31"
    values["performance"] = "ok"
    values.update(overrides)
    return [bp.Gate(k, values[k] in ("pass", "ok"), values[k], "measured") for k in bp.GATE_KEYS]


def test_evidence_values_are_bare_tokens():
    # The contract with the loop's parser: it splits on commas and reads the
    # word after the colon. `performance: pass (median 1.31s, p90 1.73s)` was
    # rejected twice for exactly this. No value may carry punctuation.
    line = bp.evidence_line(_fake_gates())
    for pair in line.split(", "):
        key, _, value = pair.partition(": ")
        assert key in bp.GATE_KEYS
        assert value.isalnum(), f"{key} carries free text: {value!r}"


def test_evidence_reports_performance_as_ok_or_regression():
    assert "performance: ok" in bp.evidence_line(_fake_gates())
    red = bp.evidence_line(_fake_gates(performance="regression"))
    assert "performance: regression" in red


def test_evidence_reports_a_red_gate_as_fail():
    line = bp.evidence_line(_fake_gates(audit="fail"))
    assert "audit: fail" in line
    assert "audit: pass" not in line
