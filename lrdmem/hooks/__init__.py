"""Harness-neutral hook handlers and the dispatcher every `lrdmem hook` call goes through."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lrdmem import config
from lrdmem.checks import CheckResult, pending, run_checks
from lrdmem.lint import lint, printable
from lrdmem.session import Session
from lrdmem.store import Store, resolve, store_root
from lrdmem.sync import autopush_enabled, pull, sync
from lrdmem.triggers import render, select

PROMPT = "If this message corrects your work, record it: lrdmem learn --help."
MAX_BLOCKS = 2
LINT_SHOWN = 10


@dataclass
class HookInput:
    """One harness event, already mapped to a handler event; path is relative to the repo root."""

    event: str
    session_id: str
    cwd: Path
    path: str | None = None
    command: str | None = None
    prompt: str | None = None
    stop_active: bool = False


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


def _toplevel(cwd: Path) -> Path | None:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(top).resolve() if top else None


def repo_path(cwd: Path, path: str | None) -> str | None:
    """path (absolute or relative to cwd) relative to cwd's git toplevel; None when empty or outside the repo."""
    top = _toplevel(cwd) if path else None
    if not top:
        return None
    try:
        return (cwd / path).resolve().relative_to(top).as_posix()
    except ValueError:
        return None


def _scope(inp: HookInput) -> _Scope | None:
    """The opted-in repo for this event, or None; AmbiguousRepo and a missing store raise (handled by the caller)."""
    store = Store(store_root())
    found = resolve(store, inp.cwd)
    return _Scope(store, config.load(), *found) if found else None


def _failures(r: CheckResult) -> str:
    return "\n".join(f"lrdmem check failed: {slug}\n{tail}" for slug, tail in r.failures)


def _session_start(inp: HookInput, s: _Scope) -> HookOutput:
    if autopush_enabled(s.cfg, s.env):
        try:
            pull(s.store, s.env)
        except Exception:
            pass  # offline or broken remote: still give the agent its context
    root = s.store.root
    parts = [f"lrdmem: repository `{s.repo}`, environment `{s.env}`. These are the indexes only; open a memory "
             "file when its line is relevant to the task, and skip memories scoped to other repositories. "
             f"Paths are relative to {root}/. Search with `lrdmem find TERM`; "
             "record corrections with `lrdmem learn --help`."]
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
    if waiting:
        parts.append(f"{len(waiting)} obligation(s) await approval: run `lrdmem allow`")
    return HookOutput("\n\n".join(parts))


def _inject(inp: HookInput, s: _Scope) -> HookOutput:
    if not (inp.path or inp.command):
        return HookOutput()
    session = Session(inp.session_id)
    picked = select(s.store, s.env, s.repo, path=inp.path, command=inp.command, exclude=session.injected)
    if not picked:
        return HookOutput()
    session.injected |= {m.slug for m in picked}
    session.save()
    return HookOutput(render(picked, inp.path or inp.command or ""))


def _post_edit(inp: HookInput, s: _Scope) -> HookOutput:
    root = _toplevel(inp.cwd)
    if not inp.path or not root:
        return HookOutput()
    session = Session(inp.session_id)
    if inp.path not in session.edited:
        session.edited.append(inp.path)
        session.save()
    return HookOutput(_failures(run_checks(s.store, s.env, s.repo, root, "post-edit", [inp.path], s.cfg)))


def _prompt(inp: HookInput, s: _Scope) -> HookOutput:
    return HookOutput(PROMPT)


def _stop(inp: HookInput, s: _Scope) -> HookOutput:
    root = _toplevel(inp.cwd)
    session = Session(inp.session_id)
    out = HookOutput()
    if root and session.edited:
        text = _failures(run_checks(s.store, s.env, s.repo, root, "stop", session.edited, s.cfg))
        if text and not inp.stop_active and session.stop_blocks < MAX_BLOCKS:
            session.stop_blocks += 1
            session.save()
            out = HookOutput(block_reason=text)
        else:
            out = HookOutput(text)
    for target in (s.env, "_global"):
        if autopush_enabled(s.cfg, target):
            sync(s.store, target)  # never raises for git problems: errors come back in the results
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
    from lrdmem.hooks import claude  # adapters import this module

    adapters = {"claude": claude}
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
