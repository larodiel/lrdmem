"""Harness-neutral hook handlers and the dispatcher every `lrdmem hook` call goes through."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem import config, consolidate, experience, revision, write
from lrdmem.model import Evidence
from lrdmem.checks import CheckResult, pending, problem, run_checks
from lrdmem.lint import lint, printable
from lrdmem.recall import recall, render
from lrdmem.session import Session
from lrdmem.store import Store, resolve, store_root
from lrdmem.sync import autopush_enabled, pull, sync, toplevel
from lrdmem.triggers import CAVEAT

PROMPT = "If this message corrects your work, record it: lrdmem learn --help."
MAX_BLOCKS = 2
LINT_SHOWN = 10
POST_EDIT_BUDGET = 8  # seconds for all post-edit checks; the post-tool hook is killed at 10 s
EDIT_TIME_STOP_BUDGET = 4  # OpenCode's plugin kills a hook at 5 s; covers its post-edit and stop checks
STOP_BUDGET = 300  # the Stop hook is killed at 330 s
PULL_BUDGET = 3  # seconds for every session-start pull together; the context matters more than freshness
CONSOLIDATE_BUDGET = 20  # seconds of consolidation at a non-blocking Stop, after the checks, before autopush
EDIT_TIME_CONSOLIDATE_BUDGET = 3  # OpenCode's end of session: the plugin kills the hook at 5 s
OUTPUT_LINES = 20  # check output kept in a check experience


@dataclass
class HookInput:
    """One harness event, already mapped to a handler event; paths are relative to the repo root."""

    event: str
    session_id: str
    cwd: Path
    paths: list[str] = field(default_factory=list)
    command: str | None = None
    prompt: str | None = None
    stop_active: bool = False
    edit_time_stop: bool = False  # harness has no blocking Stop: stop checks run at post-edit, stop only syncs


@dataclass
class HookOutput:
    """What a handler wants the harness to see: context text, or a reason to block the Stop."""

    context: str = ""
    block_reason: str | None = None


@dataclass
class _Scope:
    store: Store
    cfg: dict
    env: str
    repo: str


def repo_path(cwd: Path, path: str | None) -> str | None:
    """path (absolute or relative to cwd) relative to cwd's git toplevel; None when empty or outside the repo."""
    top = toplevel(cwd) if path else None
    if not top:
        return None
    try:
        return (cwd / path).resolve().relative_to(top).as_posix()
    except ValueError:
        return None


def repo_paths(cwd: Path, paths: list[str | None]) -> list[str]:
    """repo_path of each path, dropping empty and outside-repo ones and duplicates, in order."""
    return list(dict.fromkeys(p for p in (repo_path(cwd, path) for path in paths) if p))


def patch_paths(patch: str) -> list[str]:
    """Files an apply_patch-format patch adds or updates (an update's `Move to` target when renamed); not deletes."""
    paths: list[str] = []
    renamable = False
    for line in patch.splitlines():
        head, _, rest = line.partition(": ")
        if head in ("*** Add File", "*** Update File"):
            paths.append(rest.strip())
        elif head == "*** Move to" and renamable:
            paths[-1] = rest.strip()
        renamable = head == "*** Update File" or (renamable and not head.startswith("*** "))
    return list(dict.fromkeys(p for p in paths if p))


def _scope(inp: HookInput) -> _Scope | None:
    """The opted-in repo for this event, or None; AmbiguousRepo and a missing store raise (handled by the caller)."""
    store = Store(store_root())
    found = resolve(store, inp.cwd)
    return _Scope(store, config.load(), *found) if found else None


def _failures(r: CheckResult) -> str:
    return "\n".join(f"lrdmem check failed: {slug}\n{tail}" for slug, tail in r.failures)


def _scope_name(store: Store, m) -> str:
    env, repo = store.scope_of(m.path.parent)
    return "repo" if repo else "env" if env else "global"


