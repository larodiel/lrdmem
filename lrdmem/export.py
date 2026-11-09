"""Export the memory graph as JSON, Graphviz DOT or a self-contained interactive 3D HTML page."""

import json

from lrdmem import evidence
from lrdmem.graph import Graph
from lrdmem.store import Context, Memory


def _id(graph: Graph, item: Memory | Context) -> str:
    return item.path.relative_to(graph.store.root).with_suffix("").as_posix()


def _first(c: Context) -> str:
    return next((ln.strip() for ln in c.body.splitlines() if ln.strip()), c.id)


def to_json(graph: Graph) -> dict:
    """Nodes (memories, experiences) and edges, ids are store-relative paths.

    Memory nodes carry description, memory_type, state and the computed confidence; every edge carries rel and state: typed
    edges (file edges and v0.1 `[[links]]` as related_to), frontmatter `supersedes`, and experience → memory
    `context` links (shown as validated).
    """
    idx = evidence.Index(graph.store)
    nodes = [
        {"id": _id(graph, m), "type": "memory", "kind": m.kind, "env": m.env or "", "label": m.slug,
         "description": m.description, "strength": graph.strength(m), "memory_type": m.memory_type, "state": m.state,
         "confidence": round(evidence.confidence(graph.store, evidence.memory_evidence(graph.store, m, idx), idx), 3)}
        for m in graph.memories()
    ]
    nodes += [
        {"id": _id(graph, c), "type": "context", "kind": "context", "env": c.env or "", "label": _first(c),
         "strength": 0}
        for c in graph.store.experiences()
    ]
    edges = [{"source": _id(graph, s), "target": _id(graph, t), "rel": rel, "state": "validated"}
             for s, t, rel in graph.edges() if rel != "link"]  # [[links]] come from typed_edges as related_to
    edges += [{"source": e.source, "target": e.target, "rel": e.type, "state": e.state} for e in graph.typed_edges()]
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


