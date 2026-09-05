#!/usr/bin/env python
"""Audit the packages Botsensai actually depends on, and nothing else.

Running bare `pip-audit` here audits the whole interpreter. On a conda base
environment that is ~200 packages — conda, pip, setuptools, jupyter, whatever
else the machine happens to carry — and it reports vulnerabilities this
repository has no ability to fix and no exposure to. The signal drowns: the
first run of it found 26 findings, of which 5 were ours.

So this script derives the roots from `pyproject.toml` (runtime dependencies
plus every optional extra that is actually installed), walks the installed
metadata to the full transitive closure, pins each package at its installed
version, and audits exactly that set. What it reports is what a user who ran
`pip install -e ".[dev]"` would be exposed to.

Optional extras that are not installed are skipped and named in the output,
because "clean" and "clean, but we never looked at playwright" are different
claims.

    python scripts/audit.py            # table, exit 1 if anything is found
    python scripts/audit.py --format json

Exit code is pip-audit's own: 0 clean, 1 vulnerabilities found.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent

# The scanner is not part of what this repository ships or tests, and auditing
# it audits the wrong thing: pip-audit depends on pip-api, which depends on
# `pip`, so leaving it in makes the interpreter's own pip a "botsensai
# dependency" and reports vulnerabilities in the installer. Excluded by name
# rather than silently, and the exclusion is printed on every run.
SCANNER_ROOTS = {"pip-audit"}


def declared_roots(pyproject: Path) -> tuple[list[str], list[str]]:
    """Root requirement names from pyproject, and the extras that supplied them.

    Extras are included only when installed. An extra nobody has installed
    cannot be audited from local metadata, and guessing its closure from the
    version specifiers would report on versions that are not the ones a user
    would get.
    """
    data = tomllib.loads(pyproject.read_text())
    project = data.get("project", {})

    roots: list[str] = [Requirement(r).name for r in project.get("dependencies", [])]
    skipped: list[str] = []

    for extra, requirements in project.get("optional-dependencies", {}).items():
        names = [
            name
            for name in (Requirement(r).name for r in requirements)
            if _canonical(name) not in SCANNER_ROOTS
        ]
        if all(_installed(n) for n in names):
            roots.extend(names)
        else:
            skipped.append(extra)

    return roots, skipped


def _installed(name: str) -> bool:
    try:
        md.distribution(name)
    except md.PackageNotFoundError:
        return False
    return True


def closure(roots: list[str]) -> tuple[dict[str, str], list[str]]:
    """Transitive closure of `roots` over installed metadata, pinned to versions.

    Extra-gated requirements (`foo[bar]`) and environment-gated ones are
    followed only when their marker evaluates true for this interpreter, so a
    Windows-only or docs-only dependency does not enter the audited set.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    pending = list(roots)

    while pending:
        name = pending.pop()
        key = _canonical(name)
        if key in resolved:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            if key not in missing:
                missing.append(key)
            continue
        resolved[key] = dist.version
        for raw in dist.requires or []:
            try:
                requirement = Requirement(raw)
            except Exception:
                continue
            if requirement.marker is not None:
                try:
                    if not requirement.marker.evaluate():
                        continue
                except Exception:
                    continue
            pending.append(requirement.name)

    return resolved, sorted(missing)


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", default="columns", help="pip-audit output format")
    args = parser.parse_args()

    # Check this before running anything: a missing pip_audit makes
    # `python -m pip_audit` exit 1, which is indistinguishable from "found
    # vulnerabilities" and would turn an absent scanner into a fake finding.
    if importlib.util.find_spec("pip_audit") is None:
        print("pip-audit is not installed. Install it with: pip install -e '.[dev]'")
        return 2

    roots, skipped_extras = declared_roots(REPO_ROOT / "pyproject.toml")
    packages, missing = closure(roots)

    print(f"auditing {len(packages)} packages in botsensai's dependency closure")
    print(f"  excluded as scanner tooling, not shipped or tested: {', '.join(sorted(SCANNER_ROOTS))}")
    if skipped_extras:
        print(f"  extras not installed, so not audited: {', '.join(sorted(skipped_extras))}")
    if missing:
        print(f"  declared but not installed, so not audited: {', '.join(missing)}")
    print()
    # pip-audit writes straight to the inherited fd; flush first or the header
    # lands underneath its findings.
    sys.stdout.flush()

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        for name, version in sorted(packages.items()):
            handle.write(f"{name}=={version}\n")
        requirements = handle.name

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--no-deps",
                "--progress-spinner",
                "off",
                "--format",
                args.format,
                "-r",
                requirements,
            ],
            check=False,
        )
    except FileNotFoundError:
        print("pip-audit is not installed. Install it with: pip install -e '.[dev]'")
        return 2
    finally:
        Path(requirements).unlink(missing_ok=True)

    if result.returncode == 2:
        print("\npip-audit could not run. Install it with: pip install -e '.[dev]'")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
