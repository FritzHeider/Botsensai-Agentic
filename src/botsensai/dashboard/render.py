"""Snapshot -> self-contained HTML.

All CSS is inlined in the template and there are no external references: P5-01
requires the artifact to be one file with no build step and no network fetch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _environment() -> Environment:
    """Jinja environment with escaping on unconditionally.

    `autoescape=True`, not `select_autoescape(["html"])`: that helper matches on
    the filename suffix, and `dashboard.html.j2` ends in `.j2`, so it would
    return False and escaping would be silently off. Nearly every string on this
    page comes from the store and is attacker-supplied in practice — a post
    author, a token symbol, a collector error — so unescaped output would let a
    memecoin promoter inject markup into the operator's page, including a remote
    `<img>` that defeats the no-external-assets rule the artifact is built for.
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(snapshot: dict[str, Any]) -> str:
    """Render one snapshot to a complete HTML document."""
    template = _environment().get_template("dashboard.html.j2")
    return template.render(snap=snapshot)


__all__ = ["render_html"]
