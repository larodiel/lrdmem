"""Lint a memory store: index format, frontmatter, links, contexts, secrets."""

import os
import re
from datetime import date
from pathlib import Path

from lrdmem.frontmatter import split
from lrdmem.store import IGNORED, Context, Memory, Store

TYPES = {"user", "feedback", "project", "reference"}
KINDS = {"obligation", "pattern", "fact"}
CHECK_ON = {"stop", "post-edit"}
SOURCES = {"chat", "pr"}
INDEX_LINE = re.compile(r"^- \[[^\]]+\]\(([a-z0-9-]+\.md)\) — \S")
SECRET = re.compile(
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}"
    r"|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|lin_api_\w{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"]?[^\s'\"`<]{6,}"
)
ABS_PATH = re.compile(r"`((?:/Volumes|/Users|~)/[^`\s<>*]+)`")


def printable(text: str) -> str:
    """Replace control characters (C0, DEL, C1) with '?' so file-derived text cannot inject terminal escapes."""
    return "".join("?" if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F else c for c in text)


def has_secret(text: str) -> bool:
    """True when text looks like it holds a credential."""
    return bool(SECRET.search(text))


def _is_date(value) -> bool:
    try:
        date.fromisoformat(value if isinstance(value, str) else "")
    except ValueError:
        return False
    return True


def _index(store: Store, folder: Path, files: set[str]) -> list[str]:
    rel = folder.relative_to(store.root)
    index = folder / "MEMORY.md"
    if not index.exists():
        return [f"{rel}/MEMORY.md: missing MEMORY.md"]
    problems, indexed = [], set()
    for n, line in enumerate(index.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        m = INDEX_LINE.match(line)
        if not m:
            problems.append(f"{rel}/MEMORY.md:{n}: not '- [Title](slug.md) — hook'")
            continue
        indexed.add(m.group(1))
        if m.group(1) not in files:
            problems.append(f"{rel}/MEMORY.md:{n}: points to missing {m.group(1)}")
    problems += [f"{rel}/{name}: not listed in MEMORY.md" for name in sorted(files - indexed)]
    return problems


def _memory(m: Memory, text: str, known: set[str]) -> list[str]:
    meta = m.data.get("metadata") if isinstance(m.data.get("metadata"), dict) else {}
    out = []
    if m.data.get("name") != m.slug:
        out.append(f"name is not '{m.slug}'")
    if not m.description:
        out.append("no description")
    if m.type not in TYPES:
        out.append(f"metadata.type must be one of {sorted(TYPES)}")
    if meta.get("kind") is not None and meta["kind"] not in KINDS:
        out.append(f"metadata.kind must be one of {sorted(KINDS)}")
    if not meta.get("author"):
        out.append("no metadata.author")
    if not _is_date(meta.get("updated")):
        out.append("metadata.updated is not YYYY-MM-DD")
    if "**Why:**" not in text or "**How to apply:**" not in text:
        out.append("needs **Why:** and **How to apply:** lines")
    if "check" in m.data:
        c = m.check
        if m.kind != "obligation":
            out.append("check is only allowed on kind: obligation")
        if c is None or not c.get("run"):
            out.append("check.run is missing")
        if c is None or c.get("on") not in CHECK_ON:
            out.append(f"check.on must be one of {sorted(CHECK_ON)}")
    out += [f"[[{t}]] does not resolve in its environment or _global" for t in m.links if t not in known]
    if has_secret(text):
        out.append("looks like it holds a credential")
    for raw in ABS_PATH.findall(text):
        if not Path(os.path.expanduser(raw.rstrip(".,;:)"))).exists():
            out.append(f"names `{raw}`, which no longer exists")
    return out


def _context(c: Context, text: str, known: set[str]) -> list[str]:
    out = []
    if not c.data.get("id"):
        out.append("no id")
    if not _is_date(c.data.get("date")):
        out.append("date is not YYYY-MM-DD")
    if c.data.get("source") not in SOURCES:
        out.append(f"source must be one of {sorted(SOURCES)}")
    if not c.memories:
        out.append("memories is empty")
    out += [f"memory '{s}' does not exist in its environment or _global" for s in c.memories if s not in known]
    if has_secret(text):
        out.append("looks like it holds a credential")
    return out


def lint(store: Store, env: str | None = None) -> list[str]:
    """Return one "<path>: <message>" line per problem; empty when the store is clean."""
    # Walk files ourselves: Store skips unreadable and frontmatter-less files, lint must report them.
    stems: dict[str | None, set[str]] = {}
    for folder in store.folders():
        e = store.scope_of(folder)[0]
        stems.setdefault(e, set()).update(p.stem for p in folder.glob("*.md") if p.name not in IGNORED)
    if not store.root.is_dir():
        return [f"{store.root}: store not found"]
    if env and env not in store.environments():
        return [f"{env}: no such environment"]
    problems = []
    for folder in store.folders(env):
        e, repo = store.scope_of(folder)
        known = stems.get(e, set()) | stems.get(None, set())
        files = {p.name for p in folder.glob("*.md")} - IGNORED
        problems += _index(store, folder, files)
        jobs = [(folder / n, Memory, _memory) for n in sorted(files)]
        jobs += [(p, Context, _context) for p in sorted((folder / "contexts").glob("*.md"))]
        for path, cls, check in jobs:
            where = path.relative_to(store.root)
            try:
                text = path.read_text(encoding="utf-8")
                data, body = split(text)
            except (OSError, ValueError) as err:
                problems.append(f"{where}: unreadable ({err.__class__.__name__})")
                continue
            if data is None:
                problems.append(f"{where}: no frontmatter")
                continue
            item = cls(path.stem, path, e, repo, data, body)
            problems += [f"{where}: {msg}" for msg in check(item, text, known)]
    return problems
