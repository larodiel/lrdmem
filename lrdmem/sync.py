"""Git sync: commit and push each environment repo (and the store root) on its own."""

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lrdmem import config
from lrdmem.store import Store

PULL_TIMEOUT = 10
PUSH_TIMEOUT = 30


@dataclass
class SyncResult:
    """Outcome of syncing one repo; env is the environment name, or "_global" for the store root."""

    env: str
    committed: bool
    pushed: bool
    error: str | None


class _GitError(Exception):
    pass


def _configured_ssh(repo: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.sshCommand"], capture_output=True,
                           text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(r.stdout.strip())


def _git(repo: Path, *args: str, timeout: float = 30) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")) and not _configured_ssh(repo):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=3"  # never block on a prompt or a dead host
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise _GitError(f"git {args[0]} timed out") from None
    except OSError as e:
        raise _GitError(str(e)) from None
    if r.returncode:
        raise _GitError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def toplevel(path: Path) -> Path | None:
    """Resolved git toplevel of path, or None when path is not inside a git repository."""
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
    except _GitError:
        return None


def autopush_enabled(cfg: dict, env: str) -> bool:
    """Whether hooks may commit and push this environment: its own flag, else the "*" flag, else off."""
    return cfg["autopush"].get(env, cfg["autopush"].get("*", False))


def _targets(store: Store, env: str | None) -> list[tuple[str, Path | None, Path]]:
    """(label, repo or None when not a repo, dir) per distinct repo, deduped by toplevel."""
    root = store.root.resolve()
    seen: dict[Path, str] = {}
    out = []
    names = [env] if env else [None, *store.environments()]  # "_global" alone: only _global/ of the root repo
    for name in names:
        path = store.root if name is None else store.root / name
        if name is not None and not path.is_dir():
            out.append((name, None, path))
            continue
        top = toplevel(path)
        # an env that is not its own repo is only synced as part of the store root repo
        if top is None or (top != path.resolve() and top != root):
            if name is not None:
                out.append((name, None, path))
            continue
        if top not in seen:
            seen[top] = name or "_global"
            out.append((name or "_global", top, path))
    return out


def _ignored(repo: Path, path: str) -> bool:
    try:
        _git(repo, "check-ignore", "-q", "--", path)
        return True
    except _GitError:  # exit 1: not ignored
        return False


def _upstream(repo: Path) -> bool:
    try:
        _git(repo, "rev-parse", "--abbrev-ref", "@{u}")
        return True
    except _GitError:
        return False


def _rebasing(repo: Path) -> bool:
    return any((repo / _git(repo, "rev-parse", "--git-path", d)).exists() for d in ("rebase-merge", "rebase-apply"))


def _pull(repo: Path, label: str, timeout: float | None = None) -> str | None:
    """Rebase onto upstream (no autostash: callers guarantee a clean tree); abort only a rebase started here."""
    if _rebasing(repo):
        return f"rebase in progress in {label}"
    if not _upstream(repo):  # fresh/empty remote: nothing to rebase onto yet
        return None
    try:
        _git(repo, "pull", "--rebase", timeout=PULL_TIMEOUT if timeout is None else timeout)
    except _GitError as e:
        try:
            _git(repo, "rebase", "--abort")
        except _GitError:
            pass
        return f"pull failed in {label} (rebase aborted, nothing pushed): {e}"
    return None


def _sync_one(label: str, repo: Path, include: str, excludes: list[str], message: str,
              skip_idle: bool = False) -> SyncResult:
    spec = ["--", f":(literal){include}" if include != "." else ".",
            *(f":(exclude,literal){e}" for e in excludes)]
    committed = pushed = False
    try:
        if _rebasing(repo):
            return SyncResult(label, False, False, f"rebase in progress in {label}")
        if _git(repo, "ls-files", "-u"):
            return SyncResult(label, False, False, f"unmerged paths in {label}; resolve them first")
        if _git(repo, "status", "--porcelain", *spec):
            _git(repo, "add", "-A", *spec)
            _git(repo, "commit", "-m", message)
            committed = True
        remotes = _git(repo, "remote").split()
        if remotes and skip_idle and not committed and _upstream(repo) \
                and _git(repo, "rev-list", "--count", "@{u}..HEAD") == "0":
            return SyncResult(label, False, False, None)  # nothing to push: spare the network
        if remotes:
            if _git(repo, "status", "--porcelain", "--untracked-files=no"):
                done = f"committed {label}; " if committed else ""
                return SyncResult(label, committed, False,
                                  f"{done}not pulled/pushed: uncommitted changes outside {label}")
            err = _pull(repo, label)
            if err:
                return SyncResult(label, committed, False, err)
            if _upstream(repo):
                _git(repo, "push", timeout=PUSH_TIMEOUT)
            else:
                _git(repo, "push", "-u", remotes[0], "HEAD", timeout=PUSH_TIMEOUT)
            pushed = True
    except _GitError as e:
        return SyncResult(label, committed, False, str(e))
    return SyncResult(label, committed, pushed, None)


def sync(store: Store, env: str | None = None, message: str | None = None,
         skip_idle: bool = False) -> list[SyncResult]:
    """Commit and push each repo (store root for _global, plus every env); only env's repo when given.

    env "_global" stages only the _global/ folder of the root repo, never the rest of it. skip_idle (hooks):
    no pull or push for a repo where nothing was committed and nothing is ahead of its upstream.
    """
    message = message or f"lrdmem: {config.load()['device']} {datetime.now():%Y-%m-%d %H:%M}"
    results = []
    for label, top, path in _targets(store, env):
        if top is None:
            results.append(SyncResult(label, False, False, "not a git repository"))
            continue
        # the root repo must not swallow envs that are repos of their own; an exclude naming an ignored
        # path makes `git add` fail, and ignored envs are not staged anyway
        include = path.resolve().relative_to(top).as_posix()
        excludes = [n for n in store.environments() if include == "." and top == store.root.resolve()
                    and toplevel(store.root / n) != top and not _ignored(top, n)]
        results.append(_sync_one(label, top, include, excludes, message, skip_idle))
    return results


def pull(store: Store, env: str | None = None, budget: float | None = None) -> str | None:
    """Pull --rebase (never autostash; dirty repos are skipped) every repo that has a remote; an error string or None.

    Each pull gets 10 s, or what is left of budget (seconds shared by every pull of this call) when that is less.
    """
    errors = []
    deadline = None if budget is None else time.monotonic() + budget
    for label, top, _ in _targets(store, env):
        if top is None:
            continue
        timeout = PULL_TIMEOUT if deadline is None else min(PULL_TIMEOUT, deadline - time.monotonic())
        if timeout <= 0:
            errors.append(f"skipped {label}: pull time budget used up")
            continue
        try:
            if _git(top, "remote"):
                if not _rebasing(top) and _git(top, "status", "--porcelain", "--untracked-files=no"):
                    errors.append(f"skipped: uncommitted changes in {label}")
                    continue
                err = _pull(top, label, timeout)
                if err:
                    errors.append(err)
        except _GitError as e:
            errors.append(f"{label}: {e}")
    return "; ".join(errors) or None
