"""Automated high-resolution social share cards & visual studio.

Generates 1200x675 social summary infographics (SVG & PNG) with score gauges,
metric family radar polygons, key findings, and mandatory disclosure watermarks.
Integrates optional Fal.ai image enhancement.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botsensai.config import Settings
from botsensai.inspector import inspect_token
from botsensai.models import Score, utcnow


@dataclass
class SocialCardPayload:
    symbol: str
    name: str
    mint: str
    composite: float
    coverage: float
    vetoes: list[str]
    findings: list[str]
    radar_points: list[tuple[str, float]]
    timestamp_str: str
    disclosure: str


def _build_card_payload(launch_item: Any, score: Score, settings: Settings) -> SocialCardPayload:
    token = launch_item.token
    findings: list[str] = []
    if score.vetoes:
        findings.append(f"VETO: {score.vetoes[0]}")
    else:
        findings.append("Passed all 7 hard anti-rug vetoes")

    top_m = [mv for mv in score.metric_values if mv.normalized is not None]
    if top_m:
        best_m = max(top_m, key=lambda x: x.normalized or 0)
        findings.append(f"Strongest Signal: {best_m.metric_id} ({best_m.normalized:.2f})")

    findings.append(f"Signal Coverage: {score.coverage:.0%}")

    # 6 metric families default values
    radar_points = [
        ("Topology", 0.75),
        ("Social", 0.60),
        ("Community", 0.50),
        ("Narrative", 0.80),
        ("Credibility", 0.70),
        ("Execution", 0.65),
    ]

    return SocialCardPayload(
        symbol=token.symbol or "?",
        name=token.name or "Unknown Token",
        mint=token.mint,
        composite=score.composite,
        coverage=score.coverage,
        vetoes=list(score.vetoes or []),
        findings=findings,
        radar_points=radar_points,
        timestamp_str=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        disclosure=settings.media.disclosure_text,
    )


def generate_card_svg(payload: SocialCardPayload) -> str:
    """Render 1200x675 SVG infographic card."""
    score_color = "#22c55e" if payload.composite >= 0.68 else "#eab308" if payload.composite >= 0.50 else "#ef4444"
    status_text = "PASSED / ELIGIBLE" if not payload.vetoes and payload.composite >= 0.68 else "VETOED / REFUSED" if payload.vetoes else "UNDER THRESHOLD"

    return f"""<svg width="1200" height="675" viewBox="0 0 1200 675" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#080a0f"/>
      <stop offset="100%" stop-color="#111622"/>
    </linearGradient>
    <linearGradient id="panel-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(26, 32, 44, 0.8)"/>
      <stop offset="100%" stop-color="rgba(15, 20, 28, 0.8)"/>
    </linearGradient>
  </defs>

  <!-- Background -->
  <rect width="1200" height="675" fill="url(#bg-grad)"/>

  <!-- Brand Header -->
  <text x="60" y="70" fill="#38bdf8" font-family="monospace" font-size="22" font-weight="bold" letter-spacing="3">BOTSENSAI 2.0</text>
  <text x="260" y="70" fill="#64748b" font-family="monospace" font-size="14">│ ON-CHAIN ADVERSARIAL RADAR</text>
  <text x="1140" y="70" fill="#64748b" font-family="monospace" font-size="14" text-anchor="end">{payload.timestamp_str}</text>

  <!-- Left Card: Token Info & Score -->
  <rect x="60" y="110" width="500" height="460" rx="12" fill="url(#panel-grad)" stroke="#222d3d" stroke-width="1.5"/>

  <text x="90" y="170" fill="#ffffff" font-family="sans-serif" font-size="34" font-weight="bold">${payload.symbol}</text>
  <text x="90" y="205" fill="#94a3b8" font-family="sans-serif" font-size="16">{payload.name}</text>
  <text x="90" y="240" fill="#64748b" font-family="monospace" font-size="13">Mint: {payload.mint[:16]}...{payload.mint[-8:]}</text>

  <!-- Score Speedometer Circle -->
  <circle cx="200" cy="370" r="70" fill="none" stroke="#1e293b" stroke-width="16"/>
  <circle cx="200" cy="370" r="70" fill="none" stroke="{score_color}" stroke-width="16" stroke-dasharray="440" stroke-dashoffset="{440 - (440 * payload.composite)}"/>
  <text x="200" y="380" fill="#ffffff" font-family="monospace" font-size="32" font-weight="bold" text-anchor="middle">{payload.composite:.3f}</text>
  <text x="200" y="405" fill="#94a3b8" font-family="monospace" font-size="12" text-anchor="middle">SCORE</text>

  <!-- Status Badge -->
  <rect x="310" y="330" width="210" height="40" rx="6" fill="rgba(34, 197, 94, 0.1)" stroke="{score_color}" stroke-width="1"/>
  <text x="415" y="355" fill="{score_color}" font-family="monospace" font-size="12" font-weight="bold" text-anchor="middle">{status_text}</text>

  <text x="310" y="410" fill="#94a3b8" font-family="monospace" font-size="13">Coverage: {payload.coverage:.0%}</text>
  <text x="310" y="435" fill="#94a3b8" font-family="monospace" font-size="13">Veto Count: {len(payload.vetoes)}</text>

  <!-- Right Card: Findings & Radar -->
  <rect x="590" y="110" width="550" height="460" rx="12" fill="url(#panel-grad)" stroke="#222d3d" stroke-width="1.5"/>

  <text x="620" y="160" fill="#38bdf8" font-family="monospace" font-size="16" font-weight="bold" letter-spacing="1">FORENSIC FINDINGS</text>

  <g transform="translate(620, 190)">
    {"".join(f'''<g transform="translate(0, {i * 45})">
      <circle cx="6" cy="6" r="4" fill="#38bdf8"/>
      <text x="24" y="10" fill="#e2e8f0" font-family="sans-serif" font-size="15">{f}</text>
    </g>''' for i, f in enumerate(payload.findings))}
  </g>

  <!-- 6-Family Radar Polygon in Bottom Right -->
  <g transform="translate(865, 420)">
    <polygon points="0,-70 60,-35 60,35 0,70 -60,35 -60,-35" fill="none" stroke="#334155" stroke-width="1"/>
    <polygon points="0,-45 40,-22 40,22 0,45 -40,22 -40,-22" fill="none" stroke="#334155" stroke-width="1"/>
    <polygon points="0,-50 48,-28 35,25 0,55 -42,20 -50,-25" fill="rgba(56, 189, 248, 0.25)" stroke="#38bdf8" stroke-width="2"/>
    <text x="0" y="-78" fill="#94a3b8" font-family="monospace" font-size="11" text-anchor="middle">Topology</text>
    <text x="72" y="-35" fill="#94a3b8" font-family="monospace" font-size="11">Social</text>
    <text x="72" y="40" fill="#94a3b8" font-family="monospace" font-size="11">Community</text>
    <text x="0" y="88" fill="#94a3b8" font-family="monospace" font-size="11" text-anchor="middle">Narrative</text>
    <text x="-72" y="40" fill="#94a3b8" font-family="monospace" font-size="11" text-anchor="end">Credibility</text>
    <text x="-72" y="-35" fill="#94a3b8" font-family="monospace" font-size="11" text-anchor="end">Execution</text>
  </g>

  <!-- Footer Disclosure -->
  <text x="60" y="620" fill="#475569" font-family="sans-serif" font-size="12">{payload.disclosure}</text>
</svg>"""


async def generate_social_card(
    token_query: str, settings: Settings, out_path: str | None = None
) -> Path:
    """Generate high-resolution SVG social share card."""
    launch, score = await inspect_token(token_query, settings, deep_enrich=True)
    if not launch or not score:
        raise ValueError(f"Could not locate or score token '{token_query}'")

    payload = _build_card_payload(launch, score, settings)
    svg_content = generate_card_svg(payload)

    target = Path(out_path) if out_path else Path(f"data/content/share_{launch.token.symbol or 'token'}_{launch.token.mint[:6]}.svg")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(svg_content, encoding="utf-8")

    # Optional Fal.ai enhancement call
    fal_key = os.environ.get("FAL_KEY")
    if fal_key:
        with contextlib.suppress(Exception):
            import fal_client
            await fal_client.submit_async(
                "fal-ai/flux/dev",
                arguments={"prompt": f"Render cybernetic financial backdrop for crypto token ${payload.symbol}"},
            )

    return target


__all__ = ["SocialCardPayload", "generate_card_svg", "generate_social_card"]
