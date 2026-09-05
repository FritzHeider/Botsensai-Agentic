#!/usr/bin/env python
"""Measure synthetic metric coverage and record it in `docs/RESULTS.md`.

`tests/test_coverage_gate.py` reads the block this writes and fails when
coverage falls below it. So re-running this script is how you *accept* a
coverage change — the diff it produces is the thing a reviewer should look at,
and a drop that arrives with no explanation in the same commit is the whole
failure mode the gate exists to make visible.

    python scripts/coverage_baseline.py            # show what would change
    python scripts/coverage_baseline.py --write    # record it

Exit code is 0 when the recorded baseline already matches, 1 when it does not
(so a CI job can run it without --write and see drift), 2 on a bad doc.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO_ROOT / "docs" / "RESULTS.md"

sys.path.insert(0, str(REPO_ROOT / "src"))

from botsensai.backtest.coverage import (  # noqa: E402
    measure_synthetic_coverage,
    parse_block,
    render_block,
    write_block,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Record the measurement.")
    args = parser.parse_args()

    measured = measure_synthetic_coverage()
    print(
        f"universe {measured.params['universe']}, "
        f"{measured.params['evaluated']} evaluations, "
        f"{len(measured.participating)}/{len(measured.coverage)} metrics participating, "
        f"mean {measured.mean_coverage:.4f}"
    )
    for metric_id, reason in measured.absent.items():
        print(f"  absent: {metric_id} — {reason}")

    try:
        recorded = parse_block(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read the recorded baseline: {exc}", file=sys.stderr)
        if not args.write:
            return 2
        recorded = None

    if recorded is not None:
        drift = _report_drift(recorded, measured)
        if not drift and not args.write:
            print("recorded baseline matches.")
            return 0

    if args.write:
        write_block(RESULTS_PATH, measured)
        print(f"wrote {RESULTS_PATH}")
        return 0

    print(f"\nrun with --write to record. Preview:\n\n{render_block(measured)}")
    return 1


def _report_drift(recorded: object, measured: object) -> bool:
    """Print per-metric differences. Returns True when anything moved."""
    rec_cov = recorded.coverage  # type: ignore[attr-defined]
    new_cov = measured.coverage  # type: ignore[attr-defined]
    moved = False
    for metric_id in sorted(set(rec_cov) | set(new_cov)):
        before = rec_cov.get(metric_id)
        after = new_cov.get(metric_id)
        if before is None or after is None or abs(before - after) > 1e-9:
            moved = True
            print(f"  {metric_id}: {before} -> {after}")
    return moved


if __name__ == "__main__":
    raise SystemExit(main())
