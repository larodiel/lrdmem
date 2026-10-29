"""Record an experience: an immutable, dated observation filed under its scope's `experiences/` folder.

There is deliberately no function to edit one; history is only ever added to.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import dump
from lrdmem.learn import LearnRefused, scope_folder
from lrdmem.model import EXPERIENCE_KINDS, SOURCES
from lrdmem.store import Experience, Store, StoreFormatError

_SCOPES = ("repo", "env", "global")


class Refused(write.Refused):
    """Raised when an experience fails validation; subclass of write.Refused so one except catches both."""


def _slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()[:6]
    slug = re.sub(r"(^|-)req-[0-9a-f]{16}$", "", "-".join(words)[:40].strip("-")).strip("-")
    return slug or "experience"


def add(store: Store, cwd: Path, *, kind: str, agent: str, text: str, scope: str = "repo", files=(),
        source: str = "chat", ref: str | None = None, request_id: str | None = None, device: str = "",
        env: str | None = None, when: datetime | None = None) -> tuple[Experience, bool]:
    """Write an experience and return (it, created); the same `request_id` in the scope returns the existing one.

    Raises Refused on an unknown kind/source/scope, empty agent or text, a secret, a multi-line scalar field,
    an absolute or `..` file path, or a scope that is not available (repo not opted in, unknown env).
    """
    if kind not in EXPERIENCE_KINDS:
        raise Refused(f"kind must be one of {list(EXPERIENCE_KINDS)}")
    if source not in SOURCES:
        raise Refused(f"source must be one of {list(SOURCES)}")
    if scope not in _SCOPES:
        raise Refused(f"scope must be one of {list(_SCOPES)}")
    if not (agent or "").strip() or not (text or "").strip():
        raise Refused("agent and text are required")
    files = list(files)
    scalars = {"agent": agent, "device": device, "ref": ref or "", "request_id": request_id or "",
               **{f"files[{i}]": f for i, f in enumerate(files)}}
    if any(len(v.splitlines()) > 1 or "\n" in v or "\r" in v for v in scalars.values()):
        raise Refused("agent, device, ref, request_id and files must be single-line")
    if any(ord(c) < 32 or ord(c) == 127 for f in files for c in f):
        raise Refused("files must not contain control characters")
    if any(f.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", f) or ".." in re.split(r"[\\/]", f) for f in files):
        raise Refused("files must be relative paths without '..'")
    try:
        write.guard("\n".join([text, *scalars.values()]))
        write.refuse_home_paths("\n".join([text, *scalars.values()]), scope)
        folder, _ = scope_folder(store, cwd, scope, env)
    except (write.Refused, LearnRefused) as e:
        raise Refused(str(e)) from None
    exp = folder / "experiences"
    rid = hashlib.sha1(request_id.encode()).hexdigest()[:16] if request_id else ""
    if rid:
        for e in store.experiences():
            if e.path.parent == exp and e.request_id == request_id and e.kind and e.date:
                return e, False
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = when.strftime("%Y-%m-%d")
    data = {"date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind, "agent": agent, "device": device,
            "repo": store.scope_of(folder)[1], "files": files, "source": source, "ref": ref,
            "request_id": request_id}
    body = text.strip() + "\n"

    def render(stem: str) -> str:
        head = {"id": stem, **{k: v for k, v in data.items() if v not in (None, "", [])}}
        return dump(head) + "\n\n" + body
    try:
        write.target(store, folder, "experiences/x.md")
        exp.mkdir(exist_ok=True)
        if rid:  # deterministic name: O_EXCL makes same-day races safe
            # ponytail: a race straddling UTC midnight can write two; a lock file would close it
            path = exp / f"{day}-req-{rid}.md"
            created = write.create(path, render(path.stem))
        else:
            while True:
                path = write.unique(exp / f"{day}-{_slug(text)}")
                if created := write.create(path, render(path.stem)):
                    break
    except (write.Refused, StoreFormatError, OSError) as e:
        raise Refused(str(e)) from None
    ref = path.relative_to(store.root).with_suffix("").as_posix()
    node = store.node(ref)
    if not created and not (isinstance(node, Experience) and node.kind and node.date
                            and node.request_id == request_id):
        raise Refused(f"{ref} exists but is not the experience for this request_id")
    return node, created
