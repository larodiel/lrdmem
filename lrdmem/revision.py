"""Revisions: the semantic history of memories and edges, and reverting one."""

import hashlib
import json
from datetime import date
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import dump
from lrdmem.model import REVISION_ACTIONS
from lrdmem.store import Edge, Memory, Store


def _canonical(values: dict) -> str:
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _fields(values: dict) -> dict[str, str]:
    out = {str(k): str(v) for k, v in values.items()}
    bad = [k for k in out if not write.FIELD.match(k)]
    if bad:
        raise write.Refused(f"invalid field name(s): {bad}")
    return out


def record(store: Store, folder: Path, target_ref: str, action: str, before: dict, after: dict, reason: str,
           by: str) -> Path:
    """Write a revision of target_ref into folder/revisions and return its path.

    The id is `<date>-r<sha1(target|action|before|after)[:8]>`, so recording the same change twice the same day
    writes one file and returns the existing path. before/after hold the changed fields' old and new values as
    strings. Refused on an unknown action, a target that is not a memory or edge visible from folder, or a secret.
    """
    if action not in REVISION_ACTIONS:
        raise write.Refused(f"action must be one of {list(REVISION_ACTIONS)}")
    before, after = _fields(before), _fields(after)
    today = date.today().isoformat()
    digest = hashlib.sha1("|".join((target_ref, action, _canonical(before), _canonical(after))).encode(),
                          usedforsecurity=False).hexdigest()[:8]
    rid = f"{today}-r{digest}"
    path = write.target(store, folder, f"revisions/{rid}.md")
    node = store.node(target_ref)
    if not isinstance(node, (Memory, Edge)) or node.env not in (None, store.scope_of(folder)[0]):
        raise write.Refused(f"'{target_ref}' is not a memory or edge visible from this scope")
    text = dump({"id": rid, "date": today, "target": target_ref, "action": action, "before": before,
                 "after": after, "reason": reason, "by": by}) + "\n"
    write.guard(text)  # before mkdir: a refused revision leaves nothing behind
    path.parent.mkdir(parents=True, exist_ok=True)
    write.create(path, text)
    return path


def _current(node: Memory | Edge, key: str):
    meta = node.data.get("metadata") if isinstance(node, Memory) else None
    if isinstance(meta, dict) and key in meta:
        return meta[key]
    return node.data.get(key, "")


def revert(store: Store, rev_id: str, by: str) -> Path:
    """Restore the `before` values of a revision with set_fields and record a `revert` revision; return its path.

    rev_id is a revision id or store-relative ref. Refused when it is unknown or ambiguous, has nothing to restore,
    its target is gone, the target's current values differ from the revision's `after` (changed since), or the
    target cannot be edited safely.
    """
    found = [r for r in store.revisions() if rev_id in (r.id, r.ref)]
    if len(found) != 1:
        raise write.Refused(f"{'ambiguous' if found else 'unknown'} revision '{rev_id}'")
    rev = found[0]
    if not rev.before:
        raise write.Refused(f"revision '{rev.id}' has no previous values to restore")
    node = store.node(rev.target)
    if not isinstance(node, (Memory, Edge)):
        raise write.Refused(f"target '{rev.target}' of revision '{rev.id}' no longer exists")
    changes = {k: rev.before.get(k, "") for k in {**rev.after, **rev.before}}
    current = {k: _current(node, k) for k in changes}
    if any(current[k] != v for k, v in rev.after.items()):
        raise write.Refused(f"'{rev.target}' changed since revision '{rev.id}'; refusing to revert")
    folder = node.path.parent if isinstance(node, Memory) else node.path.parent.parent
    path = write.target(store, folder, node.path.relative_to(folder).as_posix())
    write.guard(_canonical(current) + _canonical(changes))  # refuse before editing, not between edit and record
    if not write.set_fields(path, changes):
        raise write.Refused(f"'{rev.target}' cannot be edited safely; revert it by hand")
    return record(store, rev.path.parent.parent, rev.target, "revert", current, changes, f"revert {rev.id}", by)
