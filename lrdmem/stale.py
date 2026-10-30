"""Find backticked file paths in memories that no longer exist."""

import re
from pathlib import Path

from lrdmem.lint import ABS_PATH
from lrdmem.store import Memory, Store

_SPAN = re.compile(r"`([^`\s<>*?{}$]+)`")
_REL = re.compile(r"[\w.-]+(?:/[\w.-]+)+|[\w-]+\.\w{1,6}")


def stale(store: Store, repo_root: Path | None, scope: tuple[str, str] | None = None) -> list[tuple[Memory, str]]:
    """(memory, path) pairs for backticked absolute paths that are missing, in every memory, and relative ones missing
    under repo_root, only in the memories of the (env, repo) `scope` folder: other repos' paths mean nothing here."""
    out = []
    for m in store.memories():
        seen: set[str] = set()
        for p in ABS_PATH.findall(m.body):
            if p not in seen and not Path(p).expanduser().exists():
                out.append((m, p))
            seen.add(p)
        if repo_root and scope and (m.env, m.repo) == scope:
            for p in _SPAN.findall(m.body):
                if p not in seen and _REL.fullmatch(p) and not (repo_root / p).exists():
                    out.append((m, p))
                seen.add(p)
    return out
