"""Confidence and priority, computed on read from evidence; nothing here is ever stored or written.

Pure functions over `Store` reads. Independence (spec §4): evidence from one (agent, device, calendar day) counts once,
so repeating yourself never raises confidence.
"""

import math
from datetime import date, datetime, timedelta, timezone

from lrdmem import config
from lrdmem.model import Evidence
from lrdmem.store import Experience, Store

PROMOTE_SUPPORTS = 2  # independent supports needed to promote candidate -> active
REPEAT_PROCEDURE = 3  # independent experiences that queue a repeated procedure
_OBSERVED = 0.25  # observed_in counts a quarter as much as supports
_RECENT = timedelta(days=30)
_DEFAULT_HALF_LIFE = 90.0


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _experience(store: Store, ref: str) -> Experience | None:
    node = store.node(ref)
    return node if isinstance(node, Experience) else None


def _when(exp: Experience) -> datetime | None:
    """The experience date as an aware UTC datetime; date-only (legacy) means midnight UTC; None when unparseable."""
    raw = exp.date
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time(), timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def independence_key(store: Store, ev: Evidence) -> tuple:
    """`(agent, device, YYYY-MM-DD)` of the referenced experience; any other or unresolvable ref gives `(ref,)`.

    Agent and device are stripped and lowercased; the date is the UTC calendar day of the experience.
    """
    exp = _experience(store, ev.ref)
    if exp is None:
        return (ev.ref,)
    when = _when(exp)
    day = when.date().isoformat() if when else exp.date[:10]
    return (exp.agent.strip().lower(), exp.device.strip().lower(), day)


def confidence(store: Store, items: list[Evidence]) -> float:
    """Confidence in [0, 1] from evidence: `clamp(0, 1, 1 - exp(-(S - R)))`.

    Only evidence that resolves to an experience counts; any other ref (unresolvable, memory, edge, URL) adds
    nothing, so made-up refs cannot inflate it. Per independence key (agent, device, day), positive evidence is the
    max of the supports weight and 0.25 x the observed_in weight (never their sum), and refutes are the max refutes
    weight; S and R sum those over keys. Refutes can push S - R negative, which clamps to 0; no evidence gives 0.
    """
    pos: dict[tuple, float] = {}
    neg: dict[tuple, float] = {}
    for ev in items:
        if _experience(store, ev.ref) is None:
            continue
        k = independence_key(store, ev)
        if ev.kind == "refutes":
            neg[k] = max(neg.get(k, 0.0), ev.weight)
        elif ev.kind in ("supports", "observed_in"):
            pos[k] = max(pos.get(k, 0.0), ev.weight * (_OBSERVED if ev.kind == "observed_in" else 1.0))
    return _clamp(1 - math.exp(-(sum(pos.values()) - sum(neg.values()))))


def has_outcome(store: Store, items: list[Evidence]) -> bool:
    """True when a `supports` item references an experience of kind `outcome` or `check`."""
    for ev in items:
        exp = _experience(store, ev.ref) if ev.kind == "supports" else None
        if exp and exp.kind in ("outcome", "check"):
            return True
    return False


def _irrelevant(exp: Experience) -> bool:
    """A feedback experience whose `verdict` frontmatter field is irrelevant."""
    verdict = exp.data.get("verdict")
    return exp.kind == "feedback" and isinstance(verdict, str) and verdict.strip().lower() == "irrelevant"


def priority(store: Store, node, now: datetime | None = None, cfg: dict | None = None) -> float:
    """Priority in [0, 1] of a memory, computed on read.

    Base 1.0; episodic/prospective memories decay by `0.5 ** (age / half_life_days)` (config, default 90; age from
    `metadata.created`, none when absent); superseded/obsolete x 0.1; each irrelevant feedback in the last 30 days
    x 0.8; supporting evidence from the last 30 days x 1.2 (once). Irrelevant feedback is read from the node's
    evidence list: feedback experiences linked as `observed_in` with weight 0 (the Task 9 write contract) whose
    `verdict: irrelevant` frontmatter field says so (the body is never read).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    p = 1.0
    if getattr(node, "memory_type", "") in ("episodic", "prospective"):
        half = (cfg if cfg is not None else config.load()).get("half_life_days", _DEFAULT_HALF_LIFE)
        if isinstance(half, bool) or not isinstance(half, (int, float)) or not half > 0:
            half = _DEFAULT_HALF_LIFE
        try:
            created = datetime.fromisoformat(getattr(node, "created", "")[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            created = None
        if created and now > created:
            p *= 0.5 ** ((now - created).total_seconds() / 86400 / half)
    if getattr(node, "state", "") in ("superseded", "obsolete"):
        p *= 0.1
    supported = False
    seen = set()
    for ev in getattr(node, "evidence", []):
        exp = _experience(store, ev.ref)
        when = _when(exp) if exp else None
        if when is None or ev.ref in seen or not 0 <= (now - when).total_seconds() <= _RECENT.total_seconds():
            continue
        seen.add(ev.ref)
        if ev.kind == "observed_in" and ev.weight == 0 and _irrelevant(exp):
            p *= 0.8
        elif ev.kind == "supports" and ev.weight > 0:
            supported = True
    return _clamp(p * (1.2 if supported else 1.0))
