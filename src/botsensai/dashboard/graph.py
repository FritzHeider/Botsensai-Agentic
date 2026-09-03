"""Interactive on-chain topology and funder graph builder.

Transforms on-chain transaction history, funder relationships, and wallet links
into an interactive D3.js force-directed network graph model.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from botsensai.models import utcnow
from botsensai.store.db import Database


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    group: str  # "deployer" | "funder" | "insider" | "trader" | "pool"
    size: float
    details: dict[str, Any]


@dataclass(frozen=True)
class GraphLink:
    source: str
    target: str
    kind: str  # "funding" | "buy" | "sell" | "bundle"
    weight: float


@dataclass
class TopologyGraph:
    nodes: list[GraphNode]
    links: list[GraphLink]
    token_key: str
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_key": self.token_key,
            "summary": self.summary,
            "nodes": [asdict(n) for n in self.nodes],
            "links": [asdict(lnk) for lnk in self.links],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def build_topology_graph(
    token_key: str, db: Database, as_of: datetime | None = None
) -> TopologyGraph:
    """Extract wallet nodes and relationship links for a specific token."""
    now = as_of or utcnow()
    mint = token_key.split(":")[-1]
    holders = db.holders_as_of(token_key, now)
    trades = db.trades_as_of(token_key, now)

    nodes_by_id: dict[str, GraphNode] = {}
    links: list[GraphLink] = []

    # 1. Pool / Token Center Node
    pool_node_id = f"pool:{mint[:8]}"
    nodes_by_id[pool_node_id] = GraphNode(
        id=pool_node_id,
        label=f"Token ({mint[:6]}...)",
        group="pool",
        size=24.0,
        details={"mint": mint, "holders_count": len(holders)},
    )

    # 2. Add Holder Nodes
    for holder in holders[:40]:
        h_id = holder.wallet
        short_label = f"{h_id[:4]}...{h_id[-4:]}"
        labels = holder.labels or []
        group = "deployer" if "deployer" in labels else "insider" if "insider" in labels else "trader"
        nodes_by_id[h_id] = GraphNode(
            id=h_id,
            label=short_label,
            group=group,
            size=max(8.0, min(22.0, float(holder.share_of_supply * 60.0))),
            details={
                "share": holder.share_of_supply,
                "balance": holder.balance,
                "labels": labels,
                "funded_by": holder.funded_by,
            },
        )
        links.append(
            GraphLink(
                source=h_id,
                target=pool_node_id,
                kind="holding",
                weight=max(1.0, float(holder.share_of_supply * 10.0)),
            )
        )

        if holder.funded_by and holder.funded_by not in nodes_by_id:
            f_id = holder.funded_by
            nodes_by_id[f_id] = GraphNode(
                id=f_id,
                label=f"Funder ({f_id[:4]}...)",
                group="funder",
                size=12.0,
                details={"funded_target": h_id},
            )
            links.append(
                GraphLink(
                    source=f_id,
                    target=h_id,
                    kind="funding",
                    weight=2.0,
                )
            )

    # 3. Add Trade Flows
    for trade in trades[:60]:
        t_id = trade.wallet
        if t_id and t_id not in nodes_by_id:
            nodes_by_id[t_id] = GraphNode(
                id=t_id,
                label=f"{t_id[:4]}...{t_id[-4:]}",
                group="trader",
                size=10.0,
                details={"side": trade.side.value, "amount_native": trade.amount_native},
            )
        if t_id:
            links.append(
                GraphLink(
                    source=t_id,
                    target=pool_node_id,
                    kind="buy" if trade.side.value == "buy" else "sell",
                    weight=max(1.0, min(5.0, float(trade.amount_native or 1.0))),
                )
            )

    summary = {
        "nodes_count": len(nodes_by_id),
        "links_count": len(links),
        "deployers": sum(1 for n in nodes_by_id.values() if n.group == "deployer"),
        "insiders": sum(1 for n in nodes_by_id.values() if n.group == "insider"),
        "funders": sum(1 for n in nodes_by_id.values() if n.group == "funder"),
    }

    return TopologyGraph(
        nodes=list(nodes_by_id.values()),
        links=links,
        token_key=token_key,
        summary=summary,
    )


def render_topology_html(graph: TopologyGraph) -> str:
    """Render a standalone interactive D3.js force-directed HTML graph."""
    graph_json = graph.to_json()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topology Graph — {graph.token_key}</title>
<style>
body {{ margin:0; background:#0b0d12; color:#e2e8f0; font-family:ui-monospace,monospace; overflow:hidden; }}
header {{ position:absolute; top:12px; left:16px; z-index:10; background:rgba(17,21,28,0.9); padding:10px 16px; border:1px solid #222936; border-radius:6px; }}
h1 {{ margin:0; font-size:14px; color:#38bdf8; }}
.meta {{ font-size:11px; color:#798699; margin-top:4px; }}
svg {{ width:100vw; height:100vh; }}
.tooltip {{ position:absolute; padding:8px 12px; background:rgba(15,23,42,0.95); border:1px solid #38bdf8; border-radius:4px; font-size:11px; pointer-events:none; display:none; z-index:20; }}
</style>
</head>
<body>
<header>
  <h1>On-Chain Topology & Funder Graph</h1>
  <div class="meta">{graph.token_key} · {graph.summary['nodes_count']} Wallets · {graph.summary['insiders']} Insiders</div>
</header>
<div id="tooltip" class="tooltip"></div>
<svg id="canvas"></svg>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const data = {graph_json};
const width = window.innerWidth;
const height = window.innerHeight;
const svg = d3.select("#canvas").attr("viewBox", [0, 0, width, height]);
const g = svg.append("g");

svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", (e) => g.attr("transform", e.transform)));

const colorMap = {{
  pool: "#38bdf8",
  deployer: "#ef4444",
  insider: "#f59e0b",
  funder: "#a855f7",
  trader: "#22c55e"
}};

const simulation = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(data.links).id(d => d.id).distance(80))
  .force("charge", d3.forceManyBody().strength(-200))
  .force("center", d3.forceCenter(width / 2, height / 2));

const link = g.append("g")
  .selectAll("line")
  .data(data.links)
  .join("line")
  .attr("stroke", d => d.kind === 'buy' ? '#22c55e' : d.kind === 'sell' ? '#ef4444' : '#475569')
  .attr("stroke-opacity", 0.6)
  .attr("stroke-width", d => Math.sqrt(d.weight || 1));

const node = g.append("g")
  .selectAll("circle")
  .data(data.nodes)
  .join("circle")
  .attr("r", d => d.size)
  .attr("fill", d => colorMap[d.group] || "#64748b")
  .attr("stroke", "#0b0d12")
  .attr("stroke-width", 2)
  .call(d3.drag()
    .on("start", dragstarted)
    .on("drag", dragged)
    .on("end", dragended));

const tooltip = document.getElementById("tooltip");

node.on("mouseover", (event, d) => {{
  tooltip.style.display = "block";
  tooltip.innerHTML = `<strong>${{d.label}}</strong><br>Group: ${{d.group}}<br>ID: ${{d.id}}`;
  tooltip.style.left = (event.pageX + 10) + "px";
  tooltip.style.top = (event.pageY + 10) + "px";
}}).on("mouseout", () => {{ tooltip.style.display = "none"; }});

simulation.on("tick", () => {{
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("cx", d => d.x).attr("cy", d => d.y);
}});

function dragstarted(event) {{
  if (!event.active) simulation.alphaTarget(0.3).restart();
  event.subject.fx = event.subject.x;
  event.subject.fy = event.subject.y;
}}
function dragged(event) {{
  event.subject.fx = event.x;
  event.subject.fy = event.y;
}}
function dragended(event) {{
  if (!event.active) simulation.alphaTarget(0);
  event.subject.fx = null;
  event.subject.fy = null;
}}
</script>
</body>
</html>"""


__all__ = ["GraphLink", "GraphNode", "TopologyGraph", "build_topology_graph", "render_topology_html"]
