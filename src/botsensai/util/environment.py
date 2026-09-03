"""Virtual environment detection, inspection, and automatic lifecycle management."""

from __future__ import annotations

import os
import sys
import venv
from pathlib import Path
from typing import Any

from botsensai.config import REPO_ROOT


def is_in_virtualenv() -> bool:
    """Return True if currently executing inside a virtualenv or conda environment."""
    return (
        (sys.prefix != sys.base_prefix)
        or bool(os.environ.get("VIRTUAL_ENV"))
        or bool(os.environ.get("CONDA_PREFIX"))
    )


def get_virtualenv_info() -> dict[str, Any]:
    """Inspect current Python interpreter and virtual environment status."""
    in_venv = is_in_virtualenv()
    env_type = "global"
    if os.environ.get("CONDA_PREFIX"):
        env_type = "conda"
    elif in_venv:
        env_type = "virtualenv"

    local_venv = REPO_ROOT / ".venv"
    return {
        "is_virtual": in_venv,
        "env_type": env_type,
        "current_prefix": sys.prefix,
        "executable": sys.executable,
        "local_venv_exists": local_venv.exists(),
        "local_venv_path": str(local_venv),
    }


def auto_reexec_in_virtualenv() -> bool:
    """Seamlessly re-execute current CLI command inside local .venv if available."""
    if is_in_virtualenv():
        return False

    if os.environ.get("BOTSENSAI_NO_REEXEC") == "1":
        return False

    local_venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if not local_venv_py.exists():
        # Windows fallback check
        local_venv_py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

    if local_venv_py.exists() and os.access(str(local_venv_py), os.X_OK):
        os.environ["BOTSENSAI_NO_REEXEC"] = "1"
        os.execv(str(local_venv_py), [str(local_venv_py), *sys.argv])
        return True

    return False


def create_local_virtualenv(dest_path: Path | None = None) -> Path:
    """Create a new standard library virtualenv in the repository root."""
    target = dest_path or (REPO_ROOT / ".venv")
    if target.exists():
        return target

    venv.create(target, with_pip=True, clear=False)
    return target


__all__ = [
    "auto_reexec_in_virtualenv",
    "create_local_virtualenv",
    "get_virtualenv_info",
    "is_in_virtualenv",
]
