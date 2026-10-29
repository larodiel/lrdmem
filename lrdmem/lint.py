"""Lint a memory store: index format, frontmatter, links, contexts, v2 entities, secrets."""

import math
import os
import re
from datetime import date
from pathlib import Path

from lrdmem import model
from lrdmem.sync import _git, _GitError, nested_git, toplevel
from lrdmem.frontmatter import split, unsupported
from lrdmem.store import (FORMAT_FILE, IGNORED, SUBFOLDERS, SUPPORTED_FORMAT, Edge, Experience, Memory, QueueItem,
                          Revision, Store, StoreFormatError)

TYPES = {"user", "feedback", "project", "reference"}
KINDS = {"obligation", "pattern", "fact"}
CHECK_ON = {"stop", "post-edit"}
SOURCES = {"chat", "pr"}
INDEX_LINE = re.compile(r"^- \[[^\]]+\]\(([a-z0-9-]+\.md)\) — \S")
SECRET = re.compile(
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}"
    r"|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|lin_api_\w{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|npm_[A-Za-z0-9]{36}"  # Stripe secret/restricted (pk_ is public), npm
    r"|hooks\.slack\.com/services/[A-Za-z0-9/]{20,}|ya29\.[A-Za-z0-9_-]{20,}"  # Slack webhook, Google OAuth
    r"|(?i:authorization:\s*bearer\s+)[A-Za-z0-9._~+/-]{16,}"
    r"|(?i:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"]?[^\s'\"`<]{6,}"
)
ABS_PATH = re.compile(r"`((?:/Volumes|/Users|~)/[^`\s<>*]+)`")


def printable(text: str) -> str:
    """Replace control characters (C0, DEL, C1) with '?' so file-derived text cannot inject terminal escapes."""
    return "".join("?" if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F else c for c in text)


def _strings(value):
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _unescape(text: str) -> str:
    """JSON-style escapes decoded: nested maps stay raw strings, so their quoted values keep `\\n`, `\\t`, `\\uXXXX`."""
    return _ESCAPE.sub(lambda m: chr(int(m[1][1:], 16)) if len(m[1]) == 5 else _ESCAPES.get(m[1], m[1]), text)


def has_secret(text: str) -> bool:
    """True when text, or any decoded frontmatter value in it (quoted values may hide escaped newlines/tabs),
    looks like it holds a credential."""
    if SECRET.search(text):
        return True
    try:
        data = split(text)[0]
    except ValueError:
        return False
    return any(SECRET.search(v) or ("\\" in v and SECRET.search(_unescape(v))) for v in _strings(data))


def _is_date(value) -> bool:
    try:
        date.fromisoformat(value if isinstance(value, str) else "")
    except ValueError:
        return False
    return True


_EXTERNAL = ("feedback:", "http://", "https://")  # evidence refs outside the store
_COMPUTED = ("confidence", "priority")


def _computed(*maps: dict) -> list[str]:
    return [f"{k} is computed on read; remove this field" for m in maps for k in _COMPUTED if k in m]


def _is_moment(value) -> bool:
    """YYYY-MM-DD, optionally followed by a T... time."""
    return isinstance(value, str) and _is_date(value[:10]) and (len(value) == 10 or value[10] == "T")


def _resolves(store: Store, ref, env: str | None) -> bool:
    node = store.node(ref) if isinstance(ref, str) else None
    return node is not None and node.env in (None, env)


def _evidence(store: Store, data: dict, env: str | None) -> list[str]:
    raw = data.get("evidence")
    if raw is None or raw == {}:
        return []
    if not isinstance(raw, list):
        return ["evidence must be an inline list of {ref, kind, weight}"]
    out = []
    for item in raw:
        try:
            ev = model.Evidence.from_dict(item) if isinstance(item, dict) else None
            if ev is None:
                raise ValueError("not a {ref, kind, weight} map")
        except ValueError as err:
            out.append(f"invalid evidence item: {err}")
            continue
        if not ev.ref.startswith(_EXTERNAL) and not _resolves(store, ev.ref, env):
            out.append(f"evidence ref '{ev.ref}' does not resolve in its environment or _global")
    return out


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


def _memory(m: Memory, text: str, known: set[str], store: Store) -> list[str]:
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
    if meta.get("memory_type") is not None and meta["memory_type"] not in model.MEMORY_TYPES:
        out.append(f"metadata.memory_type must be one of {list(model.MEMORY_TYPES)}")
    if meta.get("state") is not None and meta["state"] not in model.STATES:
        out.append(f"metadata.state must be one of {list(model.STATES)}")
    out += [f"metadata.{k} is not YYYY-MM-DD" for k in ("valid_from", "valid_until", "created")
            if meta.get(k) is not None and not _is_date(meta[k])]
    out += _evidence(store, m.data, m.env) + _computed(m.data, meta)
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
    for raw in ABS_PATH.findall(text):
        if not Path(os.path.expanduser(raw.rstrip(".,;:)"))).exists():
            out.append(f"names `{raw}`, which no longer exists")
    return out


def _context(c: Experience, text: str, known: set[str], store: Store) -> list[str]:
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
    return out


def _id(item, out: list[str]) -> None:
    if not item.data.get("id"):
        out.append("no id")
    elif item.data["id"] != item.path.stem:
        out.append(f"id '{item.data['id']}' is not '{item.path.stem}'")


def _experience(x: Experience, text: str, known: set[str], store: Store) -> list[str]:
    out = []
    _id(x, out)
    if not _is_moment(x.data.get("date")):
        out.append("date is not YYYY-MM-DD[THH:MM:SSZ]")
    if x.data.get("kind") not in model.EXPERIENCE_KINDS:
        out.append(f"kind must be one of {list(model.EXPERIENCE_KINDS)}")
    if x.source not in model.SOURCES:
        out.append(f"source must be one of {list(model.SOURCES)}")
    return out


