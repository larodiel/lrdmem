"""Validate and apply the memory candidates an agent extracted from one experience (spec §5.2, §5.4).

The agent proposes; this module decides what is safe to persist. Each candidate is validated in full before anything
is written for it, and a refused candidate leaves no file behind.
"""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from lrdmem import edges, evidence, revision, write
from lrdmem.frontmatter import dump
from lrdmem.learn import _RESERVED, _SCOPES, _SLUG, LearnRefused, _check_text, scope_folder
from lrdmem.model import EDGE_TYPES, MEMORY_TYPES, Evidence
from lrdmem.store import AmbiguousRepo, Experience, Memory, Store, StoreFormatError, resolve

CLASSIFICATIONS = ("new", "equivalent", "complementary", "contradicts", "insufficient")
_KEYS = {"slug", "memory_type", "description", "body", "why", "how", "scope", "triggers", "classification",
         "relates_to", "edge_type"}
_TEXT = ("body", "why", "how")  # the only multi-line fields
_TRIGGERS = ("paths", "commands", "repos")
_STRONG = ("correction", "review")  # human corrections: memories start active, the experience supports them
_RELATES = ("equivalent", "complementary", "contradicts")


@dataclass
class ProposeResult:
    """Refs of created memories, memories that gained evidence and new edges, plus (candidate index, reason)."""

    created: list[str] = field(default_factory=list)
    evidence_added: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    refused: list[tuple[int, str]] = field(default_factory=list)


def _episodic(c) -> bool:
    return isinstance(c, dict) and c.get("memory_type") == "episodic" and c.get("classification") != "equivalent"


def apply(store: Store, cwd: Path, payload: dict, *, agent: str, device: str = "",
          env: str | None = None) -> ProposeResult:
    """Apply `{"experience": ref, "candidates": [...]}` and return what was written and what was refused.

    State: `active` when the experience is a `correction` or `review` and the candidate is not a hypothesis nor
    `insufficient`, else `candidate`; the experience is `supports` (weight 1) evidence for those, `observed_in`
    otherwise. `equivalent` adds that evidence to `relates_to` (+ a merge revision) and creates nothing;
    `complementary`/`contradicts` create the memory and a proposed edge (`edge_type`, default related_to /
    contradicts), and for `contradicts` the side with the lower confidence (a tie: the new one) becomes `contested`
    (+ revision). Every new non-episodic memory gets a proposed `derived_from` edge to the payload's first episodic
    memory. A candidate failing any check is refused with its index and reason; the others proceed. Refused
    (write.Refused) for a malformed payload or an experience ref that does not resolve. `device` is accepted for
    symmetry with the other writers; the experience already records it.
    """
    if not isinstance(payload, dict) or not set(payload) <= {"experience", "candidates"}:
        raise write.Refused("payload must be a JSON object with experience and candidates")
    ref = payload.get("experience")
    exp = store.node(ref) if isinstance(ref, str) else None
    if not isinstance(exp, Experience) or not exp.kind:
        raise write.Refused("experience must be the ref of a recorded experience")
    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        raise write.Refused("candidates must be a non-empty list")
    if not isinstance(agent, str) or not agent.strip() or "\n" in agent or "\r" in agent:
        raise write.Refused("agent is required and must be single-line")
    write.guard(agent)
    item = Evidence(exp.ref, "supports" if exp.kind in _STRONG else "observed_in", 1.0)
    result, episode = ProposeResult(), None
    for i in sorted(range(len(cands)), key=lambda i: not _episodic(cands[i])):  # episodes first: derived_from targets
        try:
            made = _one(store, cwd, exp, item, cands[i], agent, env, episode, result)
        except (write.Refused, LearnRefused, StoreFormatError, OSError) as err:
            result.refused.append((i, str(err)))
            continue
        if episode is None and made and _episodic(cands[i]):
            episode = made
    result.refused.sort()
    return result


