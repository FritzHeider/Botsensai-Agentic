"""Unit tests verifying research lab notebooks structure and validity."""

import json
from pathlib import Path


def test_research_notebooks_validity() -> None:
    notebook_dir = Path("notebooks")
    assert notebook_dir.exists()

    expected = [
        "01_adversarial_signals.ipynb",
        "02_walkforward_backtesting.ipynb",
        "03_topology_forensics.ipynb",
    ]

    for nb_name in expected:
        nb_path = notebook_dir / nb_name
        assert nb_path.exists(), f"Missing notebook: {nb_name}"
        data = json.loads(nb_path.read_text(encoding="utf-8"))
        assert data.get("nbformat") == 4
        assert len(data.get("cells", [])) >= 1
