"""Trigger matching: which memories apply to the file or command an agent is touching."""

from collections.abc import Sequence
from fnmatch import fnmatchcase

from lrdmem.graph import Graph
from lrdmem.store import Memory, Store

_KINDS = ["obligation", "pattern", "fact"]


def _strs(value) -> list[str]:
    if value is None or value == {}:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _glob(path: str, pattern: str) -> bool:
    if fnmatchcase(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatchcase(path, pattern[3:])


def matches(memory: Memory, env: str, repo: str, path: str | None = None, command: str | None = None) -> bool:
    """True when the memory's scope fits env/repo and a paths glob or commands prefix matches."""
    if memory.env not in (None, env) or memory.repo not in (None, repo):
        return False
    t = memory.triggers
    repos = _strs(t.get("repos"))
    if repos and repo not in repos:
        return False
    if memory.env is not None and memory.repo is None and not repos:
        return False  # <env>/_global/ needs an explicit repos list
    if path is not None and any(_glob(path, g) for g in _strs(t.get("paths"))):
        return True
    return command is not None and any(command.startswith(c) for c in _strs(t.get("commands")))


def render(memories: list[Memory], target: str = "") -> str:
    """Markdown block injected into the agent context."""
    parts = [f"## lrdmem: rules for {target}".rstrip()]
    parts += [f"### {m.slug}\n{m.body.strip()}" for m in memories]
    return "\n\n".join(parts) + "\n"


def select(store: Store, env: str, repo: str, paths: Sequence[str] = (), command: str | None = None,
           exclude: set[str] | frozenset[str] = frozenset(), budget: int = 1500) -> list[Memory]:
    """Memories matching any of paths or command, obligations first then strongest, cut at the budget."""
    graph = Graph(store)
    found = [m for m in store.memories()
             if m.slug not in exclude and (any(matches(m, env, repo, path=p) for p in paths)
                                           or matches(m, env, repo, command=command))]
    found.sort(key=lambda m: (_KINDS.index(m.kind) if m.kind in _KINDS else len(_KINDS),
                              -graph.strength(m), m.slug))
    chosen: list[Memory] = []
    for m in found:
        if len(render(chosen + [m], ", ".join(paths) or command or "")) // 4 > budget:
            break
        chosen.append(m)
    return chosen
