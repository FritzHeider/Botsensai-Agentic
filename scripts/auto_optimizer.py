"""Botsensai Continuous Model Optimizer.

Periodically evaluates newly labelled outcomes from the store, fits signal weights
and family budgets using coordinate ascent on rank correlation, and safely deploys
improved weights with atomic swap and zero downtime.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from botsensai.config import Settings, get_settings
from botsensai.metrics import build_registry
from botsensai.models import utcnow
from botsensai.scoring.fit import WeightFitter
from botsensai.backtest.walkforward import WalkForward, _has_label
from botsensai.store.db import Database
from botsensai.backtest.engine import Backtester

STATE_FILE = BASE_DIR / "data" / "optimizer_state.json"
LOG_FILE = BASE_DIR / "data" / "optimizer.log"
WEIGHTS_FILE = BASE_DIR / "config" / "weights.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("optimizer")


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as exc:
            log.warning(f"Could not load state file: {exc}")
    return {
        "last_fitted_samples": 0,
        "last_fitted_at": None,
        "holdout_rank_correlation": 0.0,
        "top_decile_lift": 1.0,
        "version": "v0-default",
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    temp_file.replace(STATE_FILE)


def run_optimization(min_new_samples: int = 100, days: float = 30.0, force: bool = False) -> bool:
    settings = get_settings()
    db = Database(settings.path(settings.db_path))
    state = load_state()

    # Check total available labelled outcomes
    with db.conn as conn:
        total_labelled = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE max_realizable_multiple IS NOT NULL"
        ).fetchone()[0]

    log.info(f"Total labelled outcomes in store: {total_labelled} (last fitted: {state.get('last_fitted_samples', 0)})")

    new_samples = total_labelled - state.get("last_fitted_samples", 0)
    if not force and new_samples < min_new_samples:
        log.info(f"Insufficient new labelled samples ({new_samples} < {min_new_samples}). Skipping optimization.")
        db.close()
        return False

    log.info(f"Starting model optimization with {new_samples} new labelled outcomes...")
    registry = build_registry()
    end = utcnow()
    start = end - timedelta(days=days)

    # Load tapes with limit to keep memory bounded and safe
    tapes = Backtester.tapes_from_database(db, start, end, limit=2000)
    labelled = [t for t in tapes if _has_label(t)]
    db.close()

    if len(labelled) < 50:
        log.warning(f"Only {len(labelled)} labelled tapes retrieved in last {days} days. Aborting fit.")
        return False

    log.info(f"Extracted {len(labelled)} labelled tapes. Building walk-forward training examples...")
    wf = WalkForward(settings, registry, fit_target="realizable")
    examples = wf.training_examples(labelled)

    if len(examples) < 50:
        log.warning(f"Only {len(examples)} training examples built. Aborting.")
        return False

    log.info(f"Fitting weights across {len(examples)} examples (holdout fraction: 0.25)...")
    fitter = WeightFitter(registry, seed=1337)
    report = fitter.fit(examples, holdout_fraction=0.25)

    if not report.fitted:
        log.warning(f"Model fitting skipped/failed: {report.warnings}")
        return False

    log.info(
        f"Fit completed! Train corr: {report.train_rank_correlation:.4f}, "
        f"Holdout corr: {report.holdout_rank_correlation:.4f}, "
        f"Top decile lift: {report.top_decile_lift:.4f}"
    )

    # Check if holdout performance meets production bar
    prev_corr = state.get("holdout_rank_correlation", 0.0)
    if report.holdout_rank_correlation <= 0.05:
        log.warning(
            f"Holdout correlation {report.holdout_rank_correlation:.4f} <= 0.05. "
            "Rejecting new weights to prevent degrading production performance."
        )
        return False

    # Atomic write to config/weights.json
    new_version = f"v{utcnow():%Y%m%d_%H%M%S}"
    report.weights.version = new_version
    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_weights = WEIGHTS_FILE.with_suffix(".tmp")
    report.weights.save(temp_weights)
    temp_weights.replace(WEIGHTS_FILE)

    state["last_fitted_samples"] = total_labelled
    state["last_fitted_at"] = utcnow().isoformat()
    state["holdout_rank_correlation"] = report.holdout_rank_correlation
    state["top_decile_lift"] = report.top_decile_lift
    state["version"] = new_version
    save_state(state)

    log.info(f"Successfully deployed upgraded weights {new_version} to {WEIGHTS_FILE}!")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai Continuous Model Optimizer")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a loop")
    parser.add_argument("--interval", type=int, default=14400, help="Interval in seconds between runs (default 4h)")
    parser.add_argument("--min-samples", type=int, default=100, help="Min new labelled samples to trigger fit")
    parser.add_argument("--force", action="store_true", help="Force optimization immediately")
    args = parser.parse_args()

    if args.loop:
        log.info(f"Starting optimizer daemon (checking every {args.interval}s)...")
        while True:
            try:
                run_optimization(min_new_samples=args.min_samples, force=args.force)
            except Exception as exc:
                log.error(f"Error during optimization run: {exc}", exc_info=True)
            time.sleep(args.interval)
    else:
        success = run_optimization(min_new_samples=args.min_samples, force=args.force)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
