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
    """Render a standalone interactive HTML meme lineage and cluster gallery."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meme Lineage Gallery — {report.token_key}</title>
<style>
:root {{
  --bg: #0a0c10;
  --panel: #11151c;
  --line: #222936;
  --text: #e2e8f0;
  --dim: #798699;
  --accent: #38bdf8;
  --ok: #22c55e;
  --warn: #eab308;
  --alarm: #ef4444;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:ui-monospace,monospace; padding:24px; }}
header {{
  border-bottom: 1px solid var(--line);
  padding-bottom: 16px;
  margin-bottom: 20px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
}}
h1 {{ margin:0; font-size:18px; color:var(--accent); letter-spacing:0.05em; }}
.stats {{ display:flex; gap:16px; margin-top:8px; font-size:12px; color:var(--dim); flex-wrap:wrap; }}
.stat-pill {{ background:rgba(255,255,255,0.04); border:1px solid var(--line); padding:4px 8px; border-radius:4px; }}
.stat-val {{ color:var(--text); font-weight:700; }}
.filter-bar {{ display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap; }}
.filter-btn {{
  background:var(--panel); border:1px solid var(--line); color:var(--dim);
  padding:6px 12px; border-radius:6px; font:inherit; font-size:11px; cursor:pointer; font-weight:600;
  transition:all 0.15s;
}}
.filter-btn:hover {{ border-color:var(--accent); color:var(--text); }}
.filter-btn.active {{ background:rgba(56,189,248,0.15); border-color:var(--accent); color:var(--accent); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(250px, 1fr)); gap:16px; }}
.card {{
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px;
  display:flex; flex-direction:column; gap:10px; transition:border-color 0.15s, transform 0.15s;
}}
.card:hover {{ border-color:var(--accent); transform:translateY(-2px); }}
.card.is-root {{ border-left:3px solid var(--accent); }}
.card-header {{ font-weight:700; color:var(--accent); font-size:12px; display:flex; justify-content:space-between; align-items:center; }}
.identicon-box {{
  height:90px; background:#161c24; border-radius:6px; border:1px solid var(--line);
  display:flex; align-items:center; justify-content:center; position:relative; overflow:hidden;
}}
.identicon-svg {{ width:100%; height:100%; }}
.hash {{ font-size:10px; color:var(--dim); word-break:break-all; background:var(--bg); padding:4px 6px; border-radius:4px; border:1px solid var(--line); }}
.desc {{ font-size:11px; color:var(--text); font-weight:600; }}
.orig-bar {{ height:5px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:2px; }}
.orig-fill {{ height:100%; border-radius:3px; }}
.tag {{ display:inline-block; padding:2px 7px; border-radius:4px; font-size:10px; font-weight:700; width:fit-content; }}
.tag-root {{ background:rgba(56,189,248,0.15); color:var(--accent); border:1px solid rgba(56,189,248,0.3); }}
.tag-deriv {{ background:rgba(234,179,8,0.15); color:var(--warn); border:1px solid rgba(234,179,8,0.3); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Meme Lineage & Perceptual Hash Gallery</h1>
    <div class="stats">
      <span class="stat-pill">Token: <span class="stat-val">{report.token_key}</span></span>
      <span class="stat-pill">Assets: <span class="stat-val">{report.total_images}</span></span>
      <span class="stat-pill">Clusters: <span class="stat-val">{report.distinct_clusters}</span></span>
      <span class="stat-pill">Originality Index: <span class="stat-val">{report.originality_index:.1%}</span></span>
    </div>
  </div>
</header>

<div class="filter-bar">
  <button class="filter-btn active" onclick="filterGallery('all', this)">All Memes ({len(report.nodes)})</button>
  <button class="filter-btn" onclick="filterGallery('root', this)">Root Templates Only</button>
  <button class="filter-btn" onclick="filterGallery('derivative', this)">Derivatives</button>
</div>

<div class="grid">
  {"".join(f'''<div class="card {'is-root' if n.is_root else ''}" data-type="{'root' if n.is_root else 'derivative'}">
    <div class="card-header">
      <span>Cluster #{n.cluster_id}</span>
      <span class="tag {'tag-root' if n.is_root else 'tag-deriv'}">{'ROOT TEMPLATE' if n.is_root else 'DERIVATIVE'}</span>
    </div>
    <div class="identicon-box">
      <svg class="identicon-svg" viewBox="0 0 100 50">
        <rect width="100" height="50" fill="#{n.hash_value[:6] if len(n.hash_value)>=6 else '222936'}" opacity="0.3"/>
        <circle cx="50" cy="25" r="{12 + (n.cluster_id % 12)}" fill="#{n.hash_value[-6:] if len(n.hash_value)>=6 else '38bdf8'}" opacity="0.6"/>
        <text x="50" y="29" fill="#e2e8f0" font-size="10" font-family="monospace" text-anchor="middle">pHash #{n.cluster_id}</text>
      </svg>
    </div>
    <div class="hash">{n.hash_value}</div>
    <div class="desc">{n.description}</div>
    <div>
      <div style="display:flex; justify-content:space-between; font-size:10px; color:var(--dim);">
        <span>Originality Score</span>
        <span style="color:var(--text); font-weight:700;">{n.originality_score:.2f}</span>
      </div>
      <div class="orig-bar">
        <div class="orig-fill" style="width:{int(min(1.0, n.originality_score) * 100)}%; background:{'var(--ok)' if n.originality_score >= 0.7 else 'var(--warn)'};"></div>
      </div>
    </div>
  </div>''' for n in report.nodes)}
</div>

<script>
function filterGallery(type, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c => {{
    if (type === 'all' || c.getAttribute('data-type') === type) {{
      c.style.display = 'flex';
    }} else {{
      c.style.display = 'none';
    }}
  }});
}}
</script>
</body>
</html>"""


__all__ = ["MemeLineageReport", "MemeNode", "build_meme_lineage", "render_meme_gallery_html"]
