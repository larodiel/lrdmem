"""Obligation checks: run `check.run` commands, but only the exact text a human approved on this device."""

import hashlib
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem.store import Memory, Store
from lrdmem.triggers import matches

TIMEOUT = 300
TAIL = 60
KILL_WAIT = 5  # seconds to wait for output pipes after killing the group


@dataclass
class CheckResult:
    """Outcome of one run_checks call: failures are (slug, output tail), unrunnable are (slug, reason)."""

    failures: list[tuple[str, str]] = field(default_factory=list)
    skipped_pending: list[str] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    unrunnable: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[tuple[Memory, bool, str, list[str]]] = field(default_factory=list)  # (memory, passed, tail, files)


def key(store: Store, memory: Memory) -> str:
    """Allowlist key: the store-relative path of the memory without .md."""
    return memory.path.relative_to(store.root).with_suffix("").as_posix()


def command_text(memory: Memory) -> str | None:
    """The check.run text of an obligation, or None when it has none."""
    c = memory.check
    run = c.get("run") if c else None
    return run if memory.kind == "obligation" and isinstance(run, str) and run.strip() else None


def command_hash(memory: Memory) -> str:
    """sha256 hex of the memory's check.run exactly as written."""
    return hashlib.sha256((command_text(memory) or "").encode("utf-8")).hexdigest()


def _script(cmd: str) -> str | None:
    """cmd with each `{files}` word replaced by `"$@"`, or None unless every `{files}` is a bare unquoted word."""
    pieces: list[str] = []
    tok, clean, quote, i = "", True, "", 0  # clean: the current word holds no quote or escape

    def end_word() -> None:
        nonlocal tok, clean
        pieces.append('"$@"' if clean and tok == "{files}" else tok)
        tok, clean = "", True

    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and quote != "'":  # escape: keep it and the next char together
            tok, clean, i = tok + cmd[i:i + 2], False, i + 1
        elif quote:
            tok += ch
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            tok, clean, quote = tok + ch, False, ch
        elif ch.isspace():
            end_word()
            pieces.append(ch)
        else:
            tok += ch
        i += 1
    end_word()
    script = "".join(pieces)
    # an occurrence that did not become a bare word was quoted, escaped or glued to other text
    return script if script.count('"$@"') - cmd.count('"$@"') == cmd.count("{files}") else None


def problem(memory: Memory) -> str | None:
    """Why this memory's check.run can neither be approved nor run, or None when it is fine."""
    cmd = command_text(memory)
    if cmd is None:
        return "no check.run"
    if not all(0x20 <= ord(c) <= 0x7E for c in cmd):
        return "unapprovable: non-printable or non-ASCII characters"
    if "{files}" in cmd and any(t in cmd for t in ("$(", "`", "${")):
        return "unrunnable: {files} cannot be combined with $(, ` or ${"
    if _script(cmd) is None:
        return "unrunnable: {files} must be an unquoted word"
    return None


def _approved(store: Store, memory: Memory, cfg: dict) -> bool:
    return cfg["allow"].get(key(store, memory)) == command_hash(memory)


def pending(store: Store, cfg: dict) -> list[Memory]:
    """Obligations with a check.run whose hash is not the approved one."""
    return [m for m in store.memories() if command_text(m) and not _approved(store, m, cfg)]


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    try:
        proc.communicate(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:  # a grandchild in its own session still holds the pipe
        proc.stdout.close()
        proc.wait()  # the shell itself got SIGKILL: reap it


def run_checks(store: Store, env: str, repo: str, repo_root: Path, on: str, files: list[str], cfg: dict,
               budget: float | None = None) -> CheckResult:
    """Run the approved obligations for `on` against the files their triggers match; unapproved ones are never run.

    budget is the wall-clock seconds shared by every check of this call (default TIMEOUT); each check gets what
    is left of it. File names are passed as argv to `sh -c`, never spliced into the command text.
    """
    result = CheckResult()
    deadline = time.monotonic() + (TIMEOUT if budget is None else budget)
    for m in store.memories():
        if not command_text(m) or m.check.get("on") != on or m.state != "active":
            continue  # only active obligations: candidates and contested ones are never enforced
        hit = [f for f in files if matches(m, env, repo, path=f, env_wide=True)]
        if not hit:
            continue
        why = problem(m)
        if why:
            result.unrunnable.append((m.slug, why))
            continue
        if not _approved(store, m, cfg):
            result.skipped_pending.append(m.slug)
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            result.failures.append((m.slug, "not run: the check time budget is used up"))
            continue
        argv = ["sh", "-c", _script(command_text(m)), "sh", *("./" + f if f.startswith("-") else f for f in hit)]
        proc = subprocess.Popen(argv, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            _kill(proc)
            result.failures.append((m.slug, f"timed out after {math.ceil(left)}s"))
            result.outcomes.append((m, False, f"timed out after {math.ceil(left)}s", hit))
            continue
        tail = "\n".join(out.splitlines()[-TAIL:])
        if proc.returncode:
            result.failures.append((m.slug, tail))
        else:
            result.ran.append(m.slug)
        result.outcomes.append((m, not proc.returncode, tail, hit))
    return result
