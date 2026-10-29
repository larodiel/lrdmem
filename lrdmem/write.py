"""The single write path of the store: scoped targets, exclusive creates, line-level edits and the secret guard.

Every writer gets its path from `target` (which also runs the store format guard) and writes through the functions
here, so the security invariants hold in one place: nothing matching `lint.SECRET` is ever written, files are
created with O_EXCL|O_NOFOLLOW, edits never follow symlinks and never re-dump a file.
"""

import fcntl
import os
import re
import threading
from pathlib import Path
from typing import Callable

from lrdmem.frontmatter import dump, split
from lrdmem.lint import has_secret
from lrdmem.model import Evidence
from lrdmem.store import SUBFOLDERS, Store

FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMPUTED = ("confidence", "priority")
_FORMAT_OK: set[Path] = set()  # ponytail: per-process cache; a marker bumped mid-process is seen by the next run
_FORMAT_LOCK = threading.Lock()  # ensure_format's temp name is per-pid, so threads must not race it


class Refused(ValueError):
    """Raised when a write would break a store invariant: a secret, a path escaping its scope, a symlink."""


def guard(text: str) -> None:
    """Raise Refused when text looks like it holds a credential; the central barrier of every write."""
    if has_secret(text):
        raise Refused("content looks like it holds a credential; sensitive data never enters the store")


def refuse_home_paths(text: str, scope: str) -> None:
    """Raise Refused when text holds the home directory path and the scope is shared (anything but global)."""
    if scope != "global" and str(Path.home()) in text:
        raise Refused("text contains the home directory path, which must not enter a shared environment")


def target(store: Store, scope_folder: Path, rel: str) -> Path:
    """The path `rel` inside a scope folder, after the format guard; Refused when it could write outside the scope.

    The folder must be `_global`, `<env>/_global` or `<env>/<repo>` and resolve inside its environment (env dirs may
    be symlinks; folders may not leave them); `rel` is relative, without `..`, and no existing part is a symlink.
    StoreFormatError when the store is newer than this lrdmem.
    """
    with _FORMAT_LOCK:
        if store.root not in _FORMAT_OK:
            store.ensure_format()
            _FORMAT_OK.add(store.root)
    folder = Path(scope_folder)
    try:
        parts = folder.relative_to(store.root).parts
    except ValueError:
        raise Refused("target folder is outside the store") from None
    if not (parts == ("_global",) or (len(parts) == 2 and parts[0] in store.environments()
                                      and parts[1] not in SUBFOLDERS and parts[1] not in ("", ".", ".."))):
        raise Refused("target folder is not a scope folder of the store")
    if not folder.resolve().is_relative_to((store.root / parts[0]).resolve()):
        raise Refused("target folder resolves outside its environment")
    guard(rel)  # file names are store content too
    names = rel.split("/")
    if not rel or rel.startswith("/") or any(n in ("", ".", "..") for n in names):
        raise Refused(f"invalid path inside the scope folder: {rel!r}")
    cur = folder
    for name in names:
        cur = cur / name
        if cur.is_symlink():
            raise Refused(f"{name} is a symlink; refusing to write through it")
    return cur


def create(path: Path, text: str) -> bool:
    """Create path with text, complete the moment it becomes visible; False when it already exists.

    The text goes to a hidden temp file in the same folder (O_EXCL|O_NOFOLLOW), is fsynced and hard-linked to path
    (which fails if path exists, a dangling symlink included); readers never see an empty or partial file and a crash
    leaves no claim on the name. Without hard links it falls back to a direct O_EXCL|O_NOFOLLOW write.
    Refused on a secret.
    """
    return create_new(path, text) is not None


def create_new(path: Path, text: str) -> os.stat_result | None:
    """Like `create`, but returns the new file's stat (its identity, for `discard`) or None when it already exists."""
    guard(text)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        with os.fdopen(os.open(tmp, flags, 0o644), "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            st = os.fstat(f.fileno())
        try:
            os.link(tmp, path)
            return st
        except FileExistsError:
            return None
        except OSError:
            pass  # ponytail: no hard links on this filesystem; the direct write below can show a partial file
    except OSError:
        pass  # the temp file could not be made (read-only folder, ...): the direct write reports the real error
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        return os.fstat(f.fileno())


def discard(store: Store, scope_folder: Path, rel: str, created: os.stat_result) -> bool:
    """Delete `rel` in a scope folder only if it is still the file `create_new` returned `created` for.

    Resolved through `target` (store, scope and symlink checks, Refused on violation); False, deleting nothing,
    when the file is gone or its (device, inode) differs, i.e. it was replaced since.
    """
    path = target(store, scope_folder, rel)
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return False
    if (now.st_dev, now.st_ino) != (created.st_dev, created.st_ino):
        return False
    os.unlink(path)
    return True


def unique(path_stem: Path) -> Path:
    """The first free `<stem>.md`, `<stem>-2.md`, `<stem>-3.md`, ...; pair with `create`, which stays exclusive."""
    path, n = path_stem.with_name(f"{path_stem.name}.md"), 1
    while os.path.lexists(path):
        n += 1
        path = path_stem.with_name(f"{path_stem.name}-{n}.md")
    return path


def index_line(folder: Path, line: str) -> None:
    """Append line to folder/MEMORY.md unless already present, under an exclusive lock."""
    guard(line)
    if len(line.splitlines()) > 1 or "\n" in line or "\r" in line:  # splitlines: also U+2028, \x85, \x0b...
        raise Refused("an index line must be a single line")
    fd = os.open(folder / "MEMORY.md", os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        text = f.read()
        if line not in text.splitlines():
            f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")


def _edit(path: Path, change: Callable[[str], str | None]) -> bool:
    """Apply change to the file's text under a lock (never through a symlink); False when change returns None."""
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+", encoding="utf-8", newline="") as f:  # newline="" keeps CRLF files CRLF
        fcntl.flock(f, fcntl.LOCK_EX)
        text = f.read()
        new = change(text)
        if new is None:
            return False
        if new != text:
            f.seek(0)
            f.write(new)
            f.truncate()
    return True


def _scan(lines: list[str]) -> tuple[int, dict, dict, int | None] | None:
    """(closing --- index, top-level key → line, metadata key → line, last metadata line) of a header."""
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.rstrip("\r") == "---"), None)
    if end is None:
        return None
    top, meta, meta_end, section = {}, {}, None, None
    for i in range(1, end):
        line = lines[i].rstrip("\r")
        if not line.strip():
            continue
        key, colon, rest = line.strip().partition(":")
        if line[0] not in " \t":
            section = key if colon and not rest.strip() else None
            top[key] = i
            if section == "metadata":
                meta_end = i
        elif section == "metadata":
            meta[key.strip()] = i
            meta_end = i
    return end, top, meta, meta_end


