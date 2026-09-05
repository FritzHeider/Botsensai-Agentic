"""Unit tests for automated corpus snapshot and sync hub."""

from botsensai.config import Settings
from botsensai.corpus_sync import export_corpus_bundle, import_corpus_bundle
from botsensai.store.db import Database


def test_corpus_bundle_roundtrip(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.close()

    settings = Settings(db_path=str(db_path))
    bundle_out = tmp_path / "test_corpus.tar.gz"

    archive_path = export_corpus_bundle(settings, out_path=str(bundle_out))
    assert archive_path.exists()

    manifest = import_corpus_bundle(settings, bundle_path=str(archive_path))
    assert "weights_version" in manifest
    assert "counts" in manifest
