"""Record a correction: a context (the incident) linked to a memory (the rule)."""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import dump, parse
from lrdmem.lint import KINDS, SOURCES, has_secret
from lrdmem.store import AmbiguousRepo, Store, StoreFormatError, resolve

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_STOP = {"this", "that", "with", "from", "have", "when", "your", "what", "does", "into", "should", "never",
         "always", "using", "used", "about", "there", "their", "which", "would", "could", "than", "then"}
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
    return store.root / e / ("_global" if scope == "env" else repo), e


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


def _touch(path: Path, today: str) -> None:
    """Refresh `updated:` inside `metadata:` when the memory has one; every other line stays byte for byte."""
    try:
        data = parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    meta = data.get("metadata") if data else None
    if isinstance(meta, dict) and "updated" in meta:
        write.set_fields(path, {"updated": today})


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
    try:  # after the text checks: a refused learn leaves no trace, not even the format marker
        for rel in (f"{memory}.md", "contexts", "MEMORY.md"):
            write.target(store, folder, rel)
    except (write.Refused, StoreFormatError) as e:
        raise LearnRefused(str(e)) from None

    try:  # the write layer re-checks secrets, scope and symlinks; a late refusal is still a LearnRefused
        today = date.today().isoformat()
        (folder / "contexts").mkdir(parents=True, exist_ok=True)
        created = False
        if not exists:
            meta = {"type": "feedback", **({"kind": kind} if kind else {}), "author": agent, "updated": today}
            triggers = {k: list(v) for k, v in (("paths", paths), ("commands", commands), ("repos", repos)) if v}
            head = {"name": memory, "description": description, "metadata": meta,
                    **({"triggers": triggers} if triggers else {})}
            text = f"{body.strip()}\n\n**Why:** {why.strip()}\n**How to apply:** {how.strip()}\n"
            created = write.create(path, dump(head) + "\n\n" + text)
            if created:
                write.index_line(folder, f"- [{memory}]({memory}.md) — {description}")
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
            ctx = write.target(store, folder, f"contexts/{cid}.md")
            if write.create(ctx, head + "\n" + text_body):
                return LearnResult(ctx, path, created, dups)
            n += 1
    except write.Refused as e:
        raise LearnRefused(str(e)) from None
