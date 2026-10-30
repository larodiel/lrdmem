"""Recall: the memories most relevant to a task, scored, with their connections and reliability (spec §6)."""

import math
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from lrdmem import config
from lrdmem.evidence import Index, confidence, memory_evidence, priority
from lrdmem.graph import Graph
from lrdmem.lint import printable
from lrdmem.model import STATES, Evidence
from lrdmem.search import Hit, candidates, expand, visible
from lrdmem.store import Memory, Store
from lrdmem.triggers import CAVEAT, in_scope

WEIGHTS = {"text": 1.0, "context": 0.5, "confidence": 0.8, "priority": 0.6, "hops": 0.3}
HOOK_STATES = ("active", "contested")
DEFAULT_STATES = ("active", "contested", "candidate")  # hypotheses are never active: they arrive as candidates
MAX_RELATIONS = 5
_KINDS = ["obligation", "pattern", "fact"]  # v0.1 tiebreak on equal scores: obligations first
_GROUPS = ("Consolidated knowledge", "Contested", "Hypotheses — not confirmed", "Candidates — not confirmed",
           "Historical experiences")


@dataclass
class Recalled(Hit):
    """A recalled memory: its search hit plus confidence, relations (type, ref, edge state, target state) and
    provenance."""

    confidence: float = 0.0
    relations: list[tuple[str, str, str, str]] = field(default_factory=list)
    provenance: list[Evidence] = field(default_factory=list)


def _weights(cfg: dict) -> dict:
    """WEIGHTS with the config's numeric `recall_weights` overrides for known keys."""
    over = cfg.get("recall_weights")
    w = dict(WEIGHTS)
    if isinstance(over, dict):
        w.update({k: float(v) for k, v in over.items() if k in WEIGHTS and isinstance(v, (int, float))
                  and not isinstance(v, bool) and math.isfinite(v)})
    return w


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _valid(m: Memory, today: date) -> bool:
    """False when valid_until has passed or valid_from is still ahead, by the first 10 chars (YYYY-MM-DD, so datetimes
    count by their written day); an unparseable date is no bound (lint flags it)."""
    until, since = _day(m.valid_until), _day(m.valid_from)
    return not (until and until < today) and not (since and since > today)


