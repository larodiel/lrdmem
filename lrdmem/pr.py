"""Link a GitHub PR's review comments to the memories they reinforce."""

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem.learn import LearnRefused, learn, terms
from lrdmem.lint import printable
from lrdmem.store import Store, resolve

_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:#.*|/files.*)?$")
_MIN_SHARED = 2
_MISTAKE = 300


@dataclass
class PrResult:
    """Outcome of learn_pr: (comment url, memory path relative to the store) pairs and the comments left over."""

    linked: list[tuple[str, str]] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)


def fetch_review_comments(url: str, run=subprocess.run) -> list[dict]:
    """Inline and review comments of a PR as dicts with id, url, path, body, author; ValueError on a bad URL."""
    m = _URL.match(url)
    if not m or "\n" in url:
        raise ValueError("expected https://github.com/<owner>/<repo>/pull/<n>")
    owner, repo, n = m.groups()

    def gh(*args):
        return json.loads(run(["gh", *args], capture_output=True, text=True, check=True).stdout or "null")

    out = [{"id": c.get("id"), "url": c.get("html_url", ""), "path": c.get("path") or "", "body": c.get("body") or "",
            "author": (c.get("user") or {}).get("login", "")}
           for c in gh("api", f"repos/{owner}/{repo}/pulls/{n}/comments", "--paginate") or []]
    pr_url = f"https://github.com/{owner}/{repo}/pull/{n}"
    for r in (gh("pr", "view", pr_url, "--json", "reviews") or {}).get("reviews", []):
        if (r.get("body") or "").strip():
            out.append({"id": None, "url": r.get("url", ""), "path": "", "body": r["body"],
                        "author": (r.get("author") or {}).get("login", "")})
    return out


def learn_pr(store: Store, cwd: Path, url: str, agent: str, run=subprocess.run, env: str | None = None) -> PrResult:
    """Record each human review comment as a context of its best-matching memory; LookupError if cwd is not opted in."""
    found = resolve(store, cwd, env)
    if not found:
        raise LookupError("this repository is not opted in to the store")
    comments = fetch_review_comments(url, run)
    e, repo = found
    cands = sorted((m for m in store.memories() if m.env is None or (m.env == e and m.repo in (None, repo))),
                   key=lambda m: m.slug)
    words = [(m, terms(m.slug.replace("-", " ") + " " + m.description)) for m in cands]
    result = PrResult()
    for c in comments:
        if c["author"].endswith("[bot]"):
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
                  files=[c["path"]] if c["path"] else [], env=best.env)
        except LearnRefused as err:
            print(printable(f"skipped {c['url']}: {err}"), file=sys.stderr)
            continue
        result.linked.append((c["url"], best.path.relative_to(store.root).as_posix()))
    return result
