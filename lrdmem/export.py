"""Export the memory graph as JSON, Graphviz DOT or a self-contained HTML page."""

import json

from lrdmem.graph import Graph
from lrdmem.store import Context, Memory


def _id(graph: Graph, item: Memory | Context) -> str:
    return item.path.relative_to(graph.store.root).with_suffix("").as_posix()


def _first(c: Context) -> str:
    return next((ln.strip() for ln in c.body.splitlines() if ln.strip()), c.id)


def to_json(graph: Graph) -> dict:
    """Nodes (memories and contexts) and edges (context, link, supersedes), ids are store-relative paths."""
    nodes = [
        {"id": _id(graph, m), "type": "memory", "kind": m.kind, "env": m.env or "", "label": m.slug,
         "strength": graph.strength(m)}
        for m in graph.memories()
    ]
    nodes += [
        {"id": _id(graph, c), "type": "context", "kind": "context", "env": c.env or "", "label": _first(c),
         "strength": 0}
        for c in graph.store.contexts()
    ]
    edges = [{"source": _id(graph, s), "target": _id(graph, t), "rel": rel} for s, t, rel in graph.edges()]
    return {"nodes": nodes, "edges": edges}


def _q(text: str) -> str:
    """Quote text for a DOT string: escape backslash and quote, flatten newlines."""
    flat = " ".join(text.splitlines())
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(graph: Graph) -> str:
    """The graph in Graphviz DOT syntax."""
    data = to_json(graph)
    lines = ["digraph lrdmem {"]
    for n in data["nodes"]:
        shape = "box" if n["type"] == "memory" else "ellipse"
        lines.append(f"  {_q(n['id'])} [label={_q(n['label'])}, shape={shape}];")
    for e in data["edges"]:
        lines.append(f"  {_q(e['source'])} -> {_q(e['target'])} [label={_q(e['rel'])}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>lrdmem graph</title>
<style>body{margin:0;font-family:sans-serif}canvas{display:block;width:100vw;height:100vh}</style>
</head><body><canvas id="c"></canvas>
<script>
const data = __DATA__;
const cv = document.getElementById("c"), cx = cv.getContext("2d");
const W = cv.width = innerWidth, H = cv.height = innerHeight;
const byId = {};
data.nodes.forEach((n, i) => {
  const a = 2 * Math.PI * i / data.nodes.length;
  n.x = W / 2 + Math.cos(a) * W / 4; n.y = H / 2 + Math.sin(a) * H / 4; n.vx = n.vy = 0;
  byId[n.id] = n;
});
const edges = data.edges.map(e => [byId[e.source], byId[e.target], e.rel]).filter(e => e[0] && e[1]);
function step() {
  for (const a of data.nodes) for (const b of data.nodes) {
    if (a === b) continue;
    const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01, f = 2000 / d2;
    a.vx += dx * f; a.vy += dy * f;
  }
  for (const [a, b] of edges) {
    const dx = b.x - a.x, dy = b.y - a.y;
    a.vx += dx * 0.01; a.vy += dy * 0.01; b.vx -= dx * 0.01; b.vy -= dy * 0.01;
  }
  for (const n of data.nodes) {
    n.vx += (W / 2 - n.x) * 0.002; n.vy += (H / 2 - n.y) * 0.002;
    n.x += n.vx *= 0.85; n.y += n.vy *= 0.85;
  }
}
function draw() {
  cx.clearRect(0, 0, W, H);
  for (const [a, b, rel] of edges) {
    cx.strokeStyle = rel === "supersedes" ? "#c33" : rel === "link" ? "#38c" : "#aaa";
    cx.beginPath(); cx.moveTo(a.x, a.y); cx.lineTo(b.x, b.y); cx.stroke();
  }
  for (const n of data.nodes) {
    cx.fillStyle = n.type === "memory" ? "#2a7" : "#999";
    cx.beginPath(); cx.arc(n.x, n.y, 4 + n.strength * 2, 0, 7); cx.fill();
    cx.fillStyle = "#000"; cx.fillText(n.label, n.x + 8, n.y + 3);
  }
}
let ticks = 0;
(function loop() { step(); draw(); if (++ticks < 400) requestAnimationFrame(loop); })();
</script></body></html>
"""


def to_html(graph: Graph) -> str:
    """A single-file HTML page with the graph inlined and a canvas force layout; no external resources."""
    blob = json.dumps(to_json(graph)).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _PAGE.replace("__DATA__", blob)
