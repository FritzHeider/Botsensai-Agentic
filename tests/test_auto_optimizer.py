import json
import pytest
from pathlib import Path
from scripts.auto_optimizer import load_state, save_state, STATE_FILE


def test_optimizer_state_roundtrip(tmp_path, monkeypatch):
    test_state_file = tmp_path / "test_optimizer_state.json"
    monkeypatch.setattr("scripts.auto_optimizer.STATE_FILE", test_state_file)

    initial_state = load_state()
    assert initial_state["last_fitted_samples"] == 0
    assert initial_state["holdout_rank_correlation"] == 0.0

    updated = {
        "last_fitted_samples": 450,
        "last_fitted_at": "2026-09-20T07:00:00Z",
        "holdout_rank_correlation": 0.285,
        "top_decile_lift": 2.14,
        "version": "v20260920_test",
    }
    save_state(updated)

    loaded = load_state()
    assert loaded["last_fitted_samples"] == 450
    assert loaded["holdout_rank_correlation"] == 0.285
    assert loaded["version"] == "v20260920_test"
