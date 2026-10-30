"""Associative search over the memories visible from a repo: BM25, entities, triggers and graph expansion."""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from lrdmem.graph import Graph
from lrdmem.store import Memory, Store
from lrdmem.triggers import glob_match, strs

_TOKEN = re.compile(r"[^\W_]+(?:[_./-][^\W_]+)*")
_WEIGHT = {"validated": 1.0, "proposed": 0.5}
TRIGGER_BOOST = 3.0
ENTITY_BOOST = 2.0


@dataclass
class Hit:
    """One associated memory: its score, the signals that brought it, and how far in the graph it is."""

    node: Memory
    score: float
    signals: list[str]
    hops: int = 0
    via: str | None = None


def _fold(text: str) -> str:
    """Case-folded text without accents, so `migracao` matches `Migração`."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(_fold(text))


def _mentions(text: str, entity: str) -> bool:
    """True when entity (non-empty) occurs in text as a whole path token, not inside a longer one."""
    entity = entity.strip()
    return bool(entity) and re.search(rf"(?<![\w./-]){re.escape(entity)}(?![\w/-]|\.\w)", text) is not None


def bm25(docs: list[tuple[str, str]], query: str, k1: float = 1.2, b: float = 0.75) -> list[tuple[str, float, list[str]]]:
    """Okapi BM25 of query over (id, text) docs: (id, score, top 5 matched terms), score desc then id; zero scores dropped."""
    toks = {i: Counter(_tokens(t)) for i, t in docs}
    if not toks:
        return []
    avg = sum(sum(c.values()) for c in toks.values()) / len(toks) or 1.0
    df = Counter(t for c in toks.values() for t in c)
    out = []
    for i, c in toks.items():
        length = sum(c.values())
        parts = {}
        for t in set(_tokens(query)):
            if c[t]:
                idf = math.log(1 + (len(toks) - df[t] + 0.5) / (df[t] + 0.5))
                parts[t] = idf * c[t] * (k1 + 1) / (c[t] + k1 * (1 - b + b * length / avg))
        if parts:
            terms = sorted(parts, key=lambda t: (-parts[t], t))[:5]
            out.append((i, sum(parts.values()), terms))
    return sorted(out, key=lambda r: (-r[1], r[0]))


def visible(node, env: str | None, repo: str | None) -> bool:
    """True when a node (memory, experience, edge...) is seen from repo `repo` of `env`: it lives in _global,
    <env>/_global or <env>/<repo>, and a memory's triggers.repos list, when present, names the repo. env/repo None
    (not opted in) sees only _global."""
    if node.env not in (None, env) or node.repo not in (None, repo):
        return False
    return not isinstance(node, Memory) or repo in (strs(node.triggers.get("repos")) or [repo])


def _visible(store: Store, env: str, repo: str) -> list[Memory]:
    """Memories visible from the repo (see `visible`)."""
    return [m for m in store.memories() if visible(m, env, repo)]


def _text(m: Memory) -> str:
    t = m.triggers
    return "\n".join([m.description, m.body, *strs(t.get("paths")), *strs(t.get("commands"))])


def candidates(store: Store, env: str, repo: str, *, query: str = "", files: Sequence[str] = (),
               command: str | None = None, limit: int = 20) -> list[Hit]:
    """Visible memories matching the query text, a file, a command or a mentioned id, best first."""
    mems = _visible(store, env, repo)
    hits: dict[str, Hit] = {}

    def add(m: Memory, score: float, signal: str) -> None:
        h = hits.setdefault(m.ref, Hit(m, 0.0, []))
        h.score += score
        h.signals.append(signal)

    by_ref = {m.ref: m for m in mems}
    for ref, score, terms in bm25([(m.ref, _text(m)) for m in mems], query):
        add(by_ref[ref], score, "bm25: " + ", ".join(terms))
    qtokens = set(_tokens(query))
    for m in mems:
        t = m.triggers
        for f in files:
            for g in strs(t.get("paths")):
                if glob_match(f, g):
                    add(m, TRIGGER_BOOST, f"file trigger {g}")
            if _mentions(f"{m.description}\n{m.body}", f):
                add(m, ENTITY_BOOST, f"entity {f}")
        if command:
            for c in strs(t.get("commands")):
                if command.startswith(c):
                    add(m, TRIGGER_BOOST, f"command trigger {c}")
            if _mentions(f"{m.description}\n{m.body}", command):
                add(m, ENTITY_BOOST, f"entity {command}")
        for ident in (m.slug, m.ref):
            if ident in qtokens:
                add(m, ENTITY_BOOST, f"entity {ident}")
                break
    return sorted(hits.values(), key=lambda h: (-h.score, h.node.ref))[:limit]


def expand(store: Store, hits: list[Hit], hops: int = 2, limit: int = 20, *, env: str, repo: str,
           graph: Graph | None = None) -> list[Hit]:
    """Hits plus their visible graph neighbours up to `hops`, breadth first; proposed edges count half.

    A neighbour scores parent x edge weight x 0.5; each memory appears once, at its nearest distance. Pass the
    store's `graph` when you already built one, to skip re-reading the store.
    """
    graph = graph or Graph(store)
    nodes = {m.ref: m for m in graph.memories() if visible(m, env, repo)}
    adj: dict[str, list[tuple[str, str, float]]] = {}
    for e in graph.typed_edges():
        if e.state in _WEIGHT:
            adj.setdefault(e.source, []).append((e.target, e.type, _WEIGHT[e.state]))
            adj.setdefault(e.target, []).append((e.source, e.type, _WEIGHT[e.state]))
    out = {h.node.ref: h for h in hits if h.node.ref in nodes}
    frontier = list(out.values())
    for depth in range(1, hops + 1):
        found: dict[str, Hit] = {}
        for h in frontier:
            for ref, typ, w in adj.get(h.node.ref, []):
                score = h.score * w * 0.5
                if ref in nodes and ref not in out and (ref not in found or score > found[ref].score):
                    found[ref] = Hit(nodes[ref], score, [f"{depth} hop{'s' if depth > 1 else ''} via {typ}"],
                                     depth, typ)
        out.update(found)
        frontier = list(found.values())
    return sorted(out.values(), key=lambda h: (-h.score, h.node.ref))[:limit]
