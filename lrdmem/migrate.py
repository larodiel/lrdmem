"""Bring memories written before the v2 model up to what lint and hooks expect, one reviewable step at a time.

Two repairs, both through `write.py`: `author`/`updated`/`type`/`kind`/`created` written outside `metadata:` move
under it, and (opt-in) `triggers.paths` are inferred from the file paths a memory names in backticks. A memory's
explicit `memory_type`/`state` are not written: readers already default them.
"""

import re
from dataclasses import dataclass, field

from lrdmem import write
from lrdmem.lint import TICKET
from lrdmem.store import Memory, Store
from lrdmem.triggers import strs

META_KEYS = ["type", "kind", "author", "created", "updated"]
LARGE = 4000  # body characters past which a memory is probably several rules
MAX_PATHS = 5

_SPAN = re.compile(r"`([^`\s]+)`")
_FILE = re.compile(r"^[\w.-]+(?:/[\w.-]+)*$")
_EXT = re.compile(r"\.(?:php|js|jsx|ts|tsx|mjs|json|md|ya?ml|css|scss|html|txt|xml|sh|py|patch|lock|neon|dist|snap)$")
_DOTFILES = {".distfiles", ".distignore", ".gitignore", ".editorconfig", ".env"}


@dataclass
class Change:
    """What would change in one memory."""

    memory: Memory
    hoist: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    large: bool = False


def infer_paths(body: str) -> list[str]:
    """Globs for the repo-relative files and folders a memory names in backticks, in order, at most MAX_PATHS.

    A glob with a folder or an extension stays as written (`VALID_*` or `with_*()` are names, not paths); `dir/`
    becomes `**/dir/**`; a file with a known extension, or a dotfile such as `.puprc`, becomes `**/file`. Absolute,
    home and URL-like spans, ticket-named files and words such as and/or are skipped.
    """
    out: list[str] = []
    for span in _SPAN.findall(body):
        if span.startswith(("/", "~", "$")) or "://" in span or "::" in span or "..." in span or TICKET.search(span):
            continue
        name = span.rsplit("/", 1)[-1]
        if "*" in span:
            if not re.fullmatch(r"[\w.*/-]+", span) or not ("/" in span or _EXT.search(span)) or "(" in span:
                continue
            glob = span
        elif span.endswith("/") and _FILE.match(span.rstrip("/")):
            glob = f"**/{span}**"
        elif _FILE.match(span) and ((_EXT.search(name) and (not name.startswith(".") or name.count(".") > 1)) or name in _DOTFILES or (name.startswith(".") and name.endswith("rc"))):
            glob = f"**/{span}"
        else:
            continue
        if glob not in out:
            out.append(glob)
    return out[:MAX_PATHS]


def _injectable(m: Memory) -> bool:
    return m.repo is not None or bool(strs(m.triggers.get("repos")))


def plan(store: Store, env: str | None = None, infer: bool = False) -> list[Change]:
    """The changes migrate would make, memory by memory; empty when nothing needs migrating."""
    out = []
    for m in store.memories():
        if env and m.env not in (None, env):
            continue
        top = [k for k in META_KEYS if k in m.data and k != "metadata"]
        has = strs(m.triggers.get("paths")) or strs(m.triggers.get("commands"))
        paths = infer_paths(m.body) if infer and not has and _injectable(m) else []
        change = Change(m, hoist=top, paths=paths, large=len(m.body) > LARGE)
        if change.hoist or change.paths or change.large:
            out.append(change)
    return out


def apply(store: Store, changes: list[Change]) -> list[str]:
    """Write the changes; return the refs that could not be changed (an unparseable or clashing header)."""
    failed = []
    for c in changes:
        ok = not c.hoist or write.move_into_metadata(c.memory.path, c.hoist)
        ok = ok and (not c.paths or write.add_triggers(c.memory.path, {"paths": c.paths}))
        if not ok:
            failed.append(c.memory.ref)
    return failed
