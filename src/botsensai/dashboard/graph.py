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
    """Render an interactive D3.js force-directed HTML graph with filters and inspector."""
    graph_json = graph.to_json()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topology Graph — {graph.token_key}</title>
<style>
:root {{
  --bg: #0a0c10;
  --panel: rgba(17, 21, 28, 0.92);
  --line: #222936;
  --text: #e2e8f0;
  --dim: #798699;
  --accent: #38bdf8;
  --ok: #22c55e;
  --warn: #f59e0b;
  --alarm: #ef4444;
  --purple: #a855f7;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  overflow: hidden;
  user-select: none;
}}
header {{
  position: absolute;
  top: 12px;
  left: 16px;
  z-index: 10;
  background: var(--panel);
  backdrop-filter: blur(10px);
  padding: 10px 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
h1 {{ margin: 0; font-size: 14px; color: var(--accent); letter-spacing: 0.05em; }}
.meta {{ font-size: 11px; color: var(--dim); }}
.controls-hud {{
  position: absolute;
  top: 12px;
  right: 16px;
  z-index: 10;
  display: flex;
  gap: 8px;
  align-items: center;
}}
.btn {{
  background: var(--panel);
  border: 1px solid var(--line);
  color: var(--text);
  padding: 6px 12px;
  border-radius: 6px;
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: all 0.15s;
}}
.btn:hover {{ background: #1a202c; border-color: var(--accent); color: var(--accent); }}
.filter-bar {{
  position: absolute;
  bottom: 16px;
  left: 16px;
  z-index: 10;
  background: var(--panel);
  backdrop-filter: blur(10px);
  border: 1px solid var(--line);
  padding: 8px 14px;
  border-radius: 8px;
  display: flex;
  gap: 14px;
  font-size: 11px;
  flex-wrap: wrap;
}}
.filter-item {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}}
.filter-item input {{ cursor: pointer; accent-color: var(--accent); }}
.color-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
svg {{ width: 100vw; height: 100vh; cursor: grab; }}
svg:active {{ cursor: grabbing; }}
.tooltip {{
  position: absolute;
  padding: 8px 12px;
  background: rgba(15, 23, 42, 0.95);
  border: 1px solid var(--accent);
  border-radius: 6px;
  font-size: 11px;
  pointer-events: none;
  display: none;
  z-index: 20;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}}
.drawer {{
  position: absolute;
  top: 0;
  right: 0;
  width: 320px;
  height: 100vh;
  background: rgba(15, 20, 28, 0.96);
  backdrop-filter: blur(12px);
  border-left: 1px solid var(--line);
  z-index: 30;
  transform: translateX(100%);
  transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  display: flex;
  flex-direction: column;
  padding: 20px;
  box-shadow: -8px 0 24px rgba(0,0,0,0.5);
}}
.drawer.open {{ transform: translateX(0); }}
.drawer-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--line);
  padding-bottom: 12px;
  margin-bottom: 16px;
}}
.drawer-title {{ font-size: 13px; font-weight: 700; color: var(--accent); text-transform: uppercase; }}
.close-btn {{ background: none; border: none; color: var(--dim); font-size: 18px; cursor: pointer; }}
.close-btn:hover {{ color: var(--text); }}
.drawer-body {{ display: flex; flex-direction: column; gap: 14px; font-size: 12px; overflow-y: auto; }}
.prop-row {{ display: flex; flex-direction: column; gap: 3px; }}
.prop-label {{ color: var(--dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }}
.prop-val {{ word-break: break-all; font-weight: 600; }}
.badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; width: fit-content; }}
.copy-pill {{
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--line);
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  margin-top: 2px;
}}
.copy-pill:hover {{ border-color: var(--accent); }}
.share-bar {{ height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin-top: 4px; }}
.share-fill {{ height: 100%; background: var(--accent); }}
.action-link {{
  display: block;
  text-align: center;
  background: rgba(56, 189, 248, 0.15);
  border: 1px solid rgba(56, 189, 248, 0.4);
  color: var(--accent);
  padding: 8px;
  border-radius: 6px;
  text-decoration: none;
  font-weight: 600;
  margin-top: 8px;
}}
.action-link:hover {{ background: rgba(56, 189, 248, 0.25); }}
</style>
</head>
<body>
<header>
  <h1>On-Chain Topology & Funder Graph</h1>
  <div class="meta">{graph.token_key} · {graph.summary['nodes_count']} Wallets · {graph.summary['insiders']} Insiders</div>
</header>

