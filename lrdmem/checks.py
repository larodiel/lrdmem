"""Obligation checks: run `check.run` commands, but only the exact text a human approved on this device."""

import hashlib
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem.store import Memory, Store
from lrdmem.triggers import matches

TIMEOUT = 300
TAIL = 60


@dataclass
class CheckResult:
    """Outcome of one run_checks call: failures are (slug, output tail)."""

    failures: list[tuple[str, str]] = field(default_factory=list)
    skipped_pending: list[str] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)


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


def _approved(store: Store, memory: Memory, cfg: dict) -> bool:
    return cfg["allow"].get(key(store, memory)) == command_hash(memory)


def pending(store: Store, cfg: dict) -> list[Memory]:
    """Obligations with a check.run whose hash is not the approved one."""
    return [m for m in store.memories() if command_text(m) and not _approved(store, m, cfg)]


def run_checks(store: Store, env: str, repo: str, repo_root: Path, on: str, files: list[str], cfg: dict,
               run=subprocess.run) -> CheckResult:
    """Run the approved obligations for `on` against the files their triggers match; unapproved ones are never run."""
    result = CheckResult()
    for m in store.memories():
        if not command_text(m) or m.check.get("on") != on:
            continue
        hit = [f for f in files if matches(m, env, repo, path=f)]
        if not hit:
            continue
        if not _approved(store, m, cfg):
            result.skipped_pending.append(m.slug)
            continue
        cmd = command_text(m)
        if "{files}" in cmd:
            cmd = cmd.replace("{files}", " ".join(shlex.quote(f) for f in hit))
        try:
            res = run(cmd, shell=True, cwd=repo_root, timeout=TIMEOUT, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, errors="replace")
        except subprocess.TimeoutExpired:
            result.failures.append((m.slug, f"timed out after {TIMEOUT}s"))
            continue
        if res.returncode:
            result.failures.append((m.slug, "\n".join((res.stdout or "").splitlines()[-TAIL:])))
        else:
            result.ran.append(m.slug)
    return result
