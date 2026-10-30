"""Conventional Commits messages for every commit lrdmem makes (spec §7)."""

from pathlib import Path, PurePosixPath

from lrdmem.frontmatter import split

_SUBJECT_MAX = 72
_BODY_MAX = 20
_ENTITY_DIRS = {"experiences", "edges", "revisions", "queue", "contexts"}


def _revision(root: Path, rel: str) -> tuple[str, dict]:
    try:
        data, _ = split((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", {}
    after = data.get("after") if data else None
    return (str(data.get("action", "")) if data else ""), (after if isinstance(after, dict) else {})


def _clip(subject: str) -> str:
    return subject if len(subject) <= _SUBJECT_MAX else subject[:_SUBJECT_MAX - 1].rstrip() + "…"


def build(changed: list[tuple[str, str]], device: str, env_label: str, root: Path) -> str:
    """Commit message for the staged (status, path) pairs of one repo, paths relative to `root`.

    A revert revision → `revert(memory): <id>`; consolidation revisions (merge, state changes, supersede,
    invalidate) → `refactor(memory): consolidate — merge N, promote N, contest N`; new memories or experiences →
    `feat(memory): learn N memories, M experiences`; anything else → `chore(memory): sync <env> from <device>`.
    The body lists the changed paths (at most 20 lines). The summary is lowercase and the subject ≤ 72 chars.
    """
    added_revs = [(p, *_revision(root, p)) for s, p in changed
                  if s[:1] in "AM" and PurePosixPath(p).parent.name == "revisions"]
    reverts = [PurePosixPath(p).stem for p, action, _ in added_revs if action == "revert"]
    merge = sum(1 for _, a, _ in added_revs if a == "merge")
    promote = sum(1 for _, a, after in added_revs if a == "state_change" and after.get("state") == "active")
    contest = sum(1 for _, a, after in added_revs if a == "state_change" and after.get("state") == "contested")
    consolidating = merge or promote or contest or any(a in ("supersede", "invalidate") for _, a, _ in added_revs)
    new = [PurePosixPath(p) for s, p in changed if s[:1] == "A" and p.endswith(".md")]
    memories = [p for p in new if p.parent.name not in _ENTITY_DIRS and p.name not in ("MEMORY.md", "README.md")]
    experiences = [p for p in new if p.parent.name == "experiences"]
    edges = [p for p in new if p.parent.name == "edges"]
    if reverts:
        subject = f"revert(memory): {reverts[0]}"
    elif consolidating:
        subject = f"refactor(memory): consolidate — merge {merge}, promote {promote}, contest {contest}"
    elif memories or experiences:
        subject = f"feat(memory): learn {len(memories)} memories, {len(experiences)} experiences"
    else:
        subject = f"chore(memory): sync {env_label} from {device}".lower()
    lines = [f"{s} {p}" for s, p in changed[:_BODY_MAX]]
    if len(changed) > _BODY_MAX:
        lines.append(f"… and {len(changed) - _BODY_MAX} more")
    if edges:
        lines.insert(0, f"{len(edges)} edge{'s' if len(edges) != 1 else ''} added")
    return _clip(" ".join(subject.split())) + "\n\n" + "\n".join(lines) + "\n"
