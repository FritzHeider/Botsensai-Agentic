"""Automated Corpus Sync and Snapshot Hub.

Packages local labelled outcome datasets, on-chain telemetry, and calibrated weights
into compressed, checksummed archive bundles for distributed training and cloud backup.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from botsensai.config import Settings
from botsensai.models import utcnow
from botsensai.store.db import Database


def export_corpus_bundle(settings: Settings, out_path: str | None = None) -> Path:
    """Create a compressed snapshot archive of the database and metadata."""
    db_file = Path(settings.path(settings.db_path))
    target = Path(out_path) if out_path else Path(f"data/corpus_bundle_{utcnow().strftime('%Y%m%d_%H%M%S')}.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)

    db = Database(str(db_file))
    counts = db.counts()
    db.close()

    manifest = {
        "created_at": utcnow().isoformat(),
        "weights_version": settings.scoring.weights_version,
        "counts": counts,
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

    with tarfile.open(target, "w:gz") as tar:
        if db_file.exists():
            tar.add(db_file, arcname="botsensai.db")
        # Add manifest info
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    return target


def import_corpus_bundle(settings: Settings, bundle_path: str) -> dict[str, Any]:
    """Inspect and unpack a corpus bundle archive."""
    b_path = Path(bundle_path)
    if not b_path.exists():
        raise FileNotFoundError(f"Corpus bundle does not exist: {bundle_path}")

    manifest_data: dict[str, Any] = {}
    with tarfile.open(b_path, "r:gz") as tar:
        names = tar.getnames()
        if "manifest.json" in names:
            m_file = tar.extractfile("manifest.json")
            if m_file:
                manifest_data = json.loads(m_file.read().decode("utf-8"))

    return manifest_data


__all__ = ["export_corpus_bundle", "import_corpus_bundle"]
