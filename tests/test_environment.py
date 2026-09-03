"""Unit tests for virtual environment inspection and lifecycle management."""

from pathlib import Path

from botsensai.util.environment import (
    auto_reexec_in_virtualenv,
    create_local_virtualenv,
    get_virtualenv_info,
    is_in_virtualenv,
)


def test_virtualenv_inspection() -> None:
    info = get_virtualenv_info()
    assert "is_virtual" in info
    assert "env_type" in info
    assert "current_prefix" in info
    assert "executable" in info
    assert "local_venv_exists" in info

    assert isinstance(is_in_virtualenv(), bool)

    # When already in a virtualenv or test runner, auto_reexec should return False
    assert auto_reexec_in_virtualenv() is False


def test_create_local_virtualenv_smoke(tmp_path: Path) -> None:
    custom_target = tmp_path / "custom_test_env"
    # If target doesn't exist, create_local_virtualenv creates directory
    result = create_local_virtualenv(custom_target)
    assert result == custom_target
    assert custom_target.exists()