def _record(inp: HookInput, s: _Scope, result: CheckResult) -> None:
    """One `check` experience per check that ran (once per session, memory and result), as evidence on its memory:
    a failure is observed_in 0.5; a pass after a failure in the same session supports 1.0. Errors never surface."""
    sid = inp.session_id or "nosession"
    for m, passed, tail, files in result.outcomes:
        try:
            verdict = "pass" if passed else "fail"
            lines = "\n".join(tail.splitlines()[-OUTPUT_LINES:])
            text = f"check {'passed' if passed else 'failed'}: {m.ref}\nfiles: {', '.join(files)}"
            args = dict(kind="check", agent="hook", source="check", scope=_scope_name(s.store, m), files=files,
                        request_id=f"{sid}:{m.ref}:{verdict}", device=s.cfg.get("device", ""), env=s.env)
            try:
                exp, created = experience.add(s.store, inp.cwd, text=text + ("\n\n" + lines if lines else ""),
                                              **args)
            except write.Refused:  # the output held something the store refuses (a secret): keep the fact only
                exp, created = experience.add(s.store, inp.cwd, text=text, **args)
            failed_before = any(e.request_id == f"{sid}:{m.ref}:fail" for e in s.store.experiences())
            if not created or (passed and not failed_before):
                continue
            item = Evidence(exp.ref, "supports", 1.0) if passed else Evidence(exp.ref, "observed_in", 0.5)
            folder = m.path.parent
            if write.add_evidence(write.target(s.store, folder, m.path.name), item):
                revision.record(s.store, folder, m.ref, "merge", {"evidence": ""}, {"evidence": exp.ref},
                                f"check {verdict}", "hook")
        except Exception:
            continue  # a recording problem never breaks the hook


def _session_start(inp: HookInput, s: _Scope) -> HookOutput:
    deadline = time.monotonic() + PULL_BUDGET
    for target in (s.env, "_global"):
        if autopush_enabled(s.cfg, target):
            try:
                pull(s.store, target, budget=deadline - time.monotonic())
            except Exception:
                pass  # offline or broken remote: still give the agent its context
    root = s.store.root
    parts = [f"lrdmem: repository `{s.repo}`, environment `{s.env}`. These are the indexes only; open a memory "
             "file when its line is relevant to the task, and skip memories scoped to other repositories. "
             f"Paths are relative to {root}/. Search with `lrdmem find TERM`; "
             f"record corrections with `lrdmem learn --help`. {CAVEAT}"]
    for folder in s.store.scope_for(s.env, s.repo):
        index = folder / "MEMORY.md"
        body = index.read_text(encoding="utf-8", errors="replace").strip() if index.is_file() else ""
        parts.append(f"## {folder.relative_to(root).as_posix()}/MEMORY.md\n{body or '(empty)'}")
    problems = lint(s.store, s.env)
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:LINT_SHOWN])
        more = len(problems) - LINT_SHOWN
        more = f"\n- ...and {more} more (run `lrdmem lint {s.env}`)" if more > 0 else ""
        parts.append(f"## lrdmem lint: {len(problems)} problem(s); fix them when convenient\n{shown}{more}")
    waiting = [m for m in pending(s.store, s.cfg) if m.env in (None, s.env)]
    broken = sum(1 for m in waiting if problem(m))
    if len(waiting) > broken:
        parts.append(f"{len(waiting) - broken} obligation(s) await approval: run `lrdmem allow`")
    if broken:
        parts.append(f"{broken} obligation(s) can't be approved: run `lrdmem allow` for details")
    decisions = [q for q in consolidate.open_items(s.store) if s.store.scope_of(q.path.parent.parent)[0] in (None, s.env)]
    if decisions:
        parts.append(f"{len(decisions)} memory decision(s) pending: run `lrdmem consolidate --queue`")
    return HookOutput("\n\n".join(parts))


