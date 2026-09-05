"""Unit tests for interactive strategy sandbox and parameter sensitivity explorer."""


from botsensai.config import Settings
from botsensai.sandbox import render_sensitivity_table, run_parameter_sensitivity
from botsensai.store.db import Database


def test_run_parameter_sensitivity(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    settings = Settings(db_path=str(tmp_path / "test.db"))

    rows = run_parameter_sensitivity(settings, db, param_name="entry_threshold")
    assert len(rows) == 7
    assert rows[0].parameter_name == "entry_threshold"
    assert any(r.variant_pct == 0.0 for r in rows)

    # Render smoke test
    render_sensitivity_table(rows)
    db.close()
