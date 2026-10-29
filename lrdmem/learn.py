"""Record a correction: a context (the incident) linked to a memory (the rule)."""

import fcntl
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from lrdmem.frontmatter import dump, split
from lrdmem.lint import KINDS, SOURCES, has_secret
from lrdmem.store import AmbiguousRepo, Store, resolve

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_STOP = {"this", "that", "with", "from", "have", "when", "your", "what", "does", "into", "should", "never",
         "always", "using", "used", "about", "there", "their", "which", "would", "could", "than", "then"}
_UPDATED = re.compile(r"^(\s+updated:\s*).*?(\r?)$")
_RESERVED = {"memory", "readme", "contexts"}
_SCOPES = ("repo", "env", "global")


class LearnRefused(Exception):
    """Raised when a learn request fails validation; duplicates lists likely existing memories."""

    def __init__(self, message: str, duplicates: list[str] | None = None):
        super().__init__(message)
        self.duplicates = duplicates or []


@dataclass
class LearnResult:
    """What learn wrote: the new context, the memory it reinforces, and near-duplicates seen."""

    context: Path
    memory: Path
    created: bool
    duplicates: list[str] = field(default_factory=list)


def terms(text: str) -> set[str]:
    """Lowercase words of 4+ characters, minus stop words; the basis of duplicate and PR matching."""
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _STOP}


def _folder(store: Store, cwd: Path, scope: str, env: str | None) -> tuple[Path, str | None]:
    """The target folder and the env it is visible from."""
    if scope == "global":
        return store.root / "_global", None
    if env is not None and env not in store.environments():
        raise LearnRefused(f"unknown environment '{env}'")
    try:
        found = resolve(store, cwd, env)
    except AmbiguousRepo as e:
        raise LearnRefused(str(e)) from None
    if not found:
        raise LearnRefused("this repository is not opted in to the store; use scope global or create its folder")
    e, repo = found
    folder = store.root / e / ("_global" if scope == "env" else repo)
    if not folder.resolve().is_relative_to((store.root / e).resolve()):  # env dirs may be symlinks; folders may not leave them
        raise LearnRefused("target folder resolves outside its environment")
    return folder, e


def _check_text(store: Store, fields: dict[str, str], scope: str, env: str | None, slug: str) -> None:
    for name, text in fields.items():
        if has_secret(text):
            raise LearnRefused(f"{name} looks like it holds a credential")
    if scope == "global":
        return
    if str(Path.home()) in "\n".join(fields.values()):
        raise LearnRefused("text contains the home directory path, which must not enter a shared environment")
    known = {m.slug for m in store.memories() if m.env in (None, env)} | {slug}
    for link in _LINK.findall("\n".join(fields.values())):
        if link not in known:
            raise LearnRefused(f"[[{link}]] does not resolve in the environment or _global")


def _duplicates(store: Store, env: str | None, slug: str, description: str) -> list[str]:
    mine = terms(slug.replace("-", " ") + " " + description)
    return [m.slug for m in store.memories() if m.env in (None, env)
            and len(mine & terms(m.slug.replace("-", " ") + " " + m.description)) >= 2]


