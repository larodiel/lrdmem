"""Link graph over a store: memories reinforced by contexts and tied together by links."""

from collections import defaultdict

from pathlib import Path

from lrdmem import evidence
from lrdmem.model import Evidence
from lrdmem.store import Context, Edge, Experience, Memory, Store

_Key = tuple[str | None, str]


class AmbiguousSlug(Exception):
    """Raised when a slug exists in several environments and no env was given."""

    def __init__(self, slug: str, candidates: list[Memory]):
        super().__init__(f"'{slug}' exists in several environments; pass --env")
        self.candidates = candidates


class _Implicit(Edge):
    """A `[[link]]` read as a validated related_to edge; it has no file."""

    @property
    def ref(self) -> str:
        return ""


class Graph:
    """Read-only graph built from a store; nodes are (env, slug) pairs."""

    def __init__(self, store: Store):
        self.store = store
        self._nodes: dict[_Key, Memory] = {(m.env, m.slug): m for m in store.memories()}
        self._contexts: dict[_Key, list[Context]] = defaultdict(list)
        self._edges: dict[_Key, set[_Key]] = defaultdict(set)  # undirected
        for m in self._nodes.values():
            for target in m.links + m.supersedes:
                self._link((m.env, m.slug), self._target(m.env, target))
        for c in sorted(store.contexts(), key=lambda c: c.date):
            keys = {k for k in (self._target(c.env, s) for s in c.memories) if k}
            for k in keys:
                self._contexts[k].append(c)
                for other in keys:
                    self._link(k, other)

    def memories(self) -> list[Memory]:
        """Every memory node of the graph."""
        return list(self._nodes.values())

    def _target(self, env: str | None, slug: str) -> _Key | None:
        """Resolve a slug seen from env: its own env first, then _global."""
        for key in ((env, slug), (None, slug)):
            if key in self._nodes:
                return key
        return None

    def _link(self, a: _Key, b: _Key | None) -> None:
        if b and a != b:
            self._edges[a].add(b)
            self._edges[b].add(a)

    def resolve(self, slug: str, env: str | None = None) -> Memory | None:
        """The memory for slug (in env, then _global); AmbiguousSlug if env is omitted and several match."""
        if env:
            key = self._target(env, slug)
            return self._nodes[key] if key else None
        found = [m for (_, s), m in self._nodes.items() if s == slug]
        if len(found) > 1:
            raise AmbiguousSlug(slug, found)
        return found[0] if found else None

    def _key(self, slug: str | Memory, env: str | None) -> _Key | None:
        if isinstance(slug, Memory):  # already resolved: never re-resolve by slug
            return (slug.env, slug.slug)
        m = self.resolve(slug, env)
        return (m.env, m.slug) if m else None

    def strength(self, slug: str | Memory, env: str | None = None) -> int:
        """Number of contexts linking the memory."""
        return len(self._contexts.get(self._key(slug, env), []))

    def contexts_of(self, slug: str | Memory, env: str | None = None) -> list[Context]:
        """Contexts linking the memory, oldest first."""
        return list(self._contexts.get(self._key(slug, env), []))

    def related(self, slug: str | Memory, env: str | None = None) -> list[str]:
        """Slugs linked to or from the memory, superseding/superseded by it, or sharing a context."""
        return sorted({s for _, s in self._edges.get(self._key(slug, env), set())})

    def edges(self) -> list[tuple[Memory | Context, Memory, str]]:
        """Directed (source, target, rel) triples; rel is context, link or supersedes."""
        out: list[tuple[Memory | Context, Memory, str]] = []
        for m in self._nodes.values():
            for rel, slugs in (("link", m.links), ("supersedes", m.supersedes)):
                for slug in slugs:
                    key = self._target(m.env, slug)
                    if key and key != (m.env, m.slug):
                        out.append((m, self._nodes[key], rel))
        for key, ctxs in self._contexts.items():
            out += [(c, self._nodes[key], "context") for c in ctxs]
        return out

    def find(self, term: str, env: str | None = None) -> list[Memory | Context]:
        """Memories then contexts whose slug, description or body contains term (case-insensitive)."""
        t = term.lower()
        mems = [
            m for m in self._nodes.values()
            if (env is None or m.env in (None, env))
            and t in f"{m.slug}\n{m.description}\n{m.body}".lower()
        ]
        mems.sort(key=lambda m: (-len(self._contexts.get((m.env, m.slug), [])), m.slug))
        ctxs = [
            c for c in self.store.contexts()
            if (env is None or c.env in (None, env)) and t in f"{c.id}\n{c.body}".lower()
        ]
        return mems + sorted(ctxs, key=lambda c: c.date)

    def typed_edges(self) -> list[Edge]:
        """File edges of any state, then each memory's `[[links]]` as implicit validated related_to edges."""
        out = list(self.store.edges())
        seen = {frozenset((e.source, e.target)) for e in out if e.type == "related_to"}
        for m in self._nodes.values():
            for slug in m.links:
                key = self._target(m.env, slug)
                pair = key and frozenset((m.ref, self._nodes[key].ref))
                if key and key != (m.env, m.slug) and pair not in seen:
                    seen.add(pair)
                    data = {"source": m.ref, "target": self._nodes[key].ref, "type": "related_to",
                            "state": "validated"}
                    out.append(_Implicit(f"link:{m.ref}>{slug}", Path(), m.env, m.repo, data))
        return out

    def neighbours(self, ref: str, states: tuple[str, ...] = ("validated", "proposed")) -> list[tuple[Edge, str]]:
        """(edge, other end) for typed edges touching ref, limited to the given states."""
        return [(e, e.target if e.source == ref else e.source) for e in self.typed_edges()
                if ref in (e.source, e.target) and e.state in states]

    def hot(self, min_count: int = 3) -> list[tuple[Memory, int]]:
        """(memory, independent supporting experiences) with at least min_count, best first.

        Ranked by `evidence.confidence` x that count. A memory without an evidence list counts its contexts instead.
        """
        out = []
        for k, m in self._nodes.items():
            items = m.evidence or [Evidence(c.ref, "supports", 1.0) for c in self._contexts.get(k, [])]
            exps = [e for e in items if e.kind == "supports" and isinstance(self.store.node(e.ref), Experience)]
            n = len({evidence.independence_key(self.store, e) for e in exps})
            out.append((m, n, evidence.confidence(self.store, items) * n))
        out = [p for p in out if p[1] >= min_count]
        return [(m, n) for m, n, _ in sorted(out, key=lambda p: (-p[2], p[0].slug))]
