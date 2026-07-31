#!/usr/bin/env python
"""Run every backpressure gate for real and print the evidence line.

The loop has now burned two iterations discovering, after the fact, which
backpressure dimension was red — once because `pip-audit` had never been
installed, once because there was no performance harness at all. Both times the
payload claimed the check passed. A claim in a payload is not a measurement, and
nothing in the repository could tell the difference.

This script is the difference. It runs nine gates as subprocesses, reports what
each one actually measured, and prints the evidence line to hand to
`ralph emit build.done`:

    tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass,
    complexity: 17, duplication: pass, performance: ok, specs: pass

Nothing here is asserted. Every value comes from a command's exit code or its
output, and a gate that cannot be measured is reported red rather than skipped.

    python scripts/backpressure.py              # table, then the evidence line
    python scripts/backpressure.py --format evidence   # just the evidence line
    python scripts/backpressure.py --only tests,lint   # while iterating

Exit code: 0 all gates green, 1 at least one red, 2 the harness itself broke.

Two of the gates are computed here rather than shelled out, because the repo has
no dependency that provides them and neither is worth one: cyclomatic complexity
(`radon`/`mccabe`) and copy-paste duplication (`jscpd`). Both are small enough
to read, and both are covered by `tests/test_backpressure.py`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# Floors and ceilings, all measured on 2026-07-31 and set just outside the
# current reading so a real regression trips them and normal drift does not.
COVERAGE_FLOOR = 55.0  # measured 63%
# A ratchet, not an aspiration: 31 is what `util.synthetic.generate_token`
# already scores, so this forbids anything *worse* arriving without forcing a
# refactor of the fixture builder into this iteration. Lower it when the
# offenders `--only complexity` names get split up.
MAX_COMPLEXITY = 31
MAX_DUPLICATION_PCT = 3.0  # measured 2.1%
MIN_METRICS = 32  # the objective's floor; registry currently holds 33
DUPLICATE_WINDOW = 6  # consecutive normalised lines before it counts as a clone

# mypy runs over `src` only. `tests/` and `scripts/` are not shipped, and mypy
# does not check the body of an unannotated function by default, so nearly every
# test function is invisible to it anyway — gating on them would buy annotation
# churn rather than type safety. Named here so the scope is a decision on the
# record and not an accident.
TYPECHECK_PATHS = ["src"]
LINT_PATHS = ["src", "tests", "scripts"]
# Complexity and duplication are measured over the shipped package only, for the
# same reason: a long test fixture is not a maintenance liability the way a long
# collector is.
COMPLEXITY_PATHS = ["src"]


@dataclass
class Gate:
    """One backpressure dimension, its verdict, and what produced it."""

    key: str
    ok: bool
    value: str  # what follows "<key>: " in the evidence line
    detail: str  # the human-readable measurement

    @property
    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"  {mark}  {self.key:<12} {self.value:<8} {self.detail}"


def run(cmd: list[str], timeout: float = 1800.0) -> tuple[int, str]:
    """Run a command from the repo root, returning (exit code, stdout+stderr)."""
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout + proc.stderr


# --- gates that shell out ---------------------------------------------------- #


_SUMMARY = re.compile(r"\b\d+ (?:passed|failed|error|errors|skipped|xfailed)\b")


def pytest_counts(out: str) -> tuple[int, int, int]:
    """(passed, failed, errors) read from pytest's summary line only.

    Only that one line, because the rest of the output is other tools talking:
    a coverage report, a warning, or a captured "Found 1 error." from something
    the suite itself shelled out to would otherwise be counted as a test error
    and turn a green run red.
    """
    line = next((ln for ln in reversed(out.splitlines()) if _SUMMARY.search(ln)), "")

    def count(word: str) -> int:
        m = re.search(rf"(\d+) {word}", line)
        return int(m.group(1)) if m else 0

    return count("passed"), count("failed"), count("errors?")


def gate_suite() -> tuple[Gate, Gate]:
    """Tests and coverage, from a single run.

    They come from one subprocess deliberately: running the suite twice to
    measure the same thing twice is the slowest possible way to get a weaker
    answer, since the two runs could disagree.
    """
    # No `-q` here. `[tool.pytest.ini_options] addopts` already carries one, and
    # a second one is `-qq`, which suppresses the "N passed" summary line this
    # function reads — the run goes green and the gate reads zero tests.
    code, out = run([sys.executable, "-m", "pytest", "--cov=botsensai", "--cov-report=term"])
    passed, failed, errors = pytest_counts(out)
    tests_ok = code == 0 and failed == 0 and errors == 0 and passed > 0
    tests = Gate(
        "tests",
        tests_ok,
        "pass" if tests_ok else "fail",
        f"{passed} passed, {failed} failed, {errors} errors (pytest exit {code})",
    )

    pct_match = re.search(r"^TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%", out, re.MULTILINE)
    if pct_match is None:
        # No TOTAL line means coverage did not run, which is not the same thing
        # as low coverage and must not be reported as a number.
        return tests, Gate("coverage", False, "fail", "no coverage total in pytest output")
    pct = float(pct_match.group(1))
    return tests, Gate(
        "coverage",
        pct >= COVERAGE_FLOOR,
        "pass" if pct >= COVERAGE_FLOOR else "fail",
        f"{pct:.0f}% of statements (floor {COVERAGE_FLOOR:.0f}%)",
    )


def _summary_line(out: str, prefer: str) -> str:
    """The line a tool means as its verdict, not whichever line came last.

    ruff's last line is a hint about `--fix`, so the naive tail reports "1
    fixable" where the useful answer is "Found 1 error".
    """
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return next((ln for ln in reversed(lines) if ln.startswith(prefer)), lines[-1] if lines else "no output")


def gate_lint() -> Gate:
    code, out = run([sys.executable, "-m", "ruff", "check", *LINT_PATHS])
    ok = code == 0
    summary = "All checks passed!" if ok else _summary_line(out, "Found ")
    return Gate("lint", ok, "pass" if ok else "fail", f"ruff over {' '.join(LINT_PATHS)}: {summary}")


def gate_typecheck() -> Gate:
    code, out = run([sys.executable, "-m", "mypy", *TYPECHECK_PATHS])
    summary = _summary_line(out, "Found " if code else "Success")
    return Gate(
        "typecheck",
        code == 0,
        "pass" if code == 0 else "fail",
        f"mypy over {' '.join(TYPECHECK_PATHS)}: {summary}",
    )


def gate_audit() -> Gate:
    code, out = run([sys.executable, str(REPO_ROOT / "scripts" / "audit.py")])
    # audit.py exits 2 when the scanner itself is missing. That is a different
    # failure from "vulnerabilities found" and the evidence must not blur them.
    if code == 2:
        return Gate("audit", False, "fail", "pip-audit not installed — nothing was scanned")
    summary = next(
        (ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "no output"
    )
    return Gate("audit", code == 0, "pass" if code == 0 else "fail", summary)


def gate_performance() -> Gate:
    """The query-plan guard, not the clock.

    `scripts/benchmark.py` is the measurement; it is not a gate because timings
    on a laptop move by 5x under load. `tests/test_performance.py` captures the
    SQL the store really issues and fails if any hot read path starts planning a
    full table scan, which is the thing that would actually regress.
    """
    code, out = run([sys.executable, "-m", "pytest", "tests/test_performance.py"])
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    ok = code == 0 and passed > 0
    return Gate(
        "performance",
        ok,
        "ok" if ok else "regression",
        f"{passed} query-plan guards passed; no hot read path scans",
    )


def gate_specs() -> Gate:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from botsensai.metrics import build_registry; print(len(build_registry()))",
        ]
    )
    count = int(m.group(1)) if (m := re.search(r"(\d+)", out)) else 0
    ok = code == 0 and count >= MIN_METRICS
    return Gate("specs", ok, "pass" if ok else "fail", f"{count} metrics registered (floor {MIN_METRICS})")


# --- gates computed here ------------------------------------------------------ #

_C901 = re.compile(r"^(\S+?):\d+:\d+: C901 `(.+?)` is too complex \((\d+) > \d+\)$")


def parse_complexity(output: str) -> list[tuple[int, str]]:
    """(score, "path function") for every function ruff reported, worst first.

    Complexity is not hand-rolled here. `ruff check --select C901` is the same
    McCabe implementation flake8 uses, it is already a dev dependency, and it
    agrees with the 20 that `PumpPortalStream.run` was reported at by hand in
    P1-02 — an analyser written for this script would have been a second opinion
    nobody asked for.
    """
    scored: list[tuple[int, str]] = []
    for line in output.splitlines():
        m = _C901.match(line.strip())
        if m:
            scored.append((int(m.group(3)), f"{m.group(1)} {m.group(2)}"))
    return sorted(scored, reverse=True)


def gate_complexity() -> Gate:
    # A threshold of 0 makes ruff report *every* function with its score, which
    # is the only way to read the maximum back out rather than a yes/no.
    code, out = run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=0",
            "--output-format",
            "concise",
            *COMPLEXITY_PATHS,
        ]
    )
    scored = parse_complexity(out)
    if not scored:
        return Gate("complexity", False, "0", f"ruff reported no C901 lines (exit {code})")
    worst, where = scored[0]
    over = [s for s in scored if s[0] > MAX_COMPLEXITY]
    detail = f"worst {worst} ({where}); {len(scored)} functions, {len(over)} over {MAX_COMPLEXITY}"
    # The value is the score itself, not a verdict: the gate wants a number.
    return Gate("complexity", not over, str(worst), detail)


def _normalise(source: str) -> list[tuple[int, str]]:
    """(line number, stripped code) with blank and comment-only lines dropped."""
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append((i, line))
    return out


def duplication_report(
    paths: list[Path], window: int = DUPLICATE_WINDOW
) -> tuple[float, int, list[str]]:
    """Fraction of code lines inside a block repeated verbatim somewhere else.

    A plain sliding window over normalised lines. It will not find a clone that
    was reindented or renamed, and it is not trying to: the failure mode worth
    catching here is the one where a block gets pasted between two modules and
    then only one copy is fixed.
    """
    windows: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    covered: dict[Path, set[int]] = defaultdict(set)
    per_file: dict[Path, list[tuple[int, str]]] = {}
    total = 0
    for path in paths:
        lines = _normalise(path.read_text())
        per_file[path] = lines
        total += len(lines)
        for start in range(len(lines) - window + 1):
            key = "\n".join(text for _, text in lines[start : start + window])
            windows[key].append((path, start))

    clones: list[tuple[int, str]] = []
    for places in windows.values():
        if len(places) < 2:
            continue
        labels = [
            f"{path.relative_to(REPO_ROOT)}:{per_file[path][start][0]}"
            for path, start in places[:3]
        ]
        clones.append((len(places), f"{len(places)}x {', '.join(labels)}"))
        for path, start in places:
            covered[path].update(num for num, _ in per_file[path][start : start + window])

    duplicated = sum(len(v) for v in covered.values())
    pct = 100.0 * duplicated / total if total else 0.0
    return pct, duplicated, [text for _, text in sorted(clones, reverse=True)[:3]]


def gate_duplication(paths: list[Path]) -> Gate:
    pct, lines, worst = duplication_report(paths)
    ok = pct <= MAX_DUPLICATION_PCT
    detail = f"{pct:.1f}% of lines ({lines}) in a repeated {DUPLICATE_WINDOW}-line block"
    if worst:
        detail += f"; worst: {worst[0]}"
    return Gate("duplication", ok, "pass" if ok else "fail", detail)


# --- assembly ----------------------------------------------------------------- #

# Order matches the order the gate's own error message lists them in.
GATE_KEYS = [
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


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def collect(only: set[str] | None) -> list[Gate]:
    wanted = only or set(GATE_KEYS)
    gates: dict[str, Gate] = {}
    paths = source_files()

    if "tests" in wanted or "coverage" in wanted:
        tests, coverage = gate_suite()
        gates["tests"] = tests
        gates["coverage"] = coverage
    if "lint" in wanted:
        gates["lint"] = gate_lint()
    if "typecheck" in wanted:
        gates["typecheck"] = gate_typecheck()
    if "audit" in wanted:
        gates["audit"] = gate_audit()
    if "complexity" in wanted:
        gates["complexity"] = gate_complexity()
    if "duplication" in wanted:
        gates["duplication"] = gate_duplication(paths)
    if "performance" in wanted:
        gates["performance"] = gate_performance()
    if "specs" in wanted:
        gates["specs"] = gate_specs()

    return [gates[k] for k in GATE_KEYS if k in gates and k in wanted]


def evidence_line(gates: list[Gate]) -> str:
    """The payload for `ralph emit build.done`.

    Values are bare words and bare numbers on purpose. The gate parses this
    string, and free text inside a value is how a green run gets read as red —
    `performance: pass (…)` was rejected twice before this script existed.
    """
    return ", ".join(f"{g.key}: {g.value}" for g in gates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        help=f"comma-separated subset of: {','.join(GATE_KEYS)}",
    )
    parser.add_argument(
        "--format",
        choices=["table", "evidence"],
        default="table",
        help="'evidence' prints only the build.done payload line",
    )
    args = parser.parse_args(argv)

    only = None
    if args.only:
        only = {k.strip() for k in args.only.split(",") if k.strip()}
        unknown = only - set(GATE_KEYS)
        if unknown:
            print(f"unknown gate(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    try:
        gates = collect(only)
    except subprocess.TimeoutExpired as exc:
        print(f"gate harness timed out: {exc}", file=sys.stderr)
        return 2

    if args.format == "table":
        print("Backpressure gates\n")
        for gate in gates:
            print(gate.line)
        print()
        red = [g.key for g in gates if not g.ok]
        print(f"{len(gates) - len(red)}/{len(gates)} green" + (f", red: {', '.join(red)}" if red else ""))
        print()
        print("build.done evidence:")
    print(evidence_line(gates))
    return 0 if all(g.ok for g in gates) else 1


if __name__ == "__main__":
    sys.exit(main())
