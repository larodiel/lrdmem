"""Typed edges: evidence-backed, stateful relations between two memories, filed under the source's scope.

Confidence is never written; read it with `evidence.confidence(store, edge.evidence)`. Every change goes through
write.py and leaves a revision in the edge's own scope folder.
"""

import hashlib
from datetime import date

from lrdmem import revision, write
from lrdmem.frontmatter import dump
from lrdmem.model import EDGE_STATES, EDGE_TYPES, Evidence
from lrdmem.store import Edge, Memory, Store

_ACTION = {"invalidated": "invalidate", "superseded": "supersede"}


def _memory(store: Store, ref: str, role: str) -> Memory:
    node = store.node(ref)
    if not isinstance(node, Memory):
        raise write.Refused(f"{role} '{ref}' is not a memory")
    return node


def create(store: Store, source_ref: str, target_ref: str, type: str, *, evidence=(), state: str = "proposed",
           weight: float | None = None, by: str) -> tuple[Edge, bool]:
    """Write the edge source -> target and return (edge, created).

    The same (source, target, type) that is not invalidated returns the existing edge, with the new evidence added.
    The file is `<source scope>/edges/<date>-e<sha1(source|target|type)[:12]>.md`. Refused on an unknown type or
    state, a missing or non-memory endpoint, a self-edge, a target outside the source's env and `_global` (a
    `_global` source may only target `_global`), or a secret.
    """
    if type not in EDGE_TYPES:
        raise write.Refused(f"type must be one of {list(EDGE_TYPES)}")
    if state not in EDGE_STATES:
        raise write.Refused(f"state must be one of {list(EDGE_STATES)}")
    src, dst = _memory(store, source_ref, "source"), _memory(store, target_ref, "target")
    if src.ref == dst.ref:
        raise write.Refused("an edge cannot link a memory to itself")
    if dst.env not in (None, src.env):
        raise write.Refused("target must be in the source's environment or _global")
    items = [e if isinstance(e, Evidence) else Evidence.from_dict(e) for e in evidence]
    for e in items:
        write.guard("\n".join(e.to_dict().values()))
    same = (src.ref, dst.ref, type)
    old = next((e for e in store.edges() if (e.source, e.target, e.type) == same and e.state != "invalidated"), None)
    if old:
        for e in items:
            write.add_evidence(old.path, e)
        return store.node(old.ref), False
    folder = src.path.parent
    digest = hashlib.sha1("|".join(same).encode(), usedforsecurity=False).hexdigest()[:12]
    today = date.today().isoformat()
    write.target(store, folder, "edges/x.md")
    (folder / "edges").mkdir(exist_ok=True)
    path = write.target(store, folder, f"edges/{today}-e{digest}.md")
    if path.exists():  # an invalidated edge of the same triple lives there: keep it, start a new file
        path = write.unique(path.with_suffix(""))
    head = {"id": path.stem, "source": src.ref, "target": dst.ref, "type": type, "state": state,
            "weight": None if weight is None else str(weight), "created": today,
            "evidence": [e.to_dict() for e in items] or None}
    write.create(path, dump({k: v for k, v in head.items() if v is not None}) + "\n")
    ref = path.relative_to(store.root).with_suffix("").as_posix()
    revision.record(store, folder, ref, "create", {}, {"type": type, "state": state}, "edge created", by)
    return store.node(ref), True


def set_state(store: Store, edge: Edge, state: str, reason: str, by: str, superseded_by: str | None = None) -> None:
    """Move the edge to state (file kept) and record an invalidate, supersede or state_change revision.

    Refused on an unknown state, a secret, or an edge that changed since it was read.
    """
    if state not in EDGE_STATES:
        raise write.Refused(f"state must be one of {list(EDGE_STATES)}")
    write.guard(f"{reason}\n{by}\n{superseded_by or ''}")
    if (edge.state, superseded_by or "") == (state, ""):
        return
    changes = {"state": state, **({"superseded_by": superseded_by} if superseded_by else {})}
    before = {"state": edge.state, **({"superseded_by": edge.superseded_by} if superseded_by else {})}
    folder = edge.path.parent.parent
    path = write.target(store, folder, edge.path.relative_to(folder).as_posix())
    if not write.set_fields(path, changes, expect={"state": edge.data.get("state", "")}):
        raise write.Refused(f"'{edge.ref}' cannot be edited safely")
    revision.record(store, folder, edge.ref, _ACTION.get(state, "state_change"), before, changes, reason, by)