def _check(c) -> str:
    """The candidate's classification after the shape checks shared by every classification."""
    if not isinstance(c, dict):
        raise write.Refused("a candidate must be a JSON object")
    unknown = sorted(set(c) - _KEYS)
    if unknown:
        raise write.Refused(f"unknown keys: {unknown}")
    if any(not isinstance(v, str) for k, v in c.items() if k != "triggers"):
        raise write.Refused("candidate values must be strings (triggers: a map of string lists)")
    if any("\n" in v or "\r" in v for k, v in c.items() if k not in _TEXT and isinstance(v, str)):
        raise write.Refused("every field but body, why and how must be single-line")
    if c.get("classification") not in CLASSIFICATIONS:
        raise write.Refused(f"classification must be one of {list(CLASSIFICATIONS)}")
    if "memory_type" in c and c["memory_type"] not in MEMORY_TYPES:
        raise write.Refused(f"memory_type must be one of {list(MEMORY_TYPES)}")
    if "edge_type" in c and c["edge_type"] not in EDGE_TYPES:
        raise write.Refused(f"edge_type must be one of {list(EDGE_TYPES)}")
    write.guard("\n".join(v for v in c.values() if isinstance(v, str)))
    return c["classification"]


def _relates(store: Store, c: dict, cls: str) -> Memory | None:
    ref = c.get("relates_to")
    if ref is None:
        if cls in _RELATES:
            raise write.Refused(f"relates_to is required for {cls}")
        if "edge_type" in c:
            raise write.Refused("edge_type needs relates_to")
        return None
    target = store.node(ref)
    if not isinstance(target, Memory):
        raise write.Refused(f"relates_to '{ref}' is not a memory")
    return target


def _one(store: Store, cwd: Path, exp: Experience, item: Evidence, c, agent: str, env: str | None,
         episode: str | None, result: ProposeResult) -> str | None:
    """Validate then apply one candidate; the ref of the memory it created, None for equivalent."""
    cls = _check(c)
    target = _relates(store, c, cls)
    if cls == "equivalent":
        _equivalent(store, cwd, exp, item, c, target, agent, env, result)
        return None
    return _create(store, cwd, exp, item, c, cls, target, agent, env, episode, result)


def _equivalent(store: Store, cwd: Path, exp: Experience, item: Evidence, c: dict, target: Memory, agent: str,
                env: str | None, result: ProposeResult) -> None:
    if c.get("memory_type") == "hypothesis" and target.memory_type != "hypothesis":
        raise write.Refused(f"a hypothesis cannot merge into the {target.memory_type} memory '{target.ref}'")
    try:
        here = resolve(store, cwd, env)
    except AmbiguousRepo:
        here = None
    if target.env not in (None, here[0] if here else None):
        raise write.Refused(f"relates_to '{target.ref}' is outside this repository's environment and _global")
    if exp.env not in (None, target.env):
        raise write.Refused(f"the experience is not visible from '{target.ref}'")
    folder = target.path.parent
    path = write.target(store, folder, target.path.name)
    write.target(store, folder, "revisions/x.md")
    if write.add_evidence(path, item):  # False: already there (or not an editable header), nothing to record
        # ponytail: a revision failing after the evidence line leaves the evidence unrecorded; both are re-runnable
        revision.record(store, folder, target.ref, "merge", {"evidence": ""}, {"evidence": exp.ref},
                        f"equivalent evidence from {exp.ref}", agent)
        result.evidence_added.append(target.ref)


