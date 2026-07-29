"""One-command setup for the X browser profile.

The manual path is four steps — make a burner, create a profile directory,
launch Chrome against it and log in, then edit three config values — and every
one of them is a place to get something subtly wrong. The most common failure is
leaving Chrome running, which holds a lock on the profile directory that
Playwright cannot acquire, producing an error that does not obviously point at
the cause.

So this collapses the whole thing into `botsensai x-setup`, which creates the
directory, launches the right Chrome binary against it, waits while you log in,
verifies the session actually took, and only then writes the config.

Two things it deliberately will not do:

* **It never touches a credential.** Chrome handles the login in a window you
  drive. This process waits, then checks a page for a logged-in marker. There is
  no field, no keystroke injection and no cookie handling anywhere in it.
* **It will not set `acknowledged_burner` for you** without you typing the
  confirmation. That flag means "I accept this account may be suspended", which
  is a decision rather than a configuration step, and defaulting it to yes would
  make it meaningless.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from botsensai.config import Settings
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: Where Chrome usually lives, by platform. Checked in order.
CHROME_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
    "Linux": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ),
    "Windows": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
}

DEFAULT_PROFILE_DIRNAME = "botsensai-x-profile"

#: Typed exactly, to set `acknowledged_burner`. Deliberately not "y".
BURNER_CONFIRMATION = "burner"


@dataclass
class SetupResult:
    profile_dir: Path
    chrome_path: str | None
    created_dir: bool
    launched: bool
    message: str = ""


def default_profile_dir() -> Path:
    return Path.home() / DEFAULT_PROFILE_DIRNAME


def find_chrome(explicit: str | None = None) -> str | None:
    """Locate a Chromium-family browser binary.

    Falls back to whatever is on PATH, because a Homebrew or Flatpak install
    will not be at any of the canonical locations.
    """
    if explicit:
        return explicit if Path(explicit).exists() else None

    for candidate in CHROME_CANDIDATES.get(platform.system(), ()):
        if Path(candidate).exists():
            return candidate

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def profile_is_locked(profile_dir: Path) -> bool:
    """True when Chrome appears to still hold the profile.

    This is the single most common setup failure and produces an unhelpful
    error deep inside Playwright, so it is worth detecting up front and naming.
    """
    for marker in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        if (profile_dir / marker).exists():
            return True
    return False


def prepare_profile(profile_dir: Path) -> tuple[bool, str]:
    """Create the profile directory, refusing unsafe locations."""
    profile_dir = profile_dir.expanduser()

    # A profile holds live session cookies. Inside a repository it is one
    # `git add -A` away from being published, so refuse outright rather than
    # relying on .gitignore to catch it.
    for parent in [profile_dir, *profile_dir.parents]:
        if (parent / ".git").exists():
            return False, (
                f"refusing to create a browser profile inside a git repository "
                f"({parent}). A profile holds live session cookies; put it in your "
                f"home directory instead, e.g. {default_profile_dir()}"
            )

    if profile_dir.exists():
        return False, f"using existing profile at {profile_dir}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return True, f"created {profile_dir}"


def launch_chrome_for_login(
    chrome_path: str, profile_dir: Path, url: str = "https://x.com/login"
) -> subprocess.Popen | None:
    """Open Chrome against the profile so the operator can log in by hand.

    Detached on purpose: this process waits on the operator, not on Chrome, and
    the operator has to close Chrome before the profile can be read.
    """
    try:
        return subprocess.Popen(  # noqa: S603 — arguments are constructed, not user input
            [
                chrome_path,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        log.warning("x_setup.launch_failed", error=str(exc))
        return None


def manual_launch_command(chrome_path: str | None, profile_dir: Path) -> str:
    binary = chrome_path or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if " " in binary:
        binary = binary.replace(" ", r"\ ")
    return f'{binary} --user-data-dir="{profile_dir}" https://x.com/login'


# --------------------------------------------------------------------------- #
# config patching
# --------------------------------------------------------------------------- #


def patch_config(
    config_path: Path, profile_dir: Path, acknowledged_burner: bool
) -> tuple[bool, str]:
    """Write the three settings into the YAML, preserving comments.

    Done as a targeted textual patch rather than a load-and-dump round trip,
    because the config file's comments carry the operational warnings and
    round-tripping through PyYAML would silently delete every one of them.
    """
    if not config_path.exists():
        return False, f"config not found at {config_path}"

    original = config_path.read_text(encoding="utf-8")
    patched = original

    patched, user_data_ok = _replace_scalar(
        patched, "user_data_dir", f'"{profile_dir}"', section="browser"
    )
    patched, enabled_ok = _replace_scalar(patched, "enabled", "true", section="x_session")
    ack_ok = True
    if acknowledged_burner:
        patched, ack_ok = _replace_scalar(
            patched, "acknowledged_burner", "true", section="x_session"
        )

    if not (user_data_ok and enabled_ok and ack_ok):
        # Rather than half-patching, append an explicit override block. YAML
        # takes the last definition, and a visible block is easier to audit than
        # a silent partial edit.
        patched = original.rstrip() + (
            "\n\n# --- written by `botsensai x-setup` ---\n"
            "browser:\n"
            f'  user_data_dir: "{profile_dir}"\n'
            "x_session:\n"
            "  enabled: true\n"
            f"  acknowledged_burner: {'true' if acknowledged_burner else 'false'}\n"
        )

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    backup.write_text(original, encoding="utf-8")
    config_path.write_text(patched, encoding="utf-8")
    return True, f"updated {config_path} (previous version saved to {backup.name})"


def _replace_scalar(text: str, key: str, value: str, section: str) -> tuple[str, bool]:
    """Replace `key: ...` inside a top-level `section:` block.

    Scoped to the section so that a key name appearing in more than one block —
    `enabled` appears in several — is not rewritten in the wrong place.
    """
    section_re = re.compile(rf"^{re.escape(section)}:\s*$", re.MULTILINE)
    match = section_re.search(text)
    if not match:
        return text, False

    start = match.end()
    next_top_level = re.compile(r"^\S", re.MULTILINE)
    following = next_top_level.search(text, start + 1)
    end = following.start() if following else len(text)

    block = text[start:end]
    key_re = re.compile(rf"^(\s+){re.escape(key)}:[^\n]*$", re.MULTILINE)
    key_match = key_re.search(block)
    if not key_match:
        return text, False

    indent = key_match.group(1)
    new_block = key_re.sub(f"{indent}{key}: {value}", block, count=1)
    return text[:start] + new_block + text[end:], True


def config_path_for(settings: Settings, explicit: str | None = None) -> Path:
    from botsensai.config import DEFAULT_CONFIG_PATH

    return Path(explicit) if explicit else DEFAULT_CONFIG_PATH


__all__ = [
    "BURNER_CONFIRMATION",
    "CHROME_CANDIDATES",
    "DEFAULT_PROFILE_DIRNAME",
    "SetupResult",
    "config_path_for",
    "default_profile_dir",
    "find_chrome",
    "launch_chrome_for_login",
    "manual_launch_command",
    "patch_config",
    "prepare_profile",
    "profile_is_locked",
]
