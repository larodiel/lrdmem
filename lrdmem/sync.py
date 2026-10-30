"""Git sync: commit and push each environment repo (and the store root) on its own."""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lrdmem import commitmsg, config
from lrdmem.store import Store

PULL_TIMEOUT = 10
PUSH_TIMEOUT = 30


@dataclass
class SyncResult:
    """Outcome of syncing one repo; env is the environment name, "store" for the
    whole store root repo, or "_global" when only its _global/ folder was synced."""

    env: str
    committed: bool
    pushed: bool
    error: str | None


class _GitError(Exception):
    pass


_HARDEN = ["-c", "safe.bareRepository=explicit", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
           "-c", "diff.ignoreSubmodules=all", "-c", "status.submoduleSummary=false", "-c", "submodule.recurse=false"]
_AUTO = object()


def _run(repo: Path, args: tuple, env: dict, ceiling, **kw) -> subprocess.CompletedProcess:
    """The one place git runs on store content: -C the trusted repo, no fsmonitor/hooks/bare discovery."""
    env = {**env, "GIT_CONFIG_NOSYSTEM": "1"}
    ceiling = repo.parent if ceiling is _AUTO else ceiling
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return subprocess.run(["git", *_HARDEN, "-C", str(repo), *args], stdin=subprocess.DEVNULL, env=env, **kw)


