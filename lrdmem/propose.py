"""Validate and apply the memory candidates an agent extracted from one experience (spec §5.2, §5.4).

The agent proposes; this module decides what is safe to persist. Each candidate is validated in full before anything
is written for it, and a refused candidate leaves no file behind. `validate` runs the same checks without writing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem import edges, evidence, revision, write
from lrdmem.frontmatter import dump
from lrdmem.learn import _RESERVED, _SCOPES, _SLUG, LearnRefused, _check_text, scope_folder
from lrdmem.model import EDGE_TYPES, MEMORY_TYPES, Evidence, today_utc
from lrdmem.search import visible
from lrdmem.store import AmbiguousRepo, Experience, Memory, Store, StoreFormatError, resolve

CLASSIFICATIONS = ("new", "equivalent", "complementary", "contradicts", "insufficient")
_KEYS = {"slug", "memory_type", "description", "body", "why", "how", "scope", "triggers", "classification",
         "relates_to", "edge_type"}
_TEXT = ("body", "why", "how")  # the only multi-line fields
_CAPS = {"description": 300, "body": 20_000, "why": 2_000, "how": 2_000}
_TRIGGER_CAP, _TRIGGERS_MAX = 200, 50
_TRIGGERS = ("paths", "commands", "repos")
_LINKS = ("related_to", "generalizes")  # edge types a non-contradicting candidate may propose
_STRONG = ("correction", "review")  # human corrections: memories start active, the experience supports them
_RESULT = "outcome"  # the result of applying something: supports, but only consolidation makes it active
_RELATES = ("equivalent", "complementary", "contradicts")
_ERRORS = (write.Refused, LearnRefused, StoreFormatError, OSError)


@dataclass
class ProposeResult:
    """Refs of created memories, memories that gained evidence, new edges and existing edges the candidates
    matched (`edges_reused`), plus (candidate index, reason) refusals."""

    created: list[str] = field(default_factory=list)
    evidence_added: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    refused: list[tuple[int, str]] = field(default_factory=list)
    edges_reused: list[str] = field(default_factory=list)


def _oneline(value: str) -> bool:
    """No line break of any kind (splitlines also breaks on U+2028/2029, \\x85, \\x0b, \\x0c, \\x1c-\\x1e)."""
    return len(value.splitlines()) <= 1 and "\n" not in value and "\r" not in value


def _episodic(c) -> bool:
    return isinstance(c, dict) and c.get("memory_type") == "episodic" and c.get("classification") != "equivalent"


def _payload(store: Store, payload, agent, exp: Experience | None) -> tuple[Experience, list]:
    if not isinstance(payload, dict) or not set(payload) <= {"experience", "candidates"}:
        raise write.Refused("payload must be a JSON object with experience and candidates")
    if exp is None:
        ref = payload.get("experience")
        exp = store.node(ref) if isinstance(ref, str) else None
    if not isinstance(exp, Experience) or not exp.kind:
        raise write.Refused("experience must be the ref of a recorded experience")
    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        raise write.Refused("candidates must be a non-empty list")
    if not isinstance(agent, str) or not agent.strip() or not _oneline(agent):
        raise write.Refused("agent is required and must be single-line")
    write.guard(agent)
    return exp, cands


def _here(store: Store, cwd: Path, env: str | None) -> tuple[str | None, str | None]:
    """(env, repo) of cwd; (None, None), which sees only _global, when it is not opted in or ambiguous."""
    try:
        return resolve(store, cwd, env) or (None, None)
    except AmbiguousRepo:
        return None, None


def validate(store: Store, cwd: Path, payload: dict, *, agent: str, env: str | None = None,
             experience: Experience | None = None) -> list[tuple[int, str]]:
    """The (candidate index, reason) refusals `apply` would give, without writing anything.

    `experience` stands in for `payload["experience"]` (which may then be omitted), so a caller can check candidates
    before recording the experience. Candidates are checked against the store as it is: two candidates of one payload
    with the same slug both pass here, and `apply` refuses the second. Refused for a malformed payload.
    """
    exp, cands = _payload(store, payload, agent, experience)
    here, out = _here(store, cwd, env), []
    for i, c in enumerate(cands):
        try:
            _plan(store, cwd, here, exp, c, env)
        except _ERRORS as err:
            out.append((i, str(err)))
    return out


def apply(store: Store, cwd: Path, payload: dict, *, agent: str, device: str = "",
          env: str | None = None) -> ProposeResult:
    """Apply `{"experience": ref, "candidates": [...]}` and return what was written and what was refused.

    The experience and every `relates_to` must be visible from cwd's repo (`search.visible`). State: `active` when
    the experience is a `correction` or `review` and the candidate is not a hypothesis nor `insufficient`, else
    `candidate`. The experience is `supports` (weight 1) evidence when it is a correction, a review or an `outcome`
    (the result of applying something), `observed_in` otherwise: seeing the same thing again never promotes. `equivalent`
    adds that evidence to `relates_to` (+ a merge revision) and creates nothing; `complementary`/`contradicts` create
    the memory and a proposed edge (`edge_type` related_to or generalizes, default related_to / contradicts). Only an
    active new memory contests: the side with the lower confidence (`evidence.memory_evidence`; a tie: the new one)
    becomes `contested` (+ revision); a candidate's contradiction is left to consolidation. Each new memory gets a
    `create` revision; every new non-episodic one a proposed `derived_from` edge to the payload's first episodic
    memory. A refused candidate is reported with its index and reason; the others proceed. Refused (write.Refused)
    for a malformed payload or an experience ref that does not resolve. `device` is accepted for symmetry with the
    other writers; the experience already records it.
    """
    exp, cands = _payload(store, payload, agent, None)
    here = _here(store, cwd, env)
    item = Evidence(exp.ref, "supports" if exp.kind in _STRONG or exp.kind == _RESULT else "observed_in", 1.0)
    result, episode = ProposeResult(), None
    for i in sorted(range(len(cands)), key=lambda i: not _episodic(cands[i])):  # episodes first: derived_from targets
        try:
            plan = _plan(store, cwd, here, exp, cands[i], env)
            if plan["cls"] == "equivalent":
                _equivalent(store, exp, item, plan["target"], agent, result)
                continue
            made = _create(store, exp, item, plan, agent, episode, result)
        except _ERRORS as err:
            result.refused.append((i, str(err)))
            continue
        if episode is None and _episodic(cands[i]):
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
    if any(not _oneline(v) for k, v in c.items() if k not in _TEXT and isinstance(v, str)):
        raise write.Refused("every field but body, why and how must be single-line")
    for k, cap in _CAPS.items():
        if len(c.get(k, "")) > cap:
            raise write.Refused(f"{k} is too long (max {cap} characters)")
    if c.get("classification") not in CLASSIFICATIONS:
        raise write.Refused(f"classification must be one of {list(CLASSIFICATIONS)}")
    if "memory_type" in c and c["memory_type"] not in MEMORY_TYPES:
        raise write.Refused(f"memory_type must be one of {list(MEMORY_TYPES)}")
    if "edge_type" in c and c["edge_type"] not in EDGE_TYPES:
        raise write.Refused(f"edge_type must be one of {list(EDGE_TYPES)}")
    write.guard("\n".join(v for v in c.values() if isinstance(v, str)))
    return c["classification"]


def _relates(store: Store, here: tuple, c: dict, cls: str) -> Memory | None:
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
    if not visible(target, *here):
        raise write.Refused(f"relates_to '{ref}' is not visible from this repository")
    return target


def _plan(store: Store, cwd: Path, here: tuple, exp: Experience, c, env: str | None) -> dict:
    """Every check of one candidate, no writes; what `_equivalent`/`_create` need."""
    cls = _check(c)
    if not visible(exp, *here):
        raise write.Refused(f"the experience '{exp.ref}' is not visible from this repository")
    target = _relates(store, here, c, cls)
    if cls == "equivalent":
        if c.get("memory_type") == "hypothesis" and target.memory_type != "hypothesis":
            raise write.Refused(f"a hypothesis cannot merge into the {target.memory_type} memory '{target.ref}'")
        if exp.env not in (None, target.env):
            raise write.Refused(f"the experience is not visible from '{target.ref}'")
        folder = target.path.parent
        write.target(store, folder, target.path.name)
        write.target(store, folder, "revisions/x.md")
        return {"cls": cls, "target": target}

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
            or any(not isinstance(x, str) or not x.strip() or not _oneline(x) for v in triggers.values() for x in v)):
        raise write.Refused(f"triggers must map {list(_TRIGGERS)} to lists of single-line strings")
    if any(len(x) > _TRIGGER_CAP for v in triggers.values() for x in v):
        raise write.Refused(f"a trigger is too long (max {_TRIGGER_CAP} characters)")
    if sum(len(v) for v in triggers.values()) > _TRIGGERS_MAX:
        raise write.Refused(f"too many triggers (max {_TRIGGERS_MAX})")
    if cls == "contradicts":
        etype = "contradicts"
        if c.get("edge_type", etype) != etype:
            raise write.Refused("a contradicts candidate's edge is always contradicts")
    else:
        etype = c.get("edge_type", "related_to")
        if etype not in _LINKS:
            raise write.Refused(f"edge_type must be one of {list(_LINKS)} (contradicts: classify as contradicts)")
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
    return {"cls": cls, "target": target, "c": c, "mtype": mtype, "slug": slug, "folder": folder,
            "vis_env": vis_env, "path": path, "triggers": triggers, "etype": etype}


def _equivalent(store: Store, exp: Experience, item: Evidence, target: Memory, agent: str,
                result: ProposeResult) -> None:
    folder = target.path.parent
    if write.add_evidence(target.path, item):  # False: already there (or not an editable header), nothing to record
        # ponytail: a revision failing after the evidence line leaves the evidence unrecorded; both are re-runnable
        revision.record(store, folder, target.ref, "merge", {"evidence": ""}, {"evidence": exp.ref},
                        f"equivalent evidence from {exp.ref}", agent)
        result.evidence_added.append(target.ref)


def _revisions(folder: Path) -> set[str]:
    try:
        return set(os.listdir(folder / "revisions"))
    except FileNotFoundError:
        return set()


def _create(store: Store, exp: Experience, item: Evidence, plan: dict, agent: str, episode: str | None,
            result: ProposeResult) -> str:
    c, cls, mtype, slug, folder, path = (plan[k] for k in ("c", "cls", "mtype", "slug", "folder", "path"))
    target = plan["target"]
    strong = exp.kind in _STRONG and mtype != "hypothesis" and cls != "insufficient"
    state = "active" if strong else "candidate"
    today = today_utc()
    meta = {"type": "feedback", "memory_type": mtype, "state": state, "author": agent, "created": today,
            "updated": today}
    head = {"name": slug, "description": c["description"], "metadata": meta,
            **({"triggers": plan["triggers"]} if plan["triggers"] else {}), "evidence": [item.to_dict()]}
    text = c["body"].strip() + "\n"
    if c.get("why", "").strip() and c.get("how", "").strip():
        text = f"{c['body'].strip()}\n\n**Why:** {c['why'].strip()}\n**How to apply:** {c['how'].strip()}\n"
    known = _revisions(folder)
    made = write.create_new(path, dump(head) + "\n\n" + text)
    if made is None:
        raise write.Refused(f"memory '{slug}' already exists in this scope; classify it as equivalent")
    ref = path.relative_to(store.root).with_suffix("").as_posix()
    undo, new_edges, reused = [(path.name, made)], [], []
    try:
        revision.record(store, folder, ref, "create", {}, {"memory_type": mtype, "state": state},
                        f"proposed from {exp.ref}", agent)
        links = []
        if episode and mtype != "episodic" and store.node(episode).env in (None, plan["vis_env"]):
            links.append((episode, "derived_from"))
        if target:
            links.append((target.ref, plan["etype"]))
        for dst, kind in links:
            edge, created = edges.create(store, ref, dst, kind, evidence=[item], by=agent)
            if created:
                undo.append((edge.path.relative_to(folder).as_posix(), os.lstat(edge.path)))
                new_edges.append(edge.ref)
            else:
                reused.append(edge.ref)
        if cls == "contradicts" and strong:
            _contest(store, store.node(ref), target, agent)
        write.index_line(folder, f"- [{slug}]({slug}.md) — {c['description']}")
    except Exception:
        # Undo only this call's files: the memory, edges it created, and revisions of those that appeared meanwhile.
        # ponytail: a contested state already set on relates_to is kept (its own revision records it)
        fresh = _revisions(folder) - known
        undo += [(r.path.relative_to(folder).as_posix(), os.lstat(r.path)) for r in store.revisions()
                 if r.path.parent == folder / "revisions" and r.path.name in fresh and r.target in (ref, *new_edges)]
        for rel, st in reversed(undo):
            write.discard(store, folder, rel, st)
        raise
    result.created.append(ref)
    result.edges += new_edges
    result.edges_reused += reused
    return ref


def _contest(store: Store, mine: Memory, other: Memory, agent: str) -> None:
    """Mark the side with the lower confidence (a tie: mine) contested, unless it is already past active."""
    conf = lambda m: evidence.confidence(store, evidence.memory_evidence(store, m))  # noqa: E731
    weaker = other if conf(other) < conf(mine) else mine
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
