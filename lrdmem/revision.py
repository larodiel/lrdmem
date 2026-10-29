"""Revisions: the semantic history of memories and edges, and reverting one."""

import hashlib
import json
from datetime import date
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import dump
from lrdmem.model import REVISION_ACTIONS
from lrdmem.store import Edge, Memory, Revision, Store


def _canonical(values: dict) -> str:
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _fields(values: dict) -> dict[str, str]:
    bad = [k for k, v in values.items() if not isinstance(k, str) or not write.FIELD.match(k) or not isinstance(v, str)]
    if bad:
        raise write.Refused(f"invalid field name(s) or non-string value(s): {bad}")
    return dict(values)


_RESTORABLE = {Memory: {"state", "valid_from", "valid_until", "description", "memory_type"},
               Edge: {"state", "superseded_by", "weight", "valid_from", "valid_until"}}


def _tip(store: Store, folder: Path, target_ref: str) -> Revision | None:
    """The latest revision of target_ref in folder/revisions: the chain end (no other revision names it as prev),
    newest first. Revisions of other scopes are never trusted."""
    revs = [r for r in store.revisions() if r.path.parent == folder / "revisions" and r.target == target_ref]
    prevs = {r.data.get("prev") for r in revs}
    return max([r for r in revs if r.id not in prevs] or revs, key=lambda r: (r.date, r.id), default=None)


def _prepare(store: Store, folder: Path, target_ref: str, action: str, before: dict, after: dict, reason: str,
             by: str) -> tuple[Path, str | None]:
    """(path, text to write) of a revision, validated and guarded; text is None when the change is already the
    target's latest revision."""
    if action not in REVISION_ACTIONS:
        raise write.Refused(f"action must be one of {list(REVISION_ACTIONS)}")
    before, after = _fields(before), _fields(after)
    write.guard("\n".join([target_ref, reason, by, *before.values(), *after.values()]))
    write.target(store, folder, "revisions")  # scope, containment and symlinks first, before any dedupe
    node = store.node(target_ref)
    if not isinstance(node, (Memory, Edge)) or node.env not in (None, store.scope_of(folder)[0]):
        raise write.Refused(f"'{target_ref}' is not a memory or edge visible from this scope")
    tip = _tip(store, folder, target_ref)
    if tip and (tip.action, tip.before, tip.after) == (action, before, after):
        return tip.path, None
    prev = tip.id if tip else ""
    today = date.today().isoformat()
    digest = hashlib.sha1("|".join((prev, target_ref, action, _canonical(before), _canonical(after))).encode(),
                          usedforsecurity=False).hexdigest()[:8]
    rid = f"{today}-r{digest}"
    path = write.target(store, folder, f"revisions/{rid}.md")
    text = dump({"id": rid, "date": today, "target": target_ref, "action": action, "before": before,
                 "after": after, "reason": reason, "by": by, **({"prev": prev} if prev else {})}) + "\n"
    write.guard(text)
    return path, text


def _write(path: Path, text: str | None) -> Path:
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write.create(path, text)
    return path


def record(store: Store, folder: Path, target_ref: str, action: str, before: dict, after: dict, reason: str,
           by: str) -> Path:
    """Write a revision of target_ref into folder/revisions and return its path.

    The id is `<date>-r<sha1(prev|target|action|before|after)[:8]>`, prev being the id of the target's latest
    revision in folder/revisions ("" for the first): stores with the same history get the same ids, and every real
    change gets its own file, while recording a change that is already the target's latest revision is a no-op
    returning that path. before/after hold the changed fields' old and new values, which must be strings. Refused on
    an unknown action, a target that is not a memory or edge visible from folder, or a secret (checked on the raw and
    the written values).
    """
    return _write(*_prepare(store, folder, target_ref, action, before, after, reason, by))


def _current(node: Memory | Edge, key: str):
    meta = node.data.get("metadata") if isinstance(node, Memory) else None
    if isinstance(meta, dict) and key in meta:
        return meta[key]
    return node.data.get(key, "")


def revert(store: Store, rev_id: str, by: str) -> Path:
    """Restore the `before` values of a revision with set_fields and record a `revert` revision; return its path.

    rev_id is a revision id or store-relative ref. Only state, validity and description/memory_type (memories) or
    state, superseded_by, weight and validity (edges) are restored. A field the revision added (absent from
    `before`) is set to "" rather than removed. Refused when the revision is unknown or ambiguous, has nothing to
    restore, names another field, targets a node gone or not visible from the revision's scope, the target changed
    since (current values differ from `after`, checked under the file lock), or the target cannot be edited safely.
    The revert revision is validated before the target is touched.
    """
    found = [r for r in store.revisions() if rev_id in (r.id, r.ref)]
    if len(found) != 1:
        raise write.Refused(f"{'ambiguous' if found else 'unknown'} revision '{rev_id}'")
    rev = found[0]
    if not rev.before:
        raise write.Refused(f"revision '{rev.id}' has no previous values to restore")
    node = store.node(rev.target)
    if not isinstance(node, (Memory, Edge)):  # visibility from the revision's scope: _prepare, before any edit
        raise write.Refused(f"target '{rev.target}' of revision '{rev.id}' no longer exists")
    changes = {k: rev.before.get(k, "") for k in {**rev.after, **rev.before}}
    extra = sorted(set(changes) - _RESTORABLE[type(node)])
    if extra:
        raise write.Refused(f"revert does not restore {extra}; change them by hand")
    current = {k: _current(node, k) for k in changes}
    if not all(isinstance(v, str) for v in current.values()):
        raise write.Refused(f"'{rev.target}' holds a non-scalar value in {sorted(changes)}; revert it by hand")
    if any(current[k] != v for k, v in rev.after.items()):
        raise write.Refused(f"'{rev.target}' changed since revision '{rev.id}'; refusing to revert")
    folder = node.path.parent if isinstance(node, Memory) else node.path.parent.parent
    path = write.target(store, folder, node.path.relative_to(folder).as_posix())
    path_rev, text_rev = _prepare(store, rev.path.parent.parent, rev.target, "revert", current, changes,
                                  f"revert {rev.id}", by)
    if text_rev is None:
        raise write.Refused(f"nothing to revert: '{rev.target}' is already at this state ({path_rev.stem})")
    if not write.set_fields(path, changes, expect=rev.after):  # Refused if it changed since, under the lock
        raise write.Refused(f"'{rev.target}' cannot be edited safely; revert it by hand")
    return _write(path_rev, text_rev)
