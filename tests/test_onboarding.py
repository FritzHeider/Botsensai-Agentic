"""Tests for config validation, typo detection, and onboarding wizard."""

from __future__ import annotations

import tempfile
from pathlib import Path

from botsensai.config import (
    Settings,
    audit_capabilities,
    detect_unknown_yaml_keys,
)
from botsensai.onboarding import check_prerequisites, validate_config


def test_detect_unknown_yaml_keys_catches_typos():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(
            """
tradding_mode: paper
risk:
  max_pos: 0.5
  max_position_native: 0.25
"""
        )
        f_path = Path(f.name)

    try:
        unknown = detect_unknown_yaml_keys(f_path)
        assert "tradding_mode" in unknown
        assert "risk.max_pos" in unknown
        assert "risk.max_position_native" not in unknown
    finally:
        f_path.unlink()


def test_audit_capabilities():
    s = Settings()
    caps = audit_capabilities(s)
    assert "trading_mode" in caps
    assert caps["trading_mode"] == "paper"
    assert "helius_rpc" in caps
    assert "birdeye" in caps
    assert "notifications" in caps


def test_validate_config_valid():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(
            """
trading_mode: paper
risk:
  max_position_native: 0.25
"""
        )
        f_path = Path(f.name)

    try:
        assert validate_config(f_path) is True
    finally:
        f_path.unlink()


def test_check_prerequisites():
    prereqs = check_prerequisites()
    assert "python_version" in prereqs
    assert prereqs["python_version"] is True
