"""Tests for data export module."""

from __future__ import annotations

import tempfile
from pathlib import Path

from botsensai.config import Settings
from botsensai.export import export_data
from botsensai.models import Launch, Launchpad, TokenRef, utcnow
from botsensai.store.db import Database


def test_export_launches_to_csv_and_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        settings = Settings(db_path=str(db_path))

        db = Database(db_path)
        token = TokenRef(mint="mint123", name="Test", symbol="TST")
        launch = Launch(token=token, launchpad=Launchpad.PUMPFUN, created_at=utcnow())
        db.upsert_launch(launch)
        db.close()

        csv_out = Path(tmpdir) / "launches.csv"
        res_csv = export_data(settings, "launches", output_format="csv", out_file=csv_out)
        assert res_csv.exists()
        content = res_csv.read_text()
        assert "mint123" in content

        json_out = Path(tmpdir) / "launches.json"
        res_json = export_data(settings, "launches", output_format="json", out_file=json_out)
        assert res_json.exists()
        json_content = json_out.read_text()
        assert "mint123" in json_content