<div class="controls-hud">
  <button class="btn" onclick="zoomIn()">Zoom +</button>
  <button class="btn" onclick="zoomOut()">Zoom −</button>
  <button class="btn" onclick="resetZoom()">⟲ Reset</button>
  <button class="btn" onclick="exportSVG()">💾 Export</button>
</div>

<div class="filter-bar">
  <span style="font-weight:700; color:var(--dim); margin-right:4px;">Filter:</span>
  <label class="filter-item">
    <input type="checkbox" id="f-pool" checked onchange="applyFilters()">
    <span class="color-dot" style="background:#38bdf8;"></span> Pool
  </label>
  <label class="filter-item">
    <input type="checkbox" id="f-deployer" checked onchange="applyFilters()">
    <span class="color-dot" style="background:#ef4444;"></span> Deployer ({graph.summary.get('deployers', 0)})
  </label>
  <label class="filter-item">
    <input type="checkbox" id="f-funder" checked onchange="applyFilters()">
    <span class="color-dot" style="background:#a855f7;"></span> Funders ({graph.summary.get('funders', 0)})
  </label>
  <label class="filter-item">
    <input type="checkbox" id="f-insider" checked onchange="applyFilters()">
    <span class="color-dot" style="background:#f59e0b;"></span> Insiders ({graph.summary.get('insiders', 0)})
  </label>
  <label class="filter-item">
    <input type="checkbox" id="f-trader" checked onchange="applyFilters()">
    <span class="color-dot" style="background:#22c55e;"></span> Traders
  </label>
</div>

<div id="tooltip" class="tooltip"></div>

<!-- Node Details Drawer -->
<div class="drawer" id="node-drawer">
  <div class="drawer-header">
    <span class="drawer-title" id="drawer-node-title">Wallet Dossier</span>
    <button class="close-btn" onclick="closeDrawer()">&times;</button>
  </div>
  <div class="drawer-body" id="drawer-content"></div>
</div>

<svg id="canvas"></svg>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const data = {graph_json};
const width = window.innerWidth;
const height = window.innerHeight;
const svg = d3.select("#canvas").attr("viewBox", [0, 0, width, height]);
const g = svg.append("g");

const zoom = d3.zoom().scaleExtent([0.1, 4]).on("zoom", (e) => g.attr("transform", e.transform));
svg.call(zoom);

const colorMap = {{
  pool: "#38bdf8",
  deployer: "#ef4444",
  insider: "#f59e0b",
  funder: "#a855f7",
  trader: "#22c55e"
}};

const simulation = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(data.links).id(d => d.id).distance(80))
  .force("charge", d3.forceManyBody().strength(-220))
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
  .style("cursor", "pointer")
  .call(d3.drag()
    .on("start", dragstarted)
    .on("drag", dragged)
    .on("end", dragended));

const tooltip = document.getElementById("tooltip");
let selectedNode = null;

node.on("mouseover", (event, d) => {{
  tooltip.style.display = "block";
  tooltip.innerHTML = `<strong>${{d.label}}</strong><br>Group: <span style="color:${{colorMap[d.group]}}">${{d.group.toUpperCase()}}</span><br>ID: ${{d.id.slice(0,16)}}...`;
  tooltip.style.left = (event.pageX + 12) + "px";
  tooltip.style.top = (event.pageY + 12) + "px";
}}).on("mouseout", () => {{ tooltip.style.display = "none"; }});

node.on("click", (event, d) => {{
  event.stopPropagation();
  selectNode(d);
}});

svg.on("click", () => {{
  closeDrawer();
  clearSelection();
}});