def _value(data: dict, key: str, memory: bool):
    meta = data.get("metadata") if memory and isinstance(data.get("metadata"), dict) else {}
    return meta[key] if key in meta else data.get(key, "")


def _set(text: str, changes: dict[str, str], memory: bool, expect: dict[str, str]) -> str | None:
    data = split(text)[0]
    lines = text.split("\n")
    scan = _scan(lines) if data is not None else None
    if scan is None:
        return None
    if any(_value(data, k, memory) != v for k, v in expect.items()):
        raise Refused("the file changed since it was read; nothing was written")
    cr = "\r" if lines[0].endswith("\r") else ""
    for key, value in changes.items():
        end, top, meta, meta_end = _scan(lines)
        out = dump({key: value}).split("\n")[1]
        at = meta.get(key, top.get(key)) if memory else top.get(key)
        if at is not None:
            old = lines[at].rstrip("\r")
            rest = old.partition(":")[2].strip()
            if not rest or rest[0] in "[|>":
                return None  # a section, list or block scalar: not a scalar line we can safely replace
            lines[at] = old[:len(old) - len(old.lstrip())] + out + cr
        elif memory and meta_end is not None:
            first = lines[min(meta.values())] if meta else "  "
            lines.insert(meta_end + 1, first[:len(first) - len(first.lstrip())] + out + cr)
        elif memory:
            lines[end:end] = ["metadata:" + cr, "  " + out + cr]
        else:
            lines.insert(end, out + cr)
    new = "\n".join(lines)
    data = split(new)[0] or {}
    if any(_value(data, k, memory) != v for k, v in changes.items()):
        return None  # the edit would not read back as written
    return new


def set_fields(path: Path, changes: dict[str, str], expect: dict[str, str] | None = None) -> bool:
    """Set scalar frontmatter fields line by line; never re-dumps, never touches other lines, keeps CRLF.

    An existing key is replaced in place (a memory's metadata key first, else top level); a missing key goes at the
    end of a memory's `metadata:` section (created when absent), or at the top level of an edge, experience or
    revision. False, writing nothing, when the header is unparseable, a key is invalid, or the key's line is a
    section, list or block scalar. Refused on a secret or on `confidence`/`priority` (computed on read), and when
    `expect` is given and the current values (an absent field reads as "") differ, checked under the file lock.
    """
    changes = {k: str(v) for k, v in changes.items()}
    if any(k in _COMPUTED for k in changes):
        raise Refused("confidence and priority are computed on read and never written")
    for k, v in changes.items():
        guard(f"{k}: {v}")
    if not all(FIELD.match(k) for k in changes):
        return False
    memory = path.parent.name not in SUBFOLDERS
    expect = {k: str(v) for k, v in (expect or {}).items()}
    return _edit(path, lambda text: _set(text, changes, memory, expect))


def _append(text: str, item: dict, rendered: str) -> str | None:
    data = split(text)[0]
    lines = text.split("\n")
    scan = _scan(lines) if data is not None else None
    if scan is None:
        return None
    old = data.get("evidence")
    if old is None:
        old = []
    elif not isinstance(old, list):
        return None  # a block list or section: not ours to edit
    if any(isinstance(d, dict) and (d.get("ref"), d.get("kind")) == (item["ref"], item["kind"]) for d in old):
        return None
    cr = "\r" if lines[0].endswith("\r") else ""
    end, top = scan[0], scan[1]
    if "evidence" in top:
        line = lines[top["evidence"]].rstrip("\r")
        head, _, rest = line.partition(":")
        inner = rest.strip()[1:-1].strip()
        lines[top["evidence"]] = f"{head}: [{inner}{', ' if inner else ''}{rendered}]{cr}"
    else:
        lines.insert(end, f"evidence: [{rendered}]{cr}")
    new = "\n".join(lines)
    return new if (split(new)[0] or {}).get("evidence") == old + [item] else None


def add_evidence(path: Path, item: Evidence) -> bool:
    """Append item to the inline `evidence: [...]` line (created when absent) without touching other lines.

    False, writing nothing, when an item with the same ref and kind exists, the header is unparseable, or evidence
    is not an inline list. Refused on a secret.
    """
    entry = item.to_dict()
    guard("\n".join(entry.values()))
    rendered = dump({"e": [entry]}).split("\n")[1][len("e: ["):-1]
    guard(rendered)
    return _edit(path, lambda text: _append(text, entry, rendered))
