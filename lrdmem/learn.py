"""Record a correction: a context (the incident) linked to a memory (the rule)."""

import fcntl
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


def _terms(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _STOP}


def _folder(store: Store, cwd: Path, scope: str, env: str | None) -> tuple[Path, str | None]:
    """The target folder and the env it is visible from."""
    if scope == "global":
        return store.root / "_global", None
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
    mine = _terms(slug.replace("-", " ") + " " + description)
    return [m.slug for m in store.memories() if m.env in (None, env)
            and len(mine & _terms(m.slug.replace("-", " ") + " " + m.description)) >= 2]


def _create(path: Path, text: str) -> bool:
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        return False
    return True


def _touch(path: Path, today: str) -> None:
    # ponytail: read-modify-write without a lock; a concurrent edit of the same memory can be lost.
    data, body = split(path.read_text(encoding="utf-8"))
    if data is None:
        return
    if not isinstance(data.get("metadata"), dict):
        data["metadata"] = {}
    data["metadata"]["updated"] = today
    path.write_text(dump(data) + "\n" + body, encoding="utf-8")


def _index(folder: Path, line: str) -> None:
    with open(folder / "MEMORY.md", "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        text = f.read()
        if line not in text.splitlines():
            f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")


def learn(store: Store, cwd: Path, *, memory: str, mistake: str, correction: str, scope: str, agent: str,
          source: str = "chat", files: list[str] = (), pr: str | None = None, kind: str | None = None,
          description: str | None = None, body: str | None = None, env: str | None = None,
          new: bool = False, device: str = "") -> LearnResult:
    """Write a context for `memory`, creating the memory when absent; raise LearnRefused on invalid input."""
    if not _SLUG.match(memory):
        raise LearnRefused("memory slug must match ^[a-z0-9][a-z0-9-]{0,63}$")
    if scope not in _SCOPES:
        raise LearnRefused(f"scope must be one of {list(_SCOPES)}")
    if source not in SOURCES:
        raise LearnRefused(f"source must be one of {sorted(SOURCES)}")
    if kind is not None and kind not in KINDS:
        raise LearnRefused(f"kind must be one of {sorted(KINDS)}")
    if not mistake.strip() or not correction.strip() or not agent.strip():
        raise LearnRefused("mistake, correction and agent are required")
    oneline = {"agent": agent, "device": device, "pr": pr or "", "description": description or "",
               **{f"files[{i}]": f for i, f in enumerate(files)}}
    if any("\n" in v or "\r" in v for v in oneline.values()):
        raise LearnRefused("agent, device, pr, description and files must be single-line")
    folder, vis_env = _folder(store, cwd, scope, env)
    path = folder / f"{memory}.md"
    exists = path.exists()
    if not exists and not (description and description.strip() and body and body.strip()):
        raise LearnRefused(f"memory '{memory}' does not exist in this scope; description and body are required")
    _check_text(store, {"memory": memory, "mistake": mistake, "correction": correction, "body": body or "",
                        **oneline}, scope, vis_env, memory)
    dups = [] if exists else _duplicates(store, vis_env, memory, description)
    if dups and not new:
        raise LearnRefused("similar memories exist (use --new to override): " + ", ".join(dups), dups)

    today = date.today().isoformat()
    (folder / "contexts").mkdir(parents=True, exist_ok=True)
    created = False
    if not exists:
        meta = {"type": "feedback", **({"kind": kind} if kind else {}), "author": agent, "updated": today}
        created = _create(path, dump({"name": memory, "description": description, "metadata": meta})
                          + "\n\n" + body.strip() + "\n")
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