def _create(store: Store, cwd: Path, exp: Experience, item: Evidence, c: dict, cls: str, target: Memory | None,
            agent: str, env: str | None, episode: str | None, result: ProposeResult) -> str:
    mtype = c.get("memory_type")
    need = ["slug", "memory_type", "description", "body", "scope"] + ([] if mtype == "episodic" else ["why", "how"])
    missing = [k for k in need if not c.get(k, "").strip()]
    if missing:
        raise write.Refused(f"missing {', '.join(missing)}")
    slug, scope = c["slug"], c["scope"]
    if not _SLUG.match(slug) or slug in _RESERVED:
        raise write.Refused("slug must match ^[a-z0-9][a-z0-9-]{0,63}$ and not be memory, readme or contexts")
    if scope not in _SCOPES:
        raise write.Refused(f"scope must be one of {list(_SCOPES)}")
    triggers = c.get("triggers", {})
    if (not isinstance(triggers, dict) or not set(triggers) <= set(_TRIGGERS)
            or any(not isinstance(v, list) or not v for v in triggers.values())
            or any(not isinstance(x, str) or not x.strip() or "\n" in x or "\r" in x
                   for v in triggers.values() for x in v)):
        raise write.Refused(f"triggers must map {list(_TRIGGERS)} to lists of single-line strings")
    etype = "contradicts" if cls == "contradicts" else c.get("edge_type", "related_to")
    if cls == "contradicts" and c.get("edge_type", etype) != etype:
        raise write.Refused("a contradicts candidate's edge is always contradicts")
    folder, vis_env = scope_folder(store, cwd, scope, env)
    if exp.env not in (None, vis_env):
        raise write.Refused("the experience is not visible from this scope")
    if target and target.env not in (None, vis_env):
        raise write.Refused(f"relates_to '{target.ref}' is outside the candidate's environment and _global")
    texts = {k: c.get(k, "") for k in ("slug", "description", "body", "why", "how")}
    texts.update({f"triggers.{k}[{i}]": x for k, v in triggers.items() for i, x in enumerate(v)})
    _check_text(store, texts, scope, vis_env, slug)
    path = folder / f"{slug}.md"
    if os.path.lexists(path):
        raise write.Refused(f"memory '{slug}' already exists in this scope; classify it as equivalent")
    for m in store.memories():
        if m.slug == slug and m.path != path and (scope == "global" or m.env in (None, vis_env)):
            raise write.Refused(f"memory '{slug}' already exists at {m.path.relative_to(store.root)}")
    for rel in (f"{slug}.md", "MEMORY.md", "edges/x.md", "revisions/x.md"):
        write.target(store, folder, rel)

    strong = item.kind == "supports" and mtype != "hypothesis" and cls != "insufficient"
    today = date.today().isoformat()
    meta = {"type": "feedback", "memory_type": mtype, "state": "active" if strong else "candidate", "author": agent,
            "created": today, "updated": today}
    head = {"name": slug, "description": c["description"], "metadata": meta,
            **({"triggers": triggers} if triggers else {}), "evidence": [item.to_dict()]}
    text = c["body"].strip() + "\n"
    if c.get("why", "").strip() and c.get("how", "").strip():
        text = f"{c['body'].strip()}\n\n**Why:** {c['why'].strip()}\n**How to apply:** {c['how'].strip()}\n"
    made = write.create_new(path, dump(head) + "\n\n" + text)
    if made is None:
        raise write.Refused(f"memory '{slug}' already exists in this scope; classify it as equivalent")
    ref = path.relative_to(store.root).with_suffix("").as_posix()
    undo, new_edges = [(folder, path.name, made)], []
    try:
        links = []
        if episode and mtype != "episodic" and store.node(episode).env in (None, vis_env):
            links.append((episode, "derived_from"))
        if target:
            links.append((target.ref, etype))
        for dst, kind in links:
            edge, _ = edges.create(store, ref, dst, kind, evidence=[item], by=agent)
            undo.append((folder, edge.path.relative_to(folder).as_posix(), os.lstat(edge.path)))
            new_edges.append(edge.ref)
        if cls == "contradicts":
            _contest(store, store.node(ref), target, agent)
        write.index_line(folder, f"- [{slug}]({slug}.md) — {c['description']}")
    except Exception:
        # Revisions of the new memory and edges can only be this run's. ponytail: a contested state already set on
        # relates_to is kept (its own revision records it)
        undo += [(folder, r.path.relative_to(folder).as_posix(), os.lstat(r.path)) for r in store.revisions()
                 if r.target in (ref, *new_edges) and r.path.parent == folder / "revisions"]
        for f, rel, st in reversed(undo):
            write.discard(store, f, rel, st)
        raise
    result.created.append(ref)
    result.edges += new_edges
    return ref


def _contest(store: Store, mine: Memory, other: Memory, agent: str) -> None:
    """Mark the side with the lower confidence (a tie: mine) contested, unless it is already past active."""
    weaker = other if evidence.confidence(store, other.evidence) < evidence.confidence(store, mine.evidence) else mine
    if weaker.state not in ("candidate", "active"):
        return
    meta = weaker.data.get("metadata")
    old = meta.get("state", "") if isinstance(meta, dict) else ""
    old = old if isinstance(old, str) else ""
    folder = weaker.path.parent
    path = write.target(store, folder, weaker.path.name)
    if not write.set_fields(path, {"state": "contested"}, expect={"state": old}):
        raise write.Refused(f"'{weaker.ref}' cannot be edited safely")
    other_ref = other.ref if weaker is mine else mine.ref
    revision.record(store, folder, weaker.ref, "state_change", {"state": old}, {"state": "contested"},
                    f"contradicted by '{other_ref}' with higher confidence", agent)
