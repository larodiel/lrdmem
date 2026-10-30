"""Consolidation (spec §5.6, §5.6.1): the deterministic passes and the judgment queue.

Consolidation changes the store with no human approval, so every change is justified by evidence counted per
independence key (never by repetition), re-checked against the file under its lock (`expect=`), and recorded as a
revision by `consolidate`. Ids and contents derive only from the store and the UTC day, so two devices consolidating
the same history write the same files. What code cannot decide goes to the judgment queue, one file per item in
`<scope>/queue/<id>.md`, for an agent to resolve; the first resolution wins.
"""

import fcntl
import hashlib
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from lrdmem import edges, evidence, revision, write
from lrdmem.frontmatter import dump
from lrdmem.graph import Graph
from lrdmem.recall import _day
from lrdmem.search import DUP_SUSPECT, bm25_scores, visible
from lrdmem.session import state_dir
from lrdmem.store import Memory, QueueItem, Store

BY = "consolidate"
DECISIONS = {"possible-duplicate": ("merge", "keep-separate"), "promote-hypothesis": ("promote", "reject"),
             "generalize-procedure": ("create", "reject")}
_LIVE = ("candidate", "active", "contested")


@dataclass
class ConsolidateResult:
    """Refs changed by each pass, ids of queue items created, and whether work was left for the next run."""

    merged: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    contested: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    obsoleted: list[str] = field(default_factory=list)
    edges_validated: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    incomplete: bool = False

    def to_dict(self) -> dict:
        """Plain dict for `--json`."""
        return asdict(self)


class _OutOfTime(Exception):
    pass