def recall(store: Store, env: str, repo: str, *, query: str = "", files: Sequence[str] = (),
           command: str | None = None, types: Sequence[str] | None = None, states: Sequence[str] | str | None = None,
           limit: int = 10, hops: int = 2, budget: int = 1500, for_hook: bool = False, now: datetime | None = None,
           exclude: set[str] | frozenset[str] = frozenset()) -> list[Recalled]:
    """The memories visible from env/repo most relevant to query/files/command, best first (`Recalled` hits).

    score = text*w + context*w + confidence*w + priority*w - hops*w (WEIGHTS, overridable by config
    `recall_weights`); text is the search score over the best in the set; context is 1.0 for the repo folder, 0.5 for
    <env>/_global, 0.25 for _global. Memories outside their valid_from/valid_until window (UTC day) and refs in
    `exclude` are dropped. States: `states` ("all" = every state), default active, contested and candidate
    (hypotheses included). for_hook: states active and contested only, no graph expansion, only hits with a trigger
    or entity signal, and an <env>/_global memory needs triggers.repos (the v0.1 injection rule). Whole items are
    kept in score order until the next would push the estimate (len(item)//4 tokens) past `budget`, at most `limit`.
    Relations
    (validated first, at most MAX_RELATIONS) reach only nodes visible from env/repo; for hooks only active or
    contested memories (and experiences).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).date()
    allowed = HOOK_STATES if for_hook else STATES if states == "all" else tuple(states or DEFAULT_STATES)
    found = candidates(store, env, repo, query=query, files=files, command=command, limit=sys.maxsize)
    graph = None
    if for_hook:
        found = [h for h in found if in_scope(h.node, env, repo) and any(not s.startswith("bm25: ") for s in h.signals)]
    elif found:
        graph = Graph(store)
        found = expand(store, found, hops, sys.maxsize, env=env, repo=repo, graph=graph)
    found = [h for h in found if h.node.ref not in exclude and h.node.state in allowed and _valid(h.node, today)
             and (types is None or h.node.memory_type in types)]
    if not found:
        return []
    graph = graph or Graph(store)
    cfg = config.load()
    w = _weights(cfg)
    top = max((h.score for h in found), default=0.0) or 1.0
    index = Index(store, graph.memories())
    scored = []
    for h in found:
        m = h.node
        ev = memory_evidence(store, m, index)
        conf = confidence(store, ev, index)
        context = 1.0 if m.repo is not None else 0.5 if m.env is not None else 0.25
        score = (w["text"] * h.score / top + w["context"] * context + w["confidence"] * conf
                 + w["priority"] * priority(store, m, now, cfg, index) - w["hops"] * h.hops)
        scored.append(Recalled(m, score, list(dict.fromkeys(h.signals)), h.hops, h.via, conf, [], ev))
    scored.sort(key=_rank)
    relations = _relations(graph, index, env, repo, for_hook)
    chosen: list[Recalled] = []
    used = 0
    for r in scored[:max(limit, 0)]:
        r.relations = relations.get(r.node.ref, [])[:MAX_RELATIONS]
        used += len(_item(r)) // 4
        if used > budget:
            break
        chosen.append(r)
    return chosen


def _rank(r: Recalled) -> tuple:
    """Sort key: best score, then v0.1 kind order, then ref."""
    return -r.score, _KINDS.index(r.node.kind) if r.node.kind in _KINDS else len(_KINDS), r.node.ref


def _relations(graph: Graph, index: Index, env: str, repo: str, for_hook: bool) -> dict[str, list[tuple]]:
    """ref -> (type, other end, edge state, target state) over validated/proposed edges, validated first, to memories
    and experiences visible from env/repo; for hooks only to active/contested memories and experiences."""
    nodes = {m.ref: (m, m.state) for m in graph.memories()}
    nodes.update({ref: (e, "experience") for ref, e in index.experiences.items()})
    out: dict[str, list[tuple]] = {}
    for e in graph.typed_edges():
        if e.state not in ("validated", "proposed"):
            continue
        for a, b in ((e.source, e.target), (e.target, e.source)):
            node, state = nodes.get(b, (None, ""))
            if node is None or not visible(node, env, repo) or (for_hook and state not in (*HOOK_STATES, "experience")):
                continue
            out.setdefault(a, []).append((e.type, b, e.state, state))
    return {ref: sorted(rels, key=lambda r: r[2] != "validated") for ref, rels in out.items()}


def _group(m: Memory) -> int:
    """Index into _GROUPS for the memory's state and type."""
    if m.state in ("superseded", "obsolete"):
        return 4
    if m.state == "contested":
        return 1
    if m.memory_type == "hypothesis" and m.state != "active":
        return 2
    if m.state == "candidate":
        return 3
    return 4 if m.memory_type == "episodic" else 0


def _item(r: Recalled) -> str:
    m = r.node
    lines = [f"### {m.ref} — {m.memory_type}, {m.state}, confidence {r.confidence:.2f}", m.body.strip(),
             "Why recalled: " + "; ".join(r.signals)]
    if r.relations:
        lines.append("Relations: " + "; ".join(f"{t} → {ref} ({st}, {ns})" for t, ref, st, ns in r.relations))
    return "\n".join(lines)


def render(hits: list[Recalled], target: str = "") -> str:
    """Markdown block for recalled hits, grouped by state/type with the reference-data caveat; "" when empty."""
    if not hits:
        return ""
    parts = [f"## lrdmem: memories for {target}".rstrip() + "\n" + CAVEAT]
    for i, name in enumerate(_GROUPS):
        items = [_item(h) for h in hits if _group(h.node) == i]
        if items:
            parts += [f"## {name}", *items]
    return "\n".join(printable(line) for line in ("\n\n".join(parts) + "\n").split("\n"))


def to_json(hits: list[Recalled]) -> list[dict]:
    """Recalled hits as JSON-ready dicts (id, content, type, state, confidence, provenance, relations, signals)."""
    return [{"id": h.node.ref, "content": h.node.body.strip(), "type": h.node.memory_type, "state": h.node.state,
             "confidence": round(h.confidence, 4), "provenance": [asdict(e) for e in h.provenance],
             "relations": [{"type": t, "target": ref, "state": st, "target_state": ns}
                           for t, ref, st, ns in h.relations],
             "signals": list(h.signals)} for h in hits]
