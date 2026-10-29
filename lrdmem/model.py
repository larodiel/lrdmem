"""Constants and small value types of the v2 associative memory model."""

import math
from dataclasses import dataclass

MEMORY_TYPES = ("episodic", "semantic", "procedural", "hypothesis", "prospective", "metacognitive")
STATES = ("candidate", "active", "contested", "superseded", "obsolete")
EDGE_TYPES = ("related_to", "derived_from", "occurred_in", "supports", "contradicts",
              "caused_by", "resolved_by", "generalizes", "supersedes")
EDGE_STATES = ("proposed", "validated", "invalidated", "superseded")
EXPERIENCE_KINDS = ("error", "decision", "discovery", "outcome", "execution",
                    "correction", "review", "feedback", "check")
SOURCES = ("chat", "pr", "run", "check")
EVIDENCE_KINDS = ("supports", "refutes", "observed_in")
REVISION_ACTIONS = ("create", "merge", "state_change", "confidence_change",
                    "content_change", "invalidate", "supersede", "revert")


def v01_memory_type(kind: str) -> str:
    """Memory type of a v0.1 `metadata.kind`: obligation/pattern are procedural, anything else semantic."""
    return "procedural" if kind in ("obligation", "pattern") else "semantic"


@dataclass(frozen=True)
class Evidence:
    """One evidence item of a memory: a reference, how it bears on the memory, and its weight."""

    ref: str
    kind: str
    weight: float

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        """Build from a parsed inline map.

        ValueError on an unknown kind, an empty ref, or a weight that is not a float in [0, 1].
        """
        if d.get("kind") not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind: {d.get('kind')!r}")
        weight = float(d.get("weight", ""))
        if not d.get("ref"):
            raise ValueError("evidence ref is empty")
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(f"invalid evidence weight: {d.get('weight')!r}")
        return cls(str(d.get("ref", "")), d["kind"], weight)

    def to_dict(self) -> dict:
        """Inline-map form for frontmatter (string values)."""
        return {"ref": self.ref, "kind": self.kind, "weight": str(self.weight)}
