"""Link graph over a store: memories reinforced by contexts and tied together by links."""

from collections import defaultdict

from lrdmem.store import Context, Memory, Store

_Key = tuple[str | None, str]


class AmbiguousSlug(Exception):
    """Raised when a slug exists in several environments and no env was given."""

    def __init__(self, slug: str, candidates: list[Memory]):
        super().__init__(f"'{slug}' exists in several environments; pass --env")
        self.candidates = candidates


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

    def _key(self, slug: str, env: str | None) -> _Key | None:
        m = self.resolve(slug, env)
        return (m.env, m.slug) if m else None

    def strength(self, slug: str, env: str | None = None) -> int:
        """Number of contexts linking the memory."""
        return len(self._contexts.get(self._key(slug, env), []))

    def contexts_of(self, slug: str, env: str | None = None) -> list[Context]:
        """Contexts linking the memory, oldest first."""
        return list(self._contexts.get(self._key(slug, env), []))

    def related(self, slug: str, env: str | None = None) -> list[str]:
        """Slugs linked to or from the memory, superseding/superseded by it, or sharing a context."""
        return sorted({s for _, s in self._edges.get(self._key(slug, env), set())})

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

    def hot(self, min_count: int = 3) -> list[tuple[Memory, int]]:
        """Memories reinforced by at least min_count contexts, strongest first."""
        out = [(m, len(self._contexts.get(k, []))) for k, m in self._nodes.items()]
        return sorted((p for p in out if p[1] >= min_count), key=lambda p: (-p[1], p[0].slug))
