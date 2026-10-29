import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lrdmem import evidence
from lrdmem.model import Evidence
from lrdmem.store import Store
from tests.fixtures import make_store

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
CFG = {"half_life_days": 90}
S = "acme/web/experiences/"


def exp(name, kind="discovery", agent="claude", device="mac", date="2026-10-20T10:00:00Z", extra="", body="x"):
    head = f"id: {name}\ndate: {date}\nkind: {kind}\nagent: {agent}\ndevice: {device}\n{extra}"
    return f"{S}{name}.md", f"---\n{head}---\n\n{body}\n"


def mem(name, **meta):
    lines = "".join(f"  {k}: {v}\n" for k, v in meta.items())
    return f"acme/web/{name}.md", f"---\nname: {name}\ndescription: d\nmetadata:\n{lines}---\n\nbody\n"


class EvidenceTest(unittest.TestCase):
    def build(self, *files):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        base = {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": ""}
        self.store = Store(make_store(Path(t.name), {**base, **dict(files)}))
        return self.store

    def ev(self, name, kind="supports", w=1.0):
        return Evidence(f"{S}{name}", kind, w)

    def test_no_evidence_zero(self):
        self.assertEqual(evidence.confidence(self.build(), []), 0.0)

    def test_monotonic_in_independent_supports(self):
        st = self.build(*[exp(f"e{i}", agent=f"a{i}") for i in range(4)])
        vals = [evidence.confidence(st, [self.ev(f"e{i}") for i in range(n)]) for n in range(5)]
        self.assertTrue(all(a < b for a, b in zip(vals, vals[1:])), vals)
        self.assertLessEqual(vals[-1], 1.0)

    def test_same_agent_device_day_counted_once(self):
        st = self.build(exp("a"), exp("b", date="2026-10-20T18:00:00Z"), exp("c", date="2026-10-21T10:00:00Z"))
        one = evidence.confidence(st, [self.ev("a")])
        self.assertEqual(evidence.confidence(st, [self.ev("a"), self.ev("b")]), one)
        self.assertGreater(evidence.confidence(st, [self.ev("a"), self.ev("c")]), one)

    def test_legacy_contexts_same_day_collapse(self):
        ctx = lambda n: (f"acme/web/contexts/{n}.md", f"---\ndate: 2026-10-01\n---\n\nb\n")  # noqa: E731
        st = self.build(ctx("a"), ctx("b"))
        evs = [Evidence(f"acme/web/contexts/{n}", "supports", 1.0) for n in "ab"]
        self.assertEqual(evidence.independence_key(st, evs[0]), ("", "", "2026-10-01"))
        self.assertEqual(evidence.confidence(st, evs), evidence.confidence(st, evs[:1]))

    def test_unresolvable_ref_key_and_duplicates_once(self):
        st = self.build()
        ev = Evidence("nope/x/experiences/y", "supports", 0.5)
        self.assertEqual(evidence.independence_key(st, ev), ("nope/x/experiences/y",))
        self.assertEqual(evidence.confidence(st, [ev, ev]), evidence.confidence(st, [ev]))

    def test_max_weight_kept_per_key(self):
        st = self.build(exp("a"), exp("b", date="2026-10-20T11:00:00Z"))
        got = evidence.confidence(st, [self.ev("a", w=0.2), self.ev("b", w=0.9)])
        self.assertAlmostEqual(got, 1 - 2.718281828459045 ** -0.9)

    def test_refutes_lower_and_floor_at_zero(self):
        st = self.build(exp("a"), exp("b", agent="b"), exp("c", agent="c"))
        base = evidence.confidence(st, [self.ev("a")])
        self.assertLess(evidence.confidence(st, [self.ev("a"), self.ev("b", "refutes", 0.5)]), base)
        self.assertEqual(evidence.confidence(st, [self.ev("a", w=0.2), self.ev("b", "refutes"), self.ev("c", "refutes")]), 0.0)

    def test_observed_in_small_positive(self):
        st = self.build(exp("a"))
        o, s = (evidence.confidence(st, [self.ev("a", k)]) for k in ("observed_in", "supports"))
        self.assertGreater(o, 0)
        self.assertLess(o, s / 2)

    def test_has_outcome(self):
        st = self.build(exp("o", kind="outcome"), exp("k", kind="check"), exp("d"))
        self.assertTrue(evidence.has_outcome(st, [self.ev("o")]))
        self.assertTrue(evidence.has_outcome(st, [self.ev("k")]))
        self.assertFalse(evidence.has_outcome(st, [self.ev("d")]))
        self.assertFalse(evidence.has_outcome(st, [self.ev("o", "observed_in")]))
        self.assertFalse(evidence.has_outcome(st, [self.ev("o", "refutes")]))

    def prio(self, st, name, **kw):
        return evidence.priority(st, st.node(f"acme/web/{name}"), NOW, CFG, **kw)

    def test_episodic_halves_at_half_life(self):
        created = (NOW - timedelta(days=90)).strftime("%Y-%m-%d")
        st = self.build(mem("e", memory_type="episodic", created=created),
                        mem("s", memory_type="semantic", created=created),
                        mem("p", memory_type="prospective", created=created))
        self.assertAlmostEqual(self.prio(st, "e"), 0.5, places=2)
        self.assertAlmostEqual(self.prio(st, "p"), 0.5, places=2)
        self.assertEqual(self.prio(st, "s"), 1.0)

    def test_half_life_from_config(self):
        created = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
        st = self.build(mem("e", memory_type="episodic", created=created))
        self.assertAlmostEqual(evidence.priority(st, st.node("acme/web/e"), NOW, {"half_life_days": 30}), 0.5, delta=0.01)

    def test_superseded_obsolete_tenth(self):
        st = self.build(mem("a", state="superseded"), mem("b", state="obsolete"), mem("c"))
        self.assertAlmostEqual(self.prio(st, "a"), 0.1)
        self.assertAlmostEqual(self.prio(st, "b"), 0.1)
        self.assertEqual(self.prio(st, "c"), 1.0)

    def with_ev(self, name, items, **meta):
        ev = "evidence: [" + ", ".join(f"{{ref: {r}, kind: {k}, weight: {w}}}" for r, k, w in items) + "]\n"
        path, text = mem(name, **meta)
        return path, text.replace("metadata:", ev + "metadata:")

    def test_irrelevant_feedback_recent_only_and_support_boost(self):
        old = "2026-08-01T00:00:00Z"
        st = self.build(
            exp("f1", kind="feedback", extra="verdict: irrelevant\n"),
            exp("f2", kind="feedback", body="This is irrelevant here", date="2026-10-25T00:00:00Z", agent="b"),
            exp("f3", kind="feedback", extra="verdict: useful\n", body="not relevant? irrelevant", agent="c"),
            exp("f4", kind="feedback", extra="verdict: irrelevant\n", date=old, agent="d"),
            exp("sup", date="2026-10-28T00:00:00Z"),
            self.with_ev("m", [(S + "f1", "observed_in", 0), (S + "f2", "observed_in", 0), (S + "f3", "observed_in", 0),
                               (S + "f4", "observed_in", 0)], state="candidate"),
            self.with_ev("n", [(S + "sup", "supports", 1)], state="candidate"),
            self.with_ev("o", [(S + "sup", "supports", 1)], state="obsolete"))
        self.assertAlmostEqual(self.prio(st, "m"), 0.64)  # f1, f2 only
        self.assertEqual(self.prio(st, "n"), 1.0)  # 1.2 clamped
        self.assertAlmostEqual(self.prio(st, "o"), 0.12)  # boost applies under the cap

    def test_lazy_config_default(self):
        st = self.build(mem("s"))
        self.assertEqual(evidence.priority(st, st.node("acme/web/s"), NOW), 1.0)


if __name__ == "__main__":
    unittest.main()
