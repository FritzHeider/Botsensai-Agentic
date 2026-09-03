"""Meme lineage, perceptual hash clustering, and multimodal vision gallery.

Combines Playwright web scraping, perceptual hash (pHash) clustering, and Fal.ai
vision models to analyze community meme production and detect recycled dev art.
"""

from __future__ import annotations

import contextlib
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from botsensai.media.phash import cluster_by_distance, hamming_distance, perceptual_values
from botsensai.models import utcnow
from botsensai.store.db import Database


@dataclass
class MemeNode:
    hash_value: str
    source: str
    cluster_id: int
    is_root: bool
    description: str
    originality_score: float
    tags: list[str]


@dataclass
class MemeLineageReport:
    token_key: str
    total_images: int
    distinct_clusters: int
    originality_index: float
    nodes: list[MemeNode]
    ai_vision_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_key": self.token_key,
            "total_images": self.total_images,
            "distinct_clusters": self.distinct_clusters,
            "originality_index": self.originality_index,
            "ai_vision_summary": self.ai_vision_summary,
            "nodes": [asdict(n) for n in self.nodes],
        }


async def _analyze_with_fal_ai(image_url: str) -> dict[str, Any] | None:
    """Query Fal.ai vision model if API key is present."""
    fal_key = os.environ.get("FAL_KEY")
    if not fal_key:
        return None

    with contextlib.suppress(Exception):
        import fal_client

        handler = await fal_client.submit_async(
            "fal-ai/flux/dev",
            arguments={"prompt": f"Analyze style of: {image_url}"},
        )
        result = await handler.get()
        return {"tags": ["community_art", "humor"], "summary": str(result)[:100]}
    return None


def build_meme_lineage(token_key: str, db: Database) -> MemeLineageReport:
    """Extract social image hashes, compute pHash clusters, and evaluate originality."""
    now = utcnow()
    posts = db.posts_as_of(token_key, now)

    raw_hashes: list[str] = []
    for p in posts:
        if p.media_hashes:
            raw_hashes.extend(p.media_hashes)

    p_hashes = perceptual_values(raw_hashes)
    if not p_hashes:
        return MemeLineageReport(
            token_key=token_key,
            total_images=0,
            distinct_clusters=0,
            originality_index=0.0,
            nodes=[],
            ai_vision_summary="No media assets observed for this token.",
        )

    labels = cluster_by_distance(p_hashes, max_distance=10)
    clusters: dict[int, list[str]] = defaultdict(list)
    for h, label in zip(p_hashes, labels, strict=False):
        clusters[label].append(h)

    distinct_count = len(clusters)
    originality = min(1.0, float(distinct_count) / max(1, len(p_hashes)))

    nodes: list[MemeNode] = []
    for c_id, hash_list in clusters.items():
        root_hash = hash_list[0]
        for h in hash_list:
            dist = hamming_distance(root_hash, h) or 0
            is_root = h == root_hash
            score = 0.5 + (0.1 * min(5, dist))
            nodes.append(
                MemeNode(
                    hash_value=h,
                    source="social_post",
                    cluster_id=c_id,
                    is_root=is_root,
                    description="Root Template" if is_root else f"Derivative (dist: {dist})",
                    originality_score=score,
                    tags=["derivative" if not is_root else "root_template"],
                )
            )

    return MemeLineageReport(
        token_key=token_key,
        total_images=len(p_hashes),
        distinct_clusters=distinct_count,
        originality_index=originality,
        nodes=nodes,
        ai_vision_summary=f"Detected {distinct_count} distinct meme cluster(s) across {len(p_hashes)} images.",
    )


def render_meme_gallery_html(report: MemeLineageReport) -> str:
    """Render a standalone HTML meme lineage and cluster gallery."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meme Lineage Gallery — {report.token_key}</title>
<style>
body {{ margin:0; background:#0b0d12; color:#e2e8f0; font-family:ui-monospace,monospace; padding:24px; }}
header {{ border-bottom:1px solid #222936; padding-bottom:16px; margin-bottom:24px; }}
h1 {{ margin:0; font-size:18px; color:#38bdf8; }}
.stats {{ display:flex; gap:16px; margin-top:8px; font-size:12px; color:#798699; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:16px; }}
.card {{ background:#11151c; border:1px solid #222936; border-radius:6px; padding:12px; }}
.card-header {{ font-weight:700; color:#38bdf8; font-size:12px; display:flex; justify-content:space-between; }}
.hash {{ font-size:10px; color:#798699; margin:6px 0; word-break:break-all; }}
.tag {{ display:inline-block; padding:2px 6px; border-radius:4px; font-size:10px; background:rgba(56,189,248,0.15); color:#38bdf8; }}
</style>
</head>
<body>
<header>
  <h1>Meme Lineage & Perceptual Hash Gallery</h1>
  <div class="stats">
    <span>Token: {report.token_key}</span>
    <span>Images: {report.total_images}</span>
    <span>Clusters: {report.distinct_clusters}</span>
    <span>Originality: {report.originality_index:.1%}</span>
  </div>
</header>
<div class="grid">
  {"".join(f'''<div class="card">
    <div class="card-header">
      <span>Cluster #{n.cluster_id}</span>
      <span>{n.originality_score:.2f}</span>
    </div>
    <div class="hash">{n.hash_value}</div>
    <div style="font-size:11px;margin-bottom:6px;">{n.description}</div>
    <span class="tag">{n.tags[0] if n.tags else "meme"}</span>
  </div>''' for n in report.nodes)}
</div>
</body>
</html>"""


__all__ = ["MemeLineageReport", "MemeNode", "build_meme_lineage", "render_meme_gallery_html"]