function selectNode(d) {{
  selectedNode = d;
  // Connected highlighting
  const connectedIds = new Set();
  connectedIds.add(d.id);
  data.links.forEach(l => {{
    const sId = typeof l.source === 'object' ? l.source.id : l.source;
    const tId = typeof l.target === 'object' ? l.target.id : l.target;
    if (sId === d.id) connectedIds.add(tId);
    if (tId === d.id) connectedIds.add(sId);
  }});

  node.style("opacity", n => connectedIds.has(n.id) ? 1.0 : 0.15)
      .attr("stroke", n => n.id === d.id ? "#ffffff" : "#0b0d12")
      .attr("stroke-width", n => n.id === d.id ? 3 : 2);

  link.style("opacity", l => {{
    const sId = typeof l.source === 'object' ? l.source.id : l.source;
    const tId = typeof l.target === 'object' ? l.target.id : l.target;
    return (sId === d.id || tId === d.id) ? 1.0 : 0.08;
  }});

  // Fill Drawer
  const drawer = document.getElementById("node-drawer");
  document.getElementById("drawer-node-title").innerText = `${{d.group.toUpperCase()}} DOSSIER`;
  const isWallet = d.group !== 'pool';
  const cleanId = d.id.replace('pool:', '');
  const share = d.details && d.details.share ? (d.details.share * 100).toFixed(2) : null;
  const fundedBy = d.details && d.details.funded_by ? d.details.funded_by : null;

  let html = `
    <div class="prop-row">
      <span class="prop-label">Role</span>
      <span class="badge" style="background:${{colorMap[d.group]}}22; color:${{colorMap[d.group]}}; border:1px solid ${{colorMap[d.group]}}">${{d.group.toUpperCase()}}</span>
    </div>
    <div class="prop-row">
      <span class="prop-label">Wallet Address</span>
      <div class="copy-pill" onclick="navigator.clipboard.writeText('${{cleanId}}'); alert('Address copied!');">
        <span style="font-size:11px;">${{cleanId.slice(0,10)}}...${{cleanId.slice(-6)}}</span>
        <span style="color:var(--dim); font-size:10px;">📋 COPY</span>
      </div>
    </div>
  `;

  if (share !== null) {{
    html += `
      <div class="prop-row">
        <span class="prop-label">Supply Share</span>
        <div class="prop-val">${{share}}%</div>
        <div class="share-bar"><div class="share-fill" style="width:${{Math.min(100, share * 3)}}%"></div></div>
      </div>
    `;
  }}

  if (fundedBy) {{
    html += `
      <div class="prop-row">
        <span class="prop-label">Funded By</span>
        <div class="copy-pill" onclick="focusNode('${{fundedBy}}')">
          <span style="font-size:11px;">${{fundedBy.slice(0,8)}}...${{fundedBy.slice(-4)}}</span>
          <span style="color:var(--accent); font-size:10px;">TARGET ↗</span>
        </div>
      </div>
    `;
  }}

  if (isWallet) {{
    html += `<a href="https://solscan.io/account/${{cleanId}}" target="_blank" class="action-link">View on Solscan ↗</a>`;
  }} else {{
    html += `<a href="https://solscan.io/token/${{cleanId}}" target="_blank" class="action-link">View Token on Solscan ↗</a>`;
  }}

  document.getElementById("drawer-content").innerHTML = html;
  drawer.classList.add("open");
}}

function closeDrawer() {{
  document.getElementById("node-drawer").classList.remove("open");
}}

function clearSelection() {{
  selectedNode = null;
  node.style("opacity", 1.0).attr("stroke", "#0b0d12").attr("stroke-width", 2);
  link.style("opacity", 0.6);
}}

function focusNode(nodeId) {{
  const target = data.nodes.find(n => n.id === nodeId);
  if (target) {{
    selectNode(target);
    svg.transition().duration(500).call(
      zoom.transform,
      d3.zoomIdentity.translate(width / 2 - target.x, height / 2 - target.y).scale(1.5)
    );
  }}
}}

function applyFilters() {{
  const showPool = document.getElementById("f-pool").checked;
  const showDeployer = document.getElementById("f-deployer").checked;
  const showFunder = document.getElementById("f-funder").checked;
  const showInsider = document.getElementById("f-insider").checked;
  const showTrader = document.getElementById("f-trader").checked;

  const visibility = {{
    pool: showPool,
    deployer: showDeployer,
    funder: showFunder,
    insider: showInsider,
    trader: showTrader,
  }};

  node.style("display", d => visibility[d.group] !== false ? "inline" : "none");
  link.style("display", l => {{
    const sGroup = typeof l.source === 'object' ? l.source.group : data.nodes.find(n=>n.id===l.source)?.group;
    const tGroup = typeof l.target === 'object' ? l.target.group : data.nodes.find(n=>n.id===l.target)?.group;
    return (visibility[sGroup] !== false && visibility[tGroup] !== false) ? "inline" : "none";
  }});
}}

function zoomIn() {{ svg.transition().duration(250).call(zoom.scaleBy, 1.3); }}
function zoomOut() {{ svg.transition().duration(250).call(zoom.scaleBy, 0.77); }}
function resetZoom() {{ svg.transition().duration(350).call(zoom.transform, d3.zoomIdentity); }}

function exportSVG() {{
  const serializer = new XMLSerializer();
  const source = serializer.serializeToString(document.getElementById("canvas"));
  const blob = new Blob([source], {{type: "image/svg+xml;charset=utf-8"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "topology_graph_{graph.token_key.split(':')[-1][:8]}.svg";
  a.click();
}}

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
