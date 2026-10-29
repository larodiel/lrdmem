"""Link a GitHub PR's review comments to the memories they reinforce."""

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem.learn import learn, terms
from lrdmem.lint import printable
from lrdmem.store import Store, resolve

_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:#.*|/files.*)?$")
_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
_MIN_SHARED = 2
_MISTAKE = 300


@dataclass
class PrResult:
    """Outcome of learn_pr: (comment url, memory path relative to the store) pairs, the comments left over, and
    the urls of comments an earlier run already linked."""

    linked: list[tuple[str, str]] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    already: list[str] = field(default_factory=list)


def _parse(url: str) -> tuple[str, str, str]:
    m = _URL.match(url)
    if not m or "\n" in url or any(set(part) == {"."} for part in m.groups()[:2]):
        raise ValueError("expected https://github.com/<owner>/<repo>/pull/<n>")
    return m.groups()


def _json_values(text: str) -> list:
    """Every JSON value in text; `gh api --paginate` prints one array per page back to back. Pages are flattened."""
    dec, i, out = json.JSONDecoder(), 0, []
    try:
        while True:
            while i < len(text) and text[i].isspace():
                i += 1
            if i >= len(text):
                break
            value, i = dec.raw_decode(text, i)
            out += value if isinstance(value, list) else [value]
    except ValueError:
        raise RuntimeError("could not parse gh output") from None
    return out


def _item(c: dict, path: str) -> dict:
    return {"id": c.get("id"), "url": c.get("html_url", ""), "path": path, "body": c.get("body") or "",
            "author": (c.get("user") or {}).get("login", ""), "association": c.get("author_association") or "none"}


def _github_remotes(cwd: Path) -> set[tuple[str, str]]:
    """Lowercased (owner, repo) pairs of the checkout's GitHub remotes; other hosts are ignored."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             check=True).stdout.strip()
        out = subprocess.run(["git", "-C", top, "remote", "-v"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    found = (_REMOTE.match(parts[1]) for parts in (ln.split() for ln in out.splitlines()) if len(parts) > 1)
    return {(m.group(1).lower(), m.group(2).lower()) for m in found if m}


def fetch_review_comments(url: str, run=subprocess.run) -> list[dict]:
    """Inline and review comments of a PR as dicts with id, url, path, body, author, association; ValueError on a bad URL."""
    owner, repo, n = _parse(url)

    def gh(kind):
        res = run(["gh", "api", f"repos/{owner}/{repo}/pulls/{n}/{kind}", "--paginate"],
                  capture_output=True, text=True, check=True)
        return _json_values(res.stdout or "")

    out = [_item(c, c.get("path") or "") for c in gh("comments")]
    out += [_item(r, "") for r in gh("reviews") if (r.get("body") or "").strip()]
    return out


def learn_pr(store: Store, cwd: Path, url: str, agent: str, run=subprocess.run, env: str | None = None,
             device: str = "") -> PrResult:
    """Record each human review comment as a context of its best-matching memory; LookupError if cwd is not opted in."""
    owner, pr_repo, _ = _parse(url)
    found = resolve(store, cwd, env)
    if not found:
        raise LookupError("this repository is not opted in to the store")
    e, repo = found
    if pr_repo.lower() != repo.lower():
        raise LookupError(f"PR repository '{pr_repo}' is not the opted-in repository '{repo}'")
    remotes = _github_remotes(cwd)
    if not remotes:
        raise LookupError("this checkout has no GitHub remote to bind the PR to")
    if (owner.lower(), pr_repo.lower()) not in remotes:
        raise LookupError(f"PR {owner}/{pr_repo} is not a GitHub remote of this checkout")
    comments = fetch_review_comments(url, run)
    cands = sorted((m for m in store.memories() if m.env is None or (m.env == e and m.repo in (None, repo))),
                   key=lambda m: m.slug)
    words = [(m, terms(m.slug.replace("-", " ") + " " + m.description)) for m in cands]
    result = PrResult()
    seen = {c.data.get("pr") for c in store.contexts()}
    for c in comments:
        if c["author"].endswith("[bot]"):
            continue
        if c["url"] and c["url"] in seen:
            result.already.append(c["url"])
            continue
        if c["association"] not in _TRUSTED:  # anyone can comment on a public repo; only maintainers feed memory
            result.unmatched.append({**c, "note": c["association"]})
            continue
        mine = terms(c["body"])
        score, best = max(((len(mine & w), m) for m, w in words), key=lambda p: p[0], default=(0, None))
        if score < _MIN_SHARED:
            result.unmatched.append(c)
            continue
        scope = "repo" if best.repo else "env" if best.env else "global"
        try:
            learn(store, cwd, memory=best.slug, mistake=" ".join(c["body"].split())[:_MISTAKE],
                  correction=f"PR review by {c['author']}", scope=scope, agent=agent, source="pr", pr=c["url"],
                  files=[c["path"]] if c["path"] else [], env=best.env, device=device)
        except Exception as err:  # untrusted input: never abort the run
            print(printable(f"skipped {c['url']}: {err}"), file=sys.stderr)
            continue
        seen.add(c["url"])
        result.linked.append((c["url"], best.path.relative_to(store.root).as_posix()))
    return result