@contextmanager
def lock():
    """Hold the per-machine consolidation lock (`$TMPDIR/lrdmem-<uid>/consolidate.lock`, non-blocking flock).

    Yields True when held; False, holding nothing, when another run holds it or the state dir cannot be trusted.
    """
    d = state_dir()
    try:
        fd = os.open(d / "consolidate.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600) if d else None
    except OSError:
        fd = None
    held = fd is not None
    if held:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = False
    try:
        yield held
    finally:
        if fd is not None:
            os.close(fd)


def item_id(kind: str, subjects: list[str]) -> str:
    """`q-<sha1(kind|sorted subjects)[:12]>`: the same judgment gets the same id on every device and every day."""
    return "q-" + hashlib.sha1("|".join([kind, *sorted(subjects)]).encode(), usedforsecurity=False).hexdigest()[:12]


def run(store: Store, *, budget: float = 20.0, now: datetime | None = None) -> ConsolidateResult:
    """Run the deterministic passes in order, within `budget` seconds, under the per-machine lock.

    (1) `valid_until` past -> obsolete; (2) exact duplicates in one scope folder -> merged into the oldest;
    (3) candidate -> active with >= 2 independent supports and no validated contradicts (never a hypothesis);
    (4) hypothesis with outcome/check evidence -> queue `promote-hypothesis`; (5) active <-> contested;
    (6) proposed edges with >= 2 independent positive evidences -> validated; (7) >= 3 independent experiences on a
    memory with no linked procedure -> queue `generalize-procedure`; (8) paraphrase suspects -> queue
    `possible-duplicate`. The budget is checked between items; what is left (or a held lock) gives
    `incomplete=True`, and a re-run finishes it without duplicating anything.
    """
    with lock() as held:
        if not held:
            return ConsolidateResult(incomplete=True)
        return _Run(store, budget, now).go()


def _norm(m: Memory) -> str:
    return " ".join(f"{m.description}\n{m.body}".casefold().split())


def _raw_state(m: Memory) -> str:
    meta = m.data.get("metadata")
    old = meta.get("state", "") if isinstance(meta, dict) else ""
    return old if isinstance(old, str) else ""


def _v2(m: Memory) -> bool:
    """A memory written with v2 fields; v0.1 memories were curated by hand and are never compared among themselves."""
    meta = m.data.get("metadata")
    return "evidence" in m.data or (isinstance(meta, dict) and ("state" in meta or "memory_type" in meta))


def _set_state(store: Store, m: Memory, state: str, action: str, reason: str, by: str) -> None:
    """Set a memory's state (checked under the file lock against the state read) and record the revision."""
    old = _raw_state(m)
    folder = m.path.parent
    path = write.target(store, folder, m.path.name)
    if not write.set_fields(path, {"state": state}, expect={"state": old}):
        raise write.Refused(f"'{m.ref}' cannot be edited safely")
    # ponytail: a crash between the edit and the revision leaves the state unrecorded; lint/revert can't see it
    revision.record(store, folder, m.ref, action, {"state": old}, {"state": state}, reason, by)


def _merge(store: Store, kept: Memory, other: Memory, reason: str, by: str, index: evidence.Index) -> None:
    """Fold other into kept: its evidence (legacy contexts included) added to kept, a `merge` revision on kept, a
    validated `supersedes` edge kept -> other, other superseded. Every step is idempotent, so a re-run completes it."""
    folder = kept.path.parent
    path = write.target(store, folder, kept.path.name)
    for item in evidence.memory_evidence(store, other, index):
        write.add_evidence(path, item)
    revision.record(store, folder, kept.ref, "merge", {"evidence": ""}, {"evidence": other.ref}, reason, by)
    e, _ = edges.create(store, kept.ref, other.ref, "supersedes", state="validated", by=by)
    if e.state != "validated":
        edges.set_state(store, e, "validated", reason, by)
    _set_state(store, other, "superseded", "supersede", f"merged into '{kept.ref}'", by)


class _Run:
    def __init__(self, store: Store, budget: float, now: datetime | None):
        self.store = store
        self.deadline = time.monotonic() + budget
        now = now or datetime.now(timezone.utc)
        self.today = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
        self.index = evidence.Index(store)  # experiences never change during a run; memories are re-read per pass
        self.r = ConsolidateResult()

    def tick(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _OutOfTime

    def go(self) -> ConsolidateResult:
        try:
            for step in (self.obsolete, self.fuse, self.promote, self.hypotheses, self.contest, self.validate_edges,
                         self.procedures, self.duplicates):
                step(self.store.memories())
        except _OutOfTime:
            self.r.incomplete = True
        return self.r

    def items(self, m: Memory) -> list:
        return evidence.memory_evidence(self.store, m, self.index)

    def keys(self, items, kinds=("supports",)) -> set:
        """Distinct independence keys of positive-weight items of these kinds that resolve to experiences."""
        return {evidence.independence_key(self.store, e, self.index) for e in items
                if e.kind in kinds and e.weight > 0 and e.ref in self.index.experiences}

    def queue(self, kind: str, subjects: list[str], folder, reason: str) -> None:
        qid = item_id(kind, subjects)
        if os.path.lexists(folder / "queue" / f"{qid}.md"):
            return  # open or resolved: never re-opened
        self.tick()
        path = write.target(self.store, folder, f"queue/{qid}.md")
        head = {"id": qid, "kind": kind, "subjects": sorted(subjects), "status": "open", "resolution": "",
                "resolved_by": "", "resolved_at": ""}
        path.parent.mkdir(exist_ok=True)
        if write.create(path, dump(head) + "\n\n" + reason + "\n"):
            self.r.queued.append(qid)

    # (1)
    def obsolete(self, mems: list[Memory]) -> None:
        for m in mems:
            until = _day(m.valid_until)
            if m.state not in ("obsolete", "superseded") and until and until < self.today:
                self.tick()
                _set_state(self.store, m, "obsolete", "state_change", f"valid_until {m.valid_until} has passed", BY)
                self.r.obsoleted.append(m.ref)

    # (2)
    def fuse(self, mems: list[Memory]) -> None:
        groups: dict[tuple, list[Memory]] = {}
        for m in mems:
            if m.state in _LIVE and _norm(m):
                groups.setdefault((str(m.path.parent), m.memory_type, _norm(m)), []).append(m)
        for key in sorted(groups):
            kept, *others = sorted(groups[key], key=lambda m: (m.created, m.ref))
            for other in others:
                self.tick()
                _merge(self.store, kept, other, f"exact duplicate of '{kept.ref}' (same normalised content)", BY,
                       self.index)
                self.r.merged.append(other.ref)

    def contradicted(self, states: tuple[str, ...]) -> dict[str, set[str]]:
        """ref -> refs on the other end of a contradicts edge in one of these states."""
        out: dict[str, set[str]] = {}
        for e in self.store.edges():
            if e.type == "contradicts" and e.state in states:
                out.setdefault(e.source, set()).add(e.target)
                out.setdefault(e.target, set()).add(e.source)
        return out

    # (3)
    def promote(self, mems: list[Memory]) -> None:
        against = self.contradicted(("validated",))
        for m in mems:
            if m.state != "candidate" or m.memory_type == "hypothesis" or m.ref in against:
                continue
            n = len(self.keys(self.items(m)))
            if n >= evidence.PROMOTE_SUPPORTS:
                self.tick()
                _set_state(self.store, m, "active", "state_change",
                           f"{n} independent supports; no validated contradicts", BY)
                self.r.promoted.append(m.ref)

    # (4)
    def hypotheses(self, mems: list[Memory]) -> None:
        for m in mems:
            if m.memory_type == "hypothesis" and m.state in _LIVE and \
                    evidence.has_outcome(self.store, self.items(m), self.index):
                self.queue("promote-hypothesis", [m.ref], m.path.parent,
                           f"The hypothesis '{m.ref}' has outcome evidence. Decide: write its generalisation as a "
                           "semantic or procedural memory and promote it into that memory, or reject it.")

    # (5)
    def contest(self, mems: list[Memory]) -> None:
        by_ref = {m.ref: m for m in mems}
        conf = {m.ref: evidence.confidence(self.store, self.items(m), self.index) for m in mems}
        links = self.contradicted(("proposed", "validated"))
        for m in mems:
            if m.state not in ("active", "contested"):
                continue
            pos, neg = evidence.balance(self.store, self.items(m), self.index)
            s, r = sum(pos.values()), sum(neg.values())
            refuted = len([v for v in neg.values() if v > 0]) >= 2 and r > s  # one refutation never decides; a tie keeps it
            opp = max((conf[o] for o in links.get(m.ref, ()) if o in by_ref and by_ref[o].state in ("active", "contested")),
                      default=None)
            mine = conf[m.ref]
            if m.state == "active" and (refuted or (opp is not None and opp > mine)):
                self.tick()
                why = (f"refutes {r:g} from {len(neg)} independent sources outweigh supports {s:g}" if refuted
                       else "a contradicting memory has higher confidence")
                _set_state(self.store, m, "contested", "state_change", why, BY)
                self.r.contested.append(m.ref)
            elif m.state == "contested" and not refuted and s > r and (opp is None or mine > opp):
                self.tick()
                _set_state(self.store, m, "active", "state_change",
                           f"supports {s:g} dominate refutes {r:g} and every contradicting memory", BY)
                self.r.restored.append(m.ref)

    # (6)
    def validate_edges(self, mems: list[Memory]) -> None:
        for e in self.store.edges():
            n = len(self.keys(e.evidence, ("supports", "observed_in"))) if e.state == "proposed" else 0
            if n >= 2:
                self.tick()
                edges.set_state(self.store, e, "validated", f"{n} independent evidences", BY)
                self.r.edges_validated.append(e.ref)

    def graph_links(self) -> set[frozenset]:
        g = Graph(self.store)
        return {frozenset((e.source, e.target)) for e in g.typed_edges() if e.state in ("proposed", "validated")}

    # (7)
    def procedures(self, mems: list[Memory]) -> None:
        procedural = {m.ref for m in mems if m.memory_type == "procedural"}
        links = self.graph_links()
        for m in mems:
            if m.state not in _LIVE or m.memory_type in ("procedural", "hypothesis"):
                continue
            # explicit evidence only: legacy v0.1 contexts back rules that already exist
            n = len(self.keys(m.evidence, ("supports", "observed_in")))
            if n >= evidence.REPEAT_PROCEDURE and not any(frozenset((m.ref, p)) in links for p in procedural):
                self.queue("generalize-procedure", [m.ref], m.path.parent,
                           f"'{m.ref}' is backed by {n} independent experiences and no procedure is linked to it. "
                           "Decide: write the procedure as a procedural memory and create it from this item, "
                           "or reject it.")

    # (8)
    def duplicates(self, mems: list[Memory]) -> None:
        folders: dict[str, list[Memory]] = {}
        for m in mems:
            if m.state == "active":
                folders.setdefault(str(m.path.parent), []).append(m)
        links = None
        for key in sorted(folders):
            ms = folders[key]
            if len(ms) < 2 or not any(_v2(m) for m in ms):
                continue
            links = self.graph_links() if links is None else links
            docs = [(m.ref, f"{m.description}\n{m.body}") for m in ms]
            rel: dict[str, dict[str, float]] = {}
            # ponytail: O(n^2) BM25 per scope folder, budget-checked per memory; an inverted index if it gets slow
            for qid, hits in bm25_scores(docs, docs):
                self.tick()
                scores = {i: s for i, s, _ in hits}
                if scores.get(qid):
                    rel[qid] = {i: s / scores[qid] for i, s in scores.items()}
            v2 = {m.ref for m in ms if _v2(m)}
            for a in sorted(rel):
                for b in sorted(rel):
                    if a < b and (a in v2 or b in v2) and frozenset((a, b)) not in links and \
                            (rel[a].get(b, 0) + rel[b].get(a, 0)) / 2 >= DUP_SUSPECT:
                        self.queue("possible-duplicate", [a, b], ms[0].path.parent,
                                   f"'{a}' and '{b}' read alike in one scope. Decide: merge them or keep them "
                                   "separate.")


def open_items(store: Store) -> list[QueueItem]:
    """Open judgment-queue items, by id."""
    return sorted((q for q in store.queue() if q.status == "open"), key=lambda q: q.id)


def _subject(store: Store, ref: str, folder) -> Memory:
    node = store.node(ref)
    if not isinstance(node, Memory) or node.path.parent != folder:
        raise write.Refused(f"subject '{ref}' is not a memory of this item's scope")
    return node


def resolve(store: Store, item: str, decision: str, *, into: str | None = None, note: str = "",
            by: str = "human") -> str:
    """Apply an agent's decision on a queue item and mark it resolved; return a one-line message.

    possible-duplicate: `merge` (like the fusion pass: the oldest is kept) or `keep-separate`; promote-hypothesis:
    `promote` with `into`, an existing non-hypothesis memory visible from the hypothesis's scope (the hypothesis
    becomes superseded and a validated `generalizes` edge goes from `into` to it) or `reject`; generalize-procedure:
    `create` with `into`, a procedural memory visible from the subject's scope (a validated `generalizes` edge from
    it to the subject) or `reject`. The first resolution wins: a resolved item returns "already resolved by <who> at
    <when>" and changes nothing. Refused (write.Refused) on an unknown or ambiguous item, a decision the kind does
    not take, a missing or invalid `into`, or subjects outside the item's scope folder.
    """
    found = [q for q in store.queue() if item in (q.id, q.path.stem)]
    if len(found) != 1:
        raise write.Refused(f"{'ambiguous' if found else 'unknown'} queue item '{item}'")
    q = found[0]
    if q.status == "resolved":
        return f"already resolved by {q.data.get('resolved_by', '')} at {q.data.get('resolved_at', '')}"
    if decision not in DECISIONS.get(q.kind, ()):
        raise write.Refused(f"{q.kind or 'this item'} takes one of {list(DECISIONS.get(q.kind, ()))}")
    for v in (by, note):
        if len(v.splitlines()) > 1 or "\n" in v or "\r" in v:
            raise write.Refused("agent and note must be single-line")
    if not by.strip():
        raise write.Refused("agent is required")
    folder = q.path.parent.parent
    subjects = [_subject(store, ref, folder) for ref in q.subjects]
    apply = _plan(store, q, decision, subjects, into, by)
    path = write.target(store, folder, f"queue/{q.path.name}")
    claim = {"status": "resolved", "resolution": decision, "resolved_by": by,
             "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **({"note": note} if note else {})}
    try:
        if not write.set_fields(path, claim, expect={"status": "open"}):
            raise write.Refused(f"queue item '{q.id}' cannot be edited safely")
    except write.Refused:
        now = [x for x in store.queue() if x.path == q.path]
        if now and now[0].status == "resolved":  # resolved meanwhile, elsewhere: first resolution wins
            return f"already resolved by {now[0].data.get('resolved_by', '')} at {now[0].data.get('resolved_at', '')}"
        raise
    try:
        apply()
    except Exception:
        write.set_fields(path, {"status": "open", "resolution": "", "resolved_by": "", "resolved_at": ""})
        raise
    return f"resolved {q.id}: {decision}"


def _plan(store: Store, q: QueueItem, decision: str, subjects: list[Memory], into: str | None, by: str):
    """Every check of a decision, no writes; the function that applies it."""
    if decision in ("keep-separate", "reject"):
        return lambda: None
    if decision == "merge":
        if len(subjects) != 2 or any(m.state not in _LIVE for m in subjects):
            raise write.Refused("merge needs two live memories")
        kept, other = sorted(subjects, key=lambda m: (m.created, m.ref))
        return lambda: _merge(store, kept, other, f"duplicate judged by {by} ({q.id})", by, evidence.Index(store))
    if len(subjects) != 1:
        raise write.Refused(f"{q.kind} needs one subject")
    subject = subjects[0]
    target = store.node(into) if into else None
    if not isinstance(target, Memory):
        raise write.Refused(f"{decision} needs --into <ref> of an existing memory")
    if not visible(target, subject.env, subject.repo) or subject.env not in (None, target.env):
        raise write.Refused(f"'{into}' is not visible from '{subject.ref}''s scope")
    if decision == "promote":
        if subject.memory_type != "hypothesis" or subject.state not in _LIVE:
            raise write.Refused(f"'{subject.ref}' is not a live hypothesis")
        if target.memory_type == "hypothesis":
            raise write.Refused("a hypothesis is promoted into a non-hypothesis memory")
    elif target.memory_type != "procedural":
        raise write.Refused("create needs --into <ref> of a procedural memory")
    reason = f"{decision} judged by {by} ({q.id})"

    def apply():
        e, _ = edges.create(store, target.ref, subject.ref, "generalizes", state="validated", by=by)
        if e.state != "validated":
            edges.set_state(store, e, "validated", reason, by)
        if decision == "promote":
            _set_state(store, store.node(subject.ref), "superseded", "supersede", f"generalised by '{target.ref}'",
                       by)
    return apply