def _inject(inp: HookInput, s: _Scope) -> HookOutput:
    if not (inp.paths or inp.command):
        return HookOutput()
    session = Session(inp.session_id)
    picked = recall(s.store, s.env, s.repo, files=inp.paths, command=inp.command, for_hook=True,
                    exclude=session.injected)
    if not picked:
        return HookOutput()
    session.injected |= {h.node.ref for h in picked}
    session.save()
    return HookOutput(render(picked, ", ".join(inp.paths) or inp.command or ""))


def _post_edit(inp: HookInput, s: _Scope) -> HookOutput:
    root = toplevel(inp.cwd)
    if not inp.paths or not root:
        return HookOutput()
    session = Session(inp.session_id)
    new = [p for p in inp.paths if p not in session.edited]
    if new:
        session.edited += new
        session.save()
    ons = ("post-edit", "stop") if inp.edit_time_stop else ("post-edit",)
    deadline = time.monotonic() + (EDIT_TIME_STOP_BUDGET if inp.edit_time_stop else POST_EDIT_BUDGET)
    texts = []
    for on in ons:
        r = run_checks(s.store, s.env, s.repo, root, on, inp.paths, s.cfg, budget=deadline - time.monotonic())
        _record(inp, s, r)
        texts.append(_failures(r))
    return HookOutput("\n".join(filter(None, texts)))


def _prompt(inp: HookInput, s: _Scope) -> HookOutput:
    return HookOutput(PROMPT)


def _stop(inp: HookInput, s: _Scope) -> HookOutput:
    root = toplevel(inp.cwd)
    session = Session(inp.session_id)
    out = HookOutput()
    if root and session.edited and not inp.edit_time_stop:
        r = run_checks(s.store, s.env, s.repo, root, "stop", session.edited, s.cfg, budget=STOP_BUDGET)
        _record(inp, s, r)
        text = _failures(r)
        if text and not inp.stop_active and session.stop_blocks < MAX_BLOCKS:
            session.stop_blocks += 1
            session.save()
            return HookOutput(block_reason=text)  # no sync: the agent is not done yet
        out = HookOutput(text)
    try:
        consolidate.run(s.store, budget=EDIT_TIME_CONSOLIDATE_BUDGET if inp.edit_time_stop else CONSOLIDATE_BUDGET)
    except Exception:
        pass  # consolidation is best effort; the next Stop finishes what is left
    for target in (s.env, "_global"):
        if autopush_enabled(s.cfg, target):
            sync(s.store, target, skip_idle=True)  # never raises for git problems: errors come back in the results
    return out


_HANDLERS = {"session-start": _session_start, "pre-edit": _inject, "pre-command": _inject,
             "post-edit": _post_edit, "prompt": _prompt, "stop": _stop}


def _clean(text: str) -> str:
    return "\n".join(printable(line) for line in text.split("\n"))


def handle(inp: HookInput) -> HookOutput:
    """Run the handler for inp.event; unknown events, repos not opted in and any exception give an empty output."""
    try:
        handler = _HANDLERS.get(inp.event)
        s = _scope(inp) if handler else None
        if not s:
            return HookOutput()
        out = handler(inp, s)
    except Exception:
        return HookOutput()
    return HookOutput(_clean(out.context), None if out.block_reason is None else _clean(out.block_reason))


def dispatch(phase: str, agent: str, stdin: str) -> str:
    """Parse harness JSON for phase, handle it and return the adapter's stdout text; "" on any error."""
    from lrdmem.hooks import claude, codex, opencode  # adapters import this module

    adapters = {"claude": claude, "codex": codex, "opencode": opencode}
    try:
        adapter = adapters[agent]
        payload = json.loads(stdin or "{}")
        if not isinstance(payload, dict):
            return ""
        inp = adapter.parse(phase, payload)
        if agent == "claude" and inp.event == "session-start":
            try:
                s = _scope(inp)
                if s:
                    claude.link_claude_memory(inp.cwd, s.store.root / s.env / s.repo)
            except Exception:
                pass  # a failed link must not cost the session its context
        return adapter.emit(inp, handle(inp))
    except Exception:
        return ""