def _configured_ssh(repo: Path) -> bool:
    try:
        r = _run(repo, ("config", "--get", "core.sshCommand"), os.environ.copy(), _AUTO, capture_output=True,
                 text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(r.stdout.strip())


def _git(repo: Path, *args: str, timeout: float = 30, ceiling=_AUTO, binary: bool = False):
    """Run hardened git in a trusted repo dir; stripped text, or raw bytes with binary. ceiling: dir git must not climb into."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")) and not _configured_ssh(repo):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=3"  # never block on a prompt or a dead host
    try:
        r = _run(repo, args, env, ceiling, capture_output=True, text=not binary, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise _GitError(f"git {args[0]} timed out") from None
    except OSError as e:
        raise _GitError(str(e)) from None
    if r.returncode:
        err = r.stderr.decode(errors="replace") if binary else r.stderr
        raise _GitError(err.strip() or f"git {args[0]} failed")
    return r.stdout if binary else r.stdout.strip()


def nested_git(base: Path, own: Path | None = None, prune: tuple = ()) -> list[Path]:
    """Every `.git` (dir or file) under base other than own, not descending into symlinks or the prune dirs.

    A nested repository in store content can carry config and attributes that run commands when the outer
    repo stages or inspects it, so such content is refused rather than synced.
    """
    found = []
    for cur, dirs, files in os.walk(base):
        cur = Path(cur)
        dirs[:] = [d for d in dirs if cur / d not in prune and (cur / d).resolve() != own]
        for name in (*dirs, *files):
            if name.lower() == ".git" and cur / name != own:
                found.append(cur / name)
        dirs[:] = [d for d in dirs if d.lower() != ".git"]
    return found


def toplevel(path: Path, ceiling: Path | None = None) -> Path | None:
    """Resolved git toplevel of path, or None when path is not inside a git repository.

    ceiling: directory git must not climb into (pass the store root's parent when path is inside a store).
    """
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel", ceiling=ceiling)).resolve()
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
    # "_global" alone: only _global/ of the root repo; None: the whole root repo, labelled "store"
    names = [env] if env else [None, *store.environments()]
    for name in names:
        path = store.root if name is None else store.root / name
        if name is not None and not path.is_dir():
            out.append((name, None, path))
            continue
        top = toplevel(path, store.root.parent)
        # an env that is not its own repo is only synced as part of the store root repo
        if top is None or (top != path.resolve() and top != root):
            if name is not None:
                out.append((name, None, path))
            continue
        if top not in seen:
            seen[top] = name or "store"
            out.append((name or "store", top, path))
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
        conflict = _rebasing(repo)  # a rebase left in progress means conflicting changes, not a network error
        try:
            _git(repo, "rebase", "--abort")
        except _GitError:
            pass
        if conflict:
            return (f"conflict in {label}: this computer and the remote changed the same files; nothing pushed, "
                    f"your commit is kept. Resolve it with: cd {repo} && git pull --rebase, fix the files, "
                    "git add -A && git rebase --continue, then lrdmem sync")
        return f"pull failed in {label} (rebase aborted, nothing pushed): {e}"
    return None


def _nested_msg(path: Path, gitlink: bool = False) -> str:
    if gitlink:
        return f"nested git repository inside the store (gitlink in the index): {path}; run git rm --cached on it"
    return f"nested git repository inside the store: {path}; move it out of the store"


def _message(repo: Path, label: str, spec: list[str]) -> str:
    """Conventional Commits message for what is staged under spec."""
    parts = _git(repo, "diff", "--cached", "--no-renames", "--name-status", "-z", *spec).split("\0")
    changed = [(parts[i][:1], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]
    return commitmsg.build(changed, config.load()["device"], label, repo)


def _sync_one(label: str, repo: Path, include: str, excludes: list[str], message: str,
              skip_idle: bool = False, own_repos: list[str] = (), extras: list[str] = ()) -> SyncResult:
    spec = ["--", f":(literal){include}" if include != "." else ".", *(f":(literal){x}" for x in extras),
            *(f":(exclude,literal){e}" for e in excludes), ":(exclude,glob)**/.*.tmp"]  # write.create temp files
    committed = pushed = False
    try:
        if _rebasing(repo):
            return SyncResult(label, False, False, f"rebase in progress in {label}")
        if _git(repo, "ls-files", "-u"):
            return SyncResult(label, False, False, f"unmerged paths in {label}; resolve them first")
        found = nested_git(repo / include, repo / ".git", tuple(repo / e for e in [*excludes, *own_repos]))
        if found:
            return SyncResult(label, False, False, _nested_msg(found[0]))
        if _git(repo, "status", "--porcelain", "--ignore-submodules=all", *spec):
            _git(repo, "add", "-A", *spec)
            links = [line.split("\t", 1)[1] for line in _git(repo, "ls-files", "-s", "-z", *spec).split("\0")
                     if line.startswith("160000")]
            if links:
                _git(repo, "reset", "-q", "--", *(f":(literal){x}" for x in links))
                return SyncResult(label, False, False, _nested_msg(repo / links[0], gitlink=True))
            _git(repo, "commit", "-m", message or _message(repo, label, spec))
            committed = True
        remotes = _git(repo, "remote").split()
        if remotes and skip_idle and not committed and _upstream(repo) \
                and _git(repo, "rev-list", "--count", "@{u}..HEAD") == "0":
            return SyncResult(label, False, False, None)  # nothing to push: spare the network
        if remotes:
            if _git(repo, "status", "--porcelain", "--ignore-submodules=all", "--untracked-files=no"):
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
    results = []
    for label, top, path in _targets(store, env):
        if top is None:
            results.append(SyncResult(label, False, False, "not a git repository"))
            continue
        # the root repo must not swallow envs that are repos of their own; an exclude naming an ignored
        # path makes `git add` fail, and ignored envs are not staged anyway
        include = path.resolve().relative_to(top).as_posix()
        excludes = [n for n in store.environments() if include == "." and top == store.root.resolve()
                    and toplevel(store.root / n, store.root.parent) != top and not _ignored(top, n)]
        own = [n for n in store.environments() if include == "." and top == store.root.resolve()
               and toplevel(store.root / n, store.root.parent) != top]  # env repos, ignored ones too
        # the store-format marker lives at the root: a scoped sync of the root repo carries it too
        extras = [".lrdmem-format"] if include != "." and top == store.root.resolve() \
            and (store.root / ".lrdmem-format").is_file() else []
        results.append(_sync_one(label, top, include, excludes, message, skip_idle, own, extras))
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
                if not _rebasing(top) and _git(top, "status", "--porcelain", "--ignore-submodules=all", "--untracked-files=no"):
                    errors.append(f"skipped: uncommitted changes in {label}")
                    continue
                err = _pull(top, label, timeout)
                if err:
                    errors.append(err)
        except _GitError as e:
            errors.append(f"{label}: {e}")
    return "; ".join(errors) or None
