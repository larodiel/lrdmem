"""A device-local log of what hooks injected, and the report that tells which memories earn their place.

The log sits beside the config file (`recalls.jsonl`, one JSON object per line), never in the store: it is
per-device telemetry, not knowledge, and it would only add git churn.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lrdmem import config
from lrdmem.store import Store
from lrdmem.triggers import strs

CAP = 1_000_000  # bytes; past it the oldest half goes


def _path() -> Path:
    return config._path().parent / "recalls.jsonl"


def record(event: str, *, session: str, env: str, repo: str, refs: list[str] | None = None, cap: int = CAP,
           **extra) -> None:
    """Append one event (`start` or `inject`); never raises, so a full disk cannot break a hook."""
    entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, "session": session,
             "env": env, "repo": repo, **({"refs": refs} if refs is not None else {}), **extra}
    try:
        path = _path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        if path.stat().st_size > cap:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
            os.replace(tmp, path)
    except (OSError, UnicodeDecodeError):
        pass


def _entries(since: datetime) -> list[dict]:
    try:
        text = _path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
            if datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= since:
                out.append(e)
        except (ValueError, KeyError, TypeError):
            continue  # a torn or foreign line is not worth failing the report
    return out


def report(store: Store, days: int = 30, now: datetime | None = None) -> dict:
    """Sessions per repo, injections per memory (ref, count, last day), and the memories never injected:
    `unused_triggered` have file or command triggers that never fired, `index_only` have none and rely on the
    session-start index."""
    now = now or datetime.now(timezone.utc)
    sessions: dict[str, int] = {}
    counts: dict[str, list] = {}
    for e in _entries(now - timedelta(days=days)):
        if e.get("event") == "start":
            sessions[e.get("repo", "")] = sessions.get(e.get("repo", ""), 0) + 1
        elif e.get("event") == "inject":
            for ref in e.get("refs") or []:
                c = counts.setdefault(ref, [0, ""])
                c[0] += 1
                c[1] = max(c[1], str(e.get("ts", ""))[:10])
    injected = sorted(((ref, n, last) for ref, (n, last) in counts.items()), key=lambda x: (-x[1], x[0]))
    unused, index_only = [], []
    for m in store.memories():
        if m.ref in counts:
            continue
        triggered = strs(m.triggers.get("paths")) or strs(m.triggers.get("commands"))
        (unused if triggered else index_only).append(m.ref)
    return {"days": days, "sessions": sessions, "injected": injected, "unused_triggered": sorted(unused),
            "index_only": sorted(index_only)}