def _edge(e: Edge, text: str, known: set[str], store: Store) -> list[str]:
    out = []
    _id(e, out)
    for end in ("source", "target"):
        ref = getattr(e, end)
        if not ref:
            out.append(f"no {end}")
        elif not _resolves(store, ref, e.env):
            out.append(f"{end} '{ref}' does not exist in its environment or _global")
    if e.type not in model.EDGE_TYPES:
        out.append(f"type must be one of {list(model.EDGE_TYPES)}")
    if e.data.get("state") not in model.EDGE_STATES:
        out.append(f"state must be one of {list(model.EDGE_STATES)}")
    if e.data.get("weight") is not None:
        try:
            if not math.isfinite(float(e.data["weight"])):
                raise ValueError
        except (TypeError, ValueError):
            out.append("weight is not a number")
    return out + _evidence(store, e.data, e.env) + _computed(e.data)


def _revision(r: Revision, text: str, known: set[str], store: Store) -> list[str]:
    out = []
    _id(r, out)
    if not r.target:
        out.append("no target")
    elif not _resolves(store, r.target, r.env):
        out.append(f"target '{r.target}' does not exist in its environment or _global")
    if r.action not in model.REVISION_ACTIONS:
        out.append(f"action must be one of {list(model.REVISION_ACTIONS)}")
    for side in ("before", "after"):
        if side in r.data and not isinstance(r.data[side], dict):
            out.append(f"{side} must be a map")
    if not r.by:
        out.append("no by")
    if not _is_moment(r.date):
        out.append("date is not YYYY-MM-DD[THH:MM:SSZ]")
    return out


def _queue(q: QueueItem, text: str, known: set[str], store: Store) -> list[str]:
    out = []
    _id(q, out)
    if not q.kind:
        out.append("no kind")
    if not q.subjects:
        out.append("subjects is empty")
    if q.data.get("status") not in ("open", "resolved"):
        out.append("status must be one of ['open', 'resolved']")
    if q.status == "resolved" and not q.resolution:
        out.append("resolved without a resolution")
    return out


_ENTRIES = {"contexts": (Experience, _context), "experiences": (Experience, _experience), "edges": (Edge, _edge),
            "revisions": (Revision, _revision), "queue": (QueueItem, _queue)}
assert set(_ENTRIES) == set(SUBFOLDERS)


def _edited(store: Store, path: Path, tops: dict) -> bool:
    """True when the file differs from its first git commit; False when untracked or outside git.

    Git runs only in the toplevel found from the trusted environment dir, never inside store content.
    """
    env = store.scope_of(path.parent.parent)[0]
    if env not in tops:
        base = store.root / env if env else store.root
        tops[env] = toplevel(base, store.root.parent)
    top = tops[env]
    try:
        rel = path.resolve().relative_to(top).as_posix() if top else None
        if rel is None:
            return False
        commits = _git(top, "log", "--format=%H", "--follow", "--", rel).split()
        if not commits:
            return False
        return _git(top, "show", f"{commits[-1]}:{rel}", binary=True) != path.read_bytes()
    except (OSError, ValueError, _GitError):
        return False


def _marker(store: Store) -> list[str]:
    try:
        found = store.format()
    except StoreFormatError as err:
        return [f"{FORMAT_FILE}: {err}"]
    if found > SUPPORTED_FORMAT:
        return [f"{FORMAT_FILE}: store format {found} is newer than this lrdmem supports ({SUPPORTED_FORMAT})"]
    if found == 1 and any((f / d).is_dir() for f in store.folders() for d in ("experiences", "edges", "revisions")):
        return [f"{FORMAT_FILE}: missing in a store holding v2 data"]
    return []


def lint(store: Store, env: str | None = None, git_checks: bool = False) -> list[str]:
    """Return one "<path>: <message>" line per problem; empty when the store is clean.

    git_checks also flags experiences edited since their first commit (spawns git; off for hooks).
    """
    # Walk files ourselves: Store skips unreadable and frontmatter-less files, lint must report them.
    stems: dict[str | None, set[str]] = {}
    for folder in store.folders():
        e = store.scope_of(folder)[0]
        stems.setdefault(e, set()).update(p.stem for p in folder.glob("*.md") if p.name not in IGNORED)
    if not store.root.is_dir():
        return [f"{store.root}: store not found"]
    if env and env not in store.environments():
        return [f"{env}: no such environment"]
    problems, tops = _marker(store), {}
    for folder in store.folders(env):
        e, repo = store.scope_of(folder)
        known = stems.get(e, set()) | stems.get(None, set())
        files = {p.name for p in folder.glob("*.md")} - IGNORED
        problems += [f"{p.relative_to(store.root)}: nested git repository inside the store; move it out of the store"
                     for p in nested_git(folder)]
        problems += _index(store, folder, files)
        jobs = [(folder / n, Memory, _memory) for n in sorted(files)]
        for sub, (kind, fn) in _ENTRIES.items():
            jobs += [(p, kind, fn) for p in sorted((folder / sub).glob("*.md"))]
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
                if has_secret(text):
                    problems.append(f"{where}: looks like it holds a credential")
                continue
            problems += [f"{where}:{n}: unsupported frontmatter syntax; use inline lists [a, b]"
                         for n in unsupported(text)]
            item = cls(path.stem, path, e, repo, data, body)
            problems += [f"{where}: {msg}" for msg in check(item, text, known, store)]
            if has_secret(text):
                problems.append(f"{where}: looks like it holds a credential")
            if git_checks and path.parent.name == "experiences" and _edited(store, path, tops):
                problems.append(f"{where}: experience changed since its first commit (experiences are immutable)")
    return problems
