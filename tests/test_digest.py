"""Unit tests for market intelligence digest exporter."""

from botsensai.config import Settings
from botsensai.digest import build_digest_data, export_market_digest, render_digest_markdown
from botsensai.store.db import Database


def test_build_and_export_digest(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    settings = Settings(db_path=str(tmp_path / "test.db"))

    data = build_digest_data(settings, db)
    assert data.total_launches >= 0
    assert len(data.veto_highlights) >= 1

    md = render_digest_markdown(data)
    assert "# Botsensai 2.0 Market Intelligence" in md
    assert "Executive Summary" in md

    out_file = tmp_path / "test_digest.md"
    exported_path = export_market_digest(settings, out_path=str(out_file))
    assert exported_path.exists()
    assert "# Botsensai" in exported_path.read_text(encoding="utf-8")

    db.close()