def _create(path: Path, text: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        return False
    return True


def _touch(path: Path, today: str) -> None:
    """Rewrite only the `updated:` line inside `metadata:`; anything else, or no such line, stays byte for byte."""
    # ponytail: read-modify-write without a lock; a concurrent edit of the same memory can be lost.
    with open(path, encoding="utf-8", newline="") as f:  # keep CRLF files CRLF
        text = f.read()
    if split(text)[0] is None:
        return
    lines = text.split("\n")
    in_meta = False
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip("\r") == "---":
            return  # end of the header: no updated line in metadata
        if not line.strip():
            continue
        if line[:1] not in (" ", "\t"):
            key, colon, rest = line.partition(":")
            in_meta = bool(colon) and key.strip() == "metadata" and not rest.strip()
        elif in_meta:
            m = _UPDATED.match(line)
            if m:
                lines[i] = f"{m.group(1)}{today}{m.group(2)}"
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write("\n".join(lines))
                return


def _index(folder: Path, line: str) -> None:
    fd = os.open(folder / "MEMORY.md", os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        text = f.read()
        if line not in text.splitlines():
            f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")


def learn(store: Store, cwd: Path, *, memory: str, mistake: str, correction: str, scope: str, agent: str,
          source: str = "chat", files: list[str] = (), pr: str | None = None, kind: str | None = None,
          description: str | None = None, body: str | None = None, why: str | None = None, how: str | None = None,
          paths: list[str] = (), commands: list[str] = (), repos: list[str] = (), env: str | None = None,
          new: bool = False, device: str = "") -> LearnResult:
    """Write a context for `memory`, creating the memory when absent; raise LearnRefused on invalid input.

    A new memory needs description, body, why and how; paths, commands and repos become its triggers.
    """
    if not _SLUG.match(memory) or memory in _RESERVED:
        raise LearnRefused("memory slug must match ^[a-z0-9][a-z0-9-]{0,63}$ and not be memory, readme or contexts")
    if scope not in _SCOPES:
        raise LearnRefused(f"scope must be one of {list(_SCOPES)}")
    if source not in SOURCES:
        raise LearnRefused(f"source must be one of {sorted(SOURCES)}")
    if kind is not None and kind not in KINDS:
        raise LearnRefused(f"kind must be one of {sorted(KINDS)}")
    if not mistake.strip() or not correction.strip() or not agent.strip():
        raise LearnRefused("mistake, correction and agent are required")
    oneline = {"agent": agent, "device": device, "pr": pr or "", "description": description or "",
               **{f"files[{i}]": f for i, f in enumerate(files)},
               **{f"{name}[{i}]": v for name, vals in (("paths", paths), ("commands", commands), ("repos", repos))
                  for i, v in enumerate(vals)}}
    if any("\n" in v or "\r" in v for v in oneline.values()):
        raise LearnRefused("agent, device, pr, description, files and triggers must be single-line")
    folder, vis_env = _folder(store, cwd, scope, env)
    path = folder / f"{memory}.md"
    for guard in (path, folder / "contexts", folder / "MEMORY.md"):
        if guard.is_symlink():
            raise LearnRefused(f"{guard.name} is a symlink; refusing to write through it")
    exists = path.exists()
    if not exists and not all(v and v.strip() for v in (description, body, why, how)):
        raise LearnRefused(f"memory '{memory}' does not exist in this scope; description, body, why and how "
                           "are required")
    _check_text(store, {"memory": memory, "mistake": mistake, "correction": correction, "body": body or "",
                        "why": why or "", "how": how or "", **oneline}, scope, vis_env, memory)
    if not exists:
        for m in store.memories():
            if m.slug == memory and m.path != path and (scope == "global" or m.env in (None, vis_env)):
                raise LearnRefused(f"memory '{memory}' already exists at {m.path.relative_to(store.root)}")
    dups = [] if exists else _duplicates(store, vis_env, memory, description)
    if dups and not new:
        raise LearnRefused("similar memories exist (use --new to override): " + ", ".join(dups), dups)

    today = date.today().isoformat()
    (folder / "contexts").mkdir(parents=True, exist_ok=True)
    created = False
    if not exists:
        meta = {"type": "feedback", **({"kind": kind} if kind else {}), "author": agent, "updated": today}
        triggers = {k: list(v) for k, v in (("paths", paths), ("commands", commands), ("repos", repos)) if v}
        head = {"name": memory, "description": description, "metadata": meta,
                **({"triggers": triggers} if triggers else {})}
        text = f"{body.strip()}\n\n**Why:** {why.strip()}\n**How to apply:** {how.strip()}\n"
        created = _create(path, dump(head) + "\n\n" + text)
        if created:
            _index(folder, f"- [{memory}]({memory}.md) — {description}")
    if not created:
        _touch(path, today)

    repo = store.scope_of(folder)[1]
    data = {"id": "", "date": today, "agent": agent, "device": device, "repo": repo, "files": list(files),
            "source": source, "pr": pr, "memories": [memory]}
    text_body = f"\n**Mistake:** {mistake.strip()}\n\n**Correction:** {correction.strip()}\n"
    n = 1
    while True:
        cid = f"{today}-{memory}" + (f"-{n}" if n > 1 else "")
        data["id"] = cid
        head = dump({k: v for k, v in data.items() if v not in (None, "", [])})
        ctx = folder / "contexts" / f"{cid}.md"
        if _create(ctx, head + "\n" + text_body):
            return LearnResult(ctx, path, created, dups)
        n += 1