_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>lrdmem memory map</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:radial-gradient(ellipse at 50% 42%,#161d30 0,#0b0e14 70%);color:#c9d1d9;font:13px/1.4 system-ui,sans-serif;overflow:hidden}
canvas#c{display:block;width:100vw;height:100vh;touch-action:none;cursor:grab}
#bar{position:fixed;top:12px;left:12px;width:300px;max-height:calc(100vh - 60px);overflow:auto;box-sizing:border-box;padding:10px 12px 12px;
background:#0d1117b8;backdrop-filter:blur(8px);border:1px solid #21262d;border-radius:12px}
#q{box-sizing:border-box;width:100%;background:#161b22;border:1px solid #30363d;border-radius:8px;color:inherit;padding:7px 10px}
#legend{display:grid;grid-template-columns:1fr 1fr;column-gap:12px}
#legend h4{grid-column:1/-1;margin:12px 0 4px;font:600 10px system-ui;letter-spacing:.09em;text-transform:uppercase;color:#6e7681}
#legend span{display:flex;align-items:center;color:#9da7b3;font-size:12px;line-height:20px;white-space:nowrap}
#legend canvas{width:14px;height:14px;margin-right:7px}
#legend b{display:inline-block;width:14px;height:2px;margin-right:7px;border-radius:1px}
#info{position:fixed;top:12px;right:12px;width:290px;max-height:80vh;overflow:auto;box-sizing:border-box;
background:#0d1117e6;backdrop-filter:blur(8px);border:1px solid #21262d;border-radius:12px;padding:14px;display:none}
#info h3{margin:0 0 6px;font-size:14px;word-break:break-all}
#info p{margin:4px 0;color:#8b949e;word-break:break-word}
#info button{display:block;max-width:100%;padding:2px 0;border:0;background:none;color:#7aa2f7;cursor:pointer;
text-align:left;font:inherit;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#info button:hover{color:#a9c1ff}
#hint{position:fixed;bottom:12px;left:14px;color:#6e7681}
#brand{position:fixed;bottom:12px;right:16px;color:#6e7681;font:600 12px ui-monospace,monospace;letter-spacing:.04em}
</style></head><body>
<canvas id="c" aria-label="3D map of the memory store"></canvas>
<div id="bar"><input id="q" type="search" placeholder="Search memories" aria-label="Search memories"><div id="legend"></div></div>
<div id="info" role="region" aria-live="polite"></div>
<div id="hint"></div><div id="brand">lrdmem &middot; memory map</div>
<script>
const data = __DATA__;
const cv = document.getElementById("c"), cx = cv.getContext("2d");
const $ = id => document.getElementById(id), q = $("q"), info = $("info"), legend = $("legend");
const el = (tag, text) => { const e = document.createElement(tag); e.textContent = text; return e; };
const isMem = n => n.type === "memory";
const PALETTE = ["#7aa2f7", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#7dcfff", "#ff9e64"];
const TYPES = ["semantic", "procedural", "episodic", "hypothesis", "prospective", "metacognitive"];
const STATES = ["candidate", "contested", "superseded", "obsolete"];
const EDGE = {related_to: "#6b7794", derived_from: "#7dcfff", occurred_in: "#c0a2ff", supports: "#9ece6a", contradicts: "#f7768e",
  caused_by: "#ff9e64", resolved_by: "#73daca", generalizes: "#bb9af7", supersedes: "#e0af68", context: "#46506e"};
const envs = [...new Set(data.nodes.filter(n => isMem(n) && n.env).map(n => n.env))].sort();
const colorOf = n => !isMem(n) ? "#7d8590" : n.env ? PALETTE[envs.indexOf(n.env) % PALETTE.length] : "#c0caf5";
const kindOf = n => isMem(n) ? n.memory_type : "experience";

function path(c, type, x, y, r) {
  c.beginPath();
  const poly = (m, rot) => { for (let i = 0; i < m; i++) { const a = rot + 2 * Math.PI * i / m; c[i ? "lineTo" : "moveTo"](x + r * Math.cos(a), y + r * Math.sin(a)); } c.closePath(); };
  if (type === "procedural") c.rect(x - r * .85, y - r * .85, r * 1.7, r * 1.7);
  else if (type === "episodic") poly(4, 0);
  else if (type === "prospective") poly(3, -Math.PI / 2);
  else if (type === "hypothesis") poly(6, 0);
  else if (type === "metacognitive") {
    for (let i = 0; i < 10; i++) { const a = -Math.PI / 2 + Math.PI * i / 5, d = i % 2 ? r * .5 : r * 1.2; c[i ? "lineTo" : "moveTo"](x + d * Math.cos(a), y + d * Math.sin(a)); }
    c.closePath();
  } else c.arc(x, y, r, 0, 7);
}
// state: active is solid, candidate dashed and faint, contested rimmed in amber, superseded/obsolete hollow.
function mark(c, type, state, x, y, r, col, glow) {
  const a = c.globalAlpha;
  path(c, type, x, y, r); c.fillStyle = c.strokeStyle = col; c.lineWidth = 1.5;
  if (state === "candidate") { c.globalAlpha = a * .4; c.fill(); c.globalAlpha = a; c.setLineDash([3, 2]); c.stroke(); }
  else if (state === "superseded" || state === "obsolete") { c.globalAlpha = a * .65; c.setLineDash(state === "obsolete" ? [2, 2] : []); c.stroke(); }
  else { if (glow) { c.shadowColor = col; c.shadowBlur = glow; } c.fill(); c.shadowBlur = 0; }
  c.setLineDash([]);
  if (state === "contested") { c.strokeStyle = "#e0af68"; c.lineWidth = 2; c.stroke(); }
  c.globalAlpha = a;
}

const swatch = (type, state, col) => {
  const c = document.createElement("canvas"); c.width = c.height = 28;
  mark(c.getContext("2d"), type, state, 14, 14, 8, col, 0); return c;
};
const line = col => { const b = document.createElement("b"); b.style.background = col; return b; };
const row = (icon, text) => { const s = document.createElement("span"); s.append(icon, text); legend.append(s); };
const head = t => legend.append(el("h4", t));
const mems = data.nodes.filter(isMem);
head("Environment");
envs.forEach(e => row(swatch("semantic", "active", PALETTE[envs.indexOf(e) % PALETTE.length]), e));
if (mems.some(n => !n.env)) row(swatch("semantic", "active", "#c0caf5"), "_global");
if (data.nodes.some(n => !isMem(n))) row(swatch("experience", "active", "#7d8590"), "experience");
head("Memory type");
TYPES.filter(t => mems.some(n => n.memory_type === t)).forEach(t => row(swatch(t, "active", "#c9d1d9"), t));
const states = STATES.filter(s => mems.some(n => n.state === s));
if (states.length) head("State");
states.forEach(s => row(swatch("semantic", s, "#c9d1d9"), s));
head("Relation");
const rels = Object.keys(EDGE).filter(r => data.edges.some(e => e.rel === r));
rels.forEach(r => row(line(EDGE[r]), r === "context" ? "evidence" : r));
$("hint").textContent = data.nodes.length
  ? mems.length + " memories · " + data.edges.length + " relations · drag to rotate, scroll to zoom, click a node"
  : "No memories yet.";

const byId = {};
data.nodes.forEach(n => { byId[n.id] = n; n.adj = new Set(); n.rels = new Map(); });
const edges = data.edges.map(e => [byId[e.source], byId[e.target], e]).filter(e => e[0] && e[1]);
edges.forEach(([a, b, e]) => { a.adj.add(b); b.adj.add(a); a.rels.set(b, e.rel); b.rels.set(a, e.rel); });

// Fruchterman-Reingold in 3D; unlinked nodes wait on the outer shell.
// ponytail: O(n^2) per step, fewer steps as n grows; use a grid or Barnes-Hut past a few thousand nodes.
const spiral = (i, m) => { const y = 1 - 2 * (i + .5) / m, r = Math.sqrt(1 - y * y), t = i * 2.399963; return [r * Math.cos(t), y, r * Math.sin(t)]; };
const len = v => Math.hypot(v[0], v[1], v[2]);
(function layout() {
  const linked = data.nodes.filter(n => n.adj.size), loose = data.nodes.filter(n => !n.adj.size);
  const side = Math.cbrt(linked.length || 1);
  linked.forEach((n, i) => n.p = spiral(i, linked.length).map(v => v * side));
  const steps = Math.max(30, Math.min(250, Math.round(4e7 / (linked.length ** 2 || 1))));
  for (let s = 0; s < steps; s++) {
    const t = .3 * side * (1 - s / steps) + .01;
    linked.forEach(n => n.d = [-n.p[0] * .3, -n.p[1] * .3, -n.p[2] * .3]);
    for (let i = 0; i < linked.length; i++) for (let j = i + 1; j < linked.length; j++) {
      const a = linked[i], b = linked[j], v = [0, 1, 2].map(k => a.p[k] - b.p[k]);
      const f = 12 / (v[0] * v[0] + v[1] * v[1] + v[2] * v[2] + 1e-4);
      for (let k = 0; k < 3; k++) { a.d[k] += v[k] * f; b.d[k] -= v[k] * f; }
    }
    for (const [a, b] of edges) {
      const v = [0, 1, 2].map(k => a.p[k] - b.p[k]), f = len(v);
      for (let k = 0; k < 3; k++) { a.d[k] -= v[k] * f; b.d[k] += v[k] * f; }
    }
    for (const n of linked) { const l = len(n.d) || 1, m = Math.min(l, t) / l; for (let k = 0; k < 3; k++) n.p[k] += n.d[k] * m; }
  }
  const shell = linked.length ? Math.max(...linked.map(n => len(n.p))) * .85 : side;
  loose.forEach((n, i) => n.p = spiral(i, loose.length).map(v => v * shell));
  const max = Math.max(1e-6, ...data.nodes.map(n => len(n.p)));
  data.nodes.forEach(n => n.p = n.p.map(v => v / max));
})();
const stars = Array.from({length: 160}, (_, i) => { // fixed backdrop dust, so every render looks the same
  const r = 1.5 + (i * 7919 % 100) / 100, p = spiral(i, 160); return {p: p.map(v => v * r), a: .12 + (i * 104729 % 100) / 400};
});

let W, H, dpr, yaw = .6, pitch = .3, zoom = 1, sel = null, hov = null, hits = null, focus = null, dirty = true, drag = null;
let auto = !matchMedia("(prefers-reduced-motion: reduce)").matches;
let cy, sy, cp, sp, S;
function resize() { dpr = devicePixelRatio || 1; W = innerWidth; H = innerHeight; cv.width = W * dpr; cv.height = H * dpr; dirty = true; }
addEventListener("resize", resize); resize();

const rot = ([x, y, z]) => { const x1 = x * cy + z * sy, z1 = -x * sy + z * cy; return [x1, y * cp - z1 * sp, y * sp + z1 * cp]; };
function project() {
  cy = Math.cos(yaw); sy = Math.sin(yaw); cp = Math.cos(pitch); sp = Math.sin(pitch); S = Math.min(W, H) * .5 * zoom;
  for (const n of data.nodes) {
    const [x, y, z] = rot(n.p);
    n.z = z; n.k = 4 / (4 - z); n.sx = W / 2 + x * S * n.k; n.sy = H / 2 - y * S * n.k;
  }
}
const radius = n => (isMem(n) ? 6 + 7 * n.confidence + Math.min(n.strength, 5) * .5 : 3.5) * n.k;
const FAST = data.nodes.length < 500; // glow costs a blur per node
function draw() {
  project();
  cx.setTransform(dpr, 0, 0, dpr, 0, 0); cx.clearRect(0, 0, W, H);
  cx.fillStyle = "#9aa7d1";
  for (const s of stars) { const [x, y, z] = rot(s.p), k = 4 / (4 - z); cx.globalAlpha = s.a; cx.fillRect(W / 2 + x * S * k, H / 2 - y * S * k, 1.3, 1.3); }
  const on = n => !focus || focus.has(n);
  for (const [a, b, e] of edges) {
    const lit = on(a) && on(b), hot = focus && (a === sel || b === sel), depth = .6 + .4 * ((a.z + b.z) / 2 + 1) / 2;
    cx.globalAlpha = hot ? .95 : lit ? .8 * depth : .05; cx.lineWidth = hot ? 2.2 : 1.3;
    cx.strokeStyle = EDGE[e.rel] || EDGE.related_to; cx.setLineDash(e.state === "proposed" ? [4, 3] : []);
    cx.beginPath(); cx.moveTo(a.sx, a.sy); cx.lineTo(b.sx, b.sy); cx.stroke();
  }
  cx.setLineDash([]); cx.font = "12px system-ui, sans-serif";
  for (const n of data.nodes.slice().sort((a, b) => a.z - b.z)) {
    const r = radius(n);
    cx.globalAlpha = (on(n) ? 1 : .12) * (.6 + .4 * (n.z + 1) / 2);
    mark(cx, kindOf(n), isMem(n) ? n.state : "active", n.sx, n.sy, r, colorOf(n), FAST && on(n) ? 10 * n.k : 0);
    if (n === sel || n === hov) { cx.globalAlpha = 1; cx.strokeStyle = "#fff"; cx.lineWidth = 1.5; cx.beginPath(); cx.arc(n.sx, n.sy, r + 4, 0, 7); cx.stroke(); }
    if (n === sel || n === hov || (isMem(n) && focus && focus.size <= 40 && focus.has(n))) {
      const x = n.sx + r + 6, y = n.sy + 4, t = n.label.slice(0, 40);
      cx.globalAlpha = 1; cx.lineWidth = 3; cx.strokeStyle = "#0b0e14"; cx.strokeText(t, x, y);
      cx.fillStyle = "#e6edf3"; cx.fillText(t, x, y);
    }
  }
  cx.globalAlpha = 1;
}

function show(n) {
  info.replaceChildren(); info.style.display = n ? "block" : "none";
  if (!n) return;
  info.append(el("h3", n.label));
  const where = n.env || "_global";
  (isMem(n)
    ? [n.description, where + " · " + n.memory_type + " · " + n.state, "confidence " + n.confidence + " · " + n.strength + " experiences", n.id]
    : ["experience in " + where, n.id]).forEach(t => t && info.append(el("p", t)));
  if (n.adj.size) {
    info.append(el("p", "Connected to"));
    [...n.adj].sort((a, b) => a.label.localeCompare(b.label)).slice(0, 30).forEach(m => {
      const r = n.rels.get(m), b = el("button", (r === "context" ? "evidence" : r) + " · " + m.label);
      b.onclick = () => select(m); info.append(b);
    });
  }
}
function select(n) {
  sel = n; focus = n ? new Set([n, ...n.adj]) : hits; show(n); dirty = true;
}
q.addEventListener("input", () => {
  const t = q.value.trim().toLowerCase();
  hits = t ? new Set(data.nodes.filter(n => (n.label + " " + (n.description || "") + " " + n.id).toLowerCase().includes(t))) : null;
  select(null);
});
addEventListener("keydown", e => { if (e.key === "Escape") select(null); });

function pick(e) {
  let best = null;
  for (const n of data.nodes) {
    const r = radius(n) + 5, dx = n.sx - e.clientX, dy = n.sy - e.clientY;
    if (dx * dx + dy * dy <= r * r && (!best || n.z > best.z)) best = n;
  }
  return best;
}
cv.addEventListener("pointerdown", e => { cv.setPointerCapture(e.pointerId); drag = {x: e.clientX, y: e.clientY, moved: 0}; auto = false; cv.style.cursor = "grabbing"; });
cv.addEventListener("pointermove", e => {
  if (drag) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
    yaw += dx * .005; pitch = Math.max(-1.4, Math.min(1.4, pitch + dy * .005)); dirty = true;
  } else {
    const n = pick(e);
    if (n !== hov) { hov = n; cv.style.cursor = n ? "pointer" : "grab"; dirty = true; }
  }
});
cv.addEventListener("pointerup", e => {
  if (drag && drag.moved < 5) select(pick(e));
  drag = null; cv.style.cursor = hov ? "pointer" : "grab";
});
cv.addEventListener("wheel", e => { e.preventDefault(); zoom = Math.max(.3, Math.min(6, zoom * Math.exp(-e.deltaY * .001))); dirty = true; }, {passive: false});

(function loop() {
  if (auto && !sel) { yaw += .002; dirty = true; }
  if (dirty) { dirty = false; draw(); }
  requestAnimationFrame(loop);
})();
</script></body></html>
"""


def to_html(graph: Graph) -> str:
    """A single-file HTML page: the graph inlined, a 3D force layout on a canvas; no external resources."""
    blob = json.dumps(to_json(graph)).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _PAGE.replace("__DATA__", blob)
