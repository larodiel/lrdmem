"""Record a correction: an experience (the incident) that creates or reinforces a memory (the rule).

The experience is a `correction` (a `review` when the source is a PR); the memory goes through `propose.apply`, so a
new one starts active with the experience as supporting evidence and an existing one gains that evidence.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import parse
from lrdmem.lint import KINDS, SOURCES, TICKET, _is_date, has_secret
from lrdmem.model import today_utc, v01_memory_type
from lrdmem.search import alike
from lrdmem.store import AmbiguousRepo, Experience, Store, StoreFormatError, resolve
from lrdmem.triggers import glob_match, strs

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
    """What learn wrote: the new experience (`context`, its v0.1 name), the memory it reinforces, and near-duplicates
    seen."""

    context: Path
    memory: Path
    created: bool
    duplicates: list[str] = field(default_factory=list)
    hint: str = ""  # when a correction reinforces an existing rule: why it was broken and what would stop that


def terms(text: str) -> set[str]:
    """Lowercase words of 4+ characters, minus stop words; the basis of duplicate and PR matching."""
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _STOP}


def scope_folder(store: Store, cwd: Path, scope: str, env: str | None) -> tuple[Path, str | None]:
    """The scope folder for repo/env/global and the env it is visible from; LearnRefused when not opted in."""
    if scope == "global":
        return store.root / "_global", None
    if env is not None and env not in store.environments():
        raise LearnRefused(f"unknown environment '{env}'")
    try:
        found = write.resolve_or_enroll(store, cwd, env)
    except AmbiguousRepo as e:
        raise LearnRefused(str(e)) from None
    if not found:
        raise LearnRefused("this repository is not registered in the store; ask the user, then run `lrdmem enroll --env ENV` "
                           "(or use scope global)")
    e, repo = found
    return store.root / e / ("_global" if scope == "env" else repo), e


def repo_specifics(store: Store, cwd: Path, env: str | None, text: str, files=()) -> str | None:
    """Why `text` and `files` belong to a repository, not to the user's global scope (None when nothing does).

    Repo-relative files, a ticket id, or the name of the repository cwd is in all tie the fact to that repository.
    """
    if files:
        return "files name paths inside a repository"
    if m := TICKET.search(text):
        return f"it names the ticket {m.group()}"
    try:
        found = resolve(store, cwd, env)
    except AmbiguousRepo:
        found = None
    if found and re.search(rf"(?<![\w-]){re.escape(found[1])}(?![\w-])", text, re.I):
        return f"it names the repository {found[1]}"
    return None


def refuse_repo_specifics(store: Store, cwd: Path, env: str | None, text: str, files=()) -> None:
    """Raise LearnRefused when a global-scope fact is about the repository cwd is in."""
    if why := repo_specifics(store, cwd, env, text, files):
        raise LearnRefused(f"--scope global is for facts about the user, but {why}: record it with --scope repo "
                           "(or --scope env with --repos), or drop the repository details")


def _check_text(store: Store, fields: dict[str, str], scope: str, env: str | None, slug: str) -> None:
    for name, text in fields.items():
        if has_secret(text):
            raise LearnRefused(f"{name} looks like it holds a credential")
    if scope == "global":
        return
    try:
        write.refuse_home_paths("\n".join(fields.values()), scope)
    except write.Refused as e:
        raise LearnRefused(str(e)) from None
    known = {m.slug for m in store.memories() if m.env in (None, env)} | {slug}
    for link in _LINK.findall("\n".join(fields.values())):
        if link not in known:
            raise LearnRefused(f"[[{link}]] does not resolve in the environment or _global")


def _duplicates(store: Store, env: str | None, slug: str, description: str) -> list[str]:
    mems = [m for m in store.memories() if m.env in (None, env)]
    docs = [(str(n), m.slug.replace("-", " ") + " " + m.description) for n, m in enumerate(mems)]
    return [mems[int(i)].slug for i in sorted(alike(docs, slug.replace("-", " ") + " " + description), key=int)]


def _violation_hint(memory, files: list[str]) -> str:
    """Why a rule that already existed was broken, and the cheapest change that makes hooks show it next time."""
    paths, commands = strs(memory.triggers.get("paths")), strs(memory.triggers.get("commands"))
    suggest = ""
    if files:
        first = Path(files[0])
        suggest = f" (e.g. --paths '{first.parent.as_posix()}/*{first.suffix}')".replace("'./*", "'*")
    if not paths and not commands:
        return ("this rule existed but is index only (no file or command triggers), so it reached the agent only "
                f"through the session-start index: add --paths or --commands{suggest} so hooks inject it before the "
                "next edit")
    if files and not any(glob_match(f, g) for f in files for g in paths):
        return f"this rule's triggers did not match these files, so hooks never showed it: add --paths{suggest}"
    return ("this rule's triggers matched and it was broken anyway: repeat offenders (`lrdmem hot`) are candidates "
            "for an obligation with a check")


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
          new: bool = False, device: str = "", alternatives: str | None = None,
          revisit: str | None = None) -> LearnResult:
    """Record an experience for `memory`, creating the memory when absent; raise LearnRefused on invalid input.

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
    if (alternatives or revisit) and kind != "decision":
        raise LearnRefused("--alternatives and --revisit belong to --kind decision")
    if revisit and not _is_date(revisit):
        raise LearnRefused("--revisit must be a YYYY-MM-DD date")
    if alternatives is not None and (not alternatives.strip() or "\n" in alternatives):
        raise LearnRefused("--alternatives must be one non-empty line")
    oneline = {"agent": agent, "device": device, "pr": pr or "", "description": description or "",
               **{f"files[{i}]": f for i, f in enumerate(files)},
               **{f"{name}[{i}]": v for name, vals in (("paths", paths), ("commands", commands), ("repos", repos))
                  for i, v in enumerate(vals)}}
    if any(len(v.splitlines()) > 1 or "\n" in v or "\r" in v for v in oneline.values()):
        raise LearnRefused("agent, device, pr, description, files and triggers must be single-line")
    if scope == "global" and source == "chat":  # a PR review is repo-bound by nature: learn-pr decides its scope
        refuse_repo_specifics(store, cwd, env, "\n".join([mistake, correction, body or "", why or "", how or "",
                                                           description or ""]), files)
    folder, vis_env = scope_folder(store, cwd, scope, env)
    path = folder / f"{memory}.md"
    exists = path.exists()
    if kind == "decision" and not exists:
        if not (alternatives or "").strip():
            raise LearnRefused("a decision needs --alternatives: what was weighed and rejected")
        body = f"{(body or '').rstrip()}\n\n**Alternatives rejected:** {alternatives.strip()}"
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
        for rel in (f"{memory}.md", "experiences/x.md", "MEMORY.md"):
            write.target(store, folder, rel)
    except (write.Refused, StoreFormatError) as e:
        raise LearnRefused(str(e)) from None

    from lrdmem import experience, propose  # both build on this module

    if exists:
        cand = {"classification": "equivalent", "relates_to": path.relative_to(store.root).with_suffix("").as_posix()}
    else:
        triggers = {k: kept for k, v in (("paths", paths), ("commands", commands), ("repos", repos))
                    if (kept := [x for x in v if x.strip()])}  # v0.1 accepted blank entries; they mean nothing
        cand = {"slug": memory, "memory_type": v01_memory_type(kind or ""), "description": description,
                "body": body, "why": why, "how": how, "scope": scope, "classification": "new",
                **({"triggers": triggers} if triggers else {})}
    exp_kind = "review" if source == "pr" else "correction"
    pending = Experience("pending", folder / "experiences" / "pending.md", *store.scope_of(folder), {"kind": exp_kind})
    try:  # every ordinary refusal happens here, before the experience is recorded
        refused = propose.validate(store, cwd, {"candidates": [cand]}, agent=agent, env=env, experience=pending)
    except write.Refused as e:
        raise LearnRefused(str(e)) from None
    if refused:
        raise LearnRefused(refused[0][1])

    try:  # the write layer re-checks secrets, scope and symlinks; a late refusal is still a LearnRefused
        exp, _ = experience.add(store, cwd, kind=exp_kind, agent=agent,
                                text=f"**Mistake:** {mistake.strip()}\n\n**Correction:** {correction.strip()}",
                                scope=scope, files=files, source=source, ref=pr, device=device, env=env)
        r = propose.apply(store, cwd, {"experience": exp.ref, "candidates": [cand]}, agent=agent, device=device,
                          env=env)
        if r.refused:
            raise LearnRefused(r.refused[0][1])
        created = bool(r.created)
        if created and kind:
            write.set_fields(path, {"kind": kind, **({"revisit": revisit} if revisit else {})})
        hint = ""
        if not created:
            _touch(path, today_utc())
            widen = {k: [x for x in v if x.strip()] for k, v in (("paths", paths), ("commands", commands),
                                                                  ("repos", repos)) if any(x.strip() for x in v)}
            if widen:
                write.add_triggers(path, widen)
            elif node := store.node(path.relative_to(store.root).with_suffix("").as_posix()):
                hint = _violation_hint(node, list(files))
        return LearnResult(exp.path, path, created, dups, hint)
    except write.Refused as e:
        raise LearnRefused(str(e)) from None
