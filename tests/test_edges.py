import tempfile
import unittest
from pathlib import Path

from lrdmem import edges, write
from lrdmem.evidence import confidence
from lrdmem.graph import Graph
from lrdmem.lint import lint
from lrdmem.model import Evidence
from lrdmem.store import Store
from tests.fixtures import make_store

MEM = "---\nname: {n}\ndescription: d\nmetadata:\n  type: feedback\n---\nbody {b}\n"
EXP = "---\nid: {i}\ndate: {d}\nkind: error\nagent: {a}\n---\nwhat happened\n"
A, B, G, H, E = "work/app/a", "work/app/b", "_global/g", "home/app/h", "work/app/experiences/"


class EdgesTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        files = {"work/app/a.md": MEM.format(n="a", b="see [[b]]"), "work/app/b.md": MEM.format(n="b", b=""),
                 "_global/g.md": MEM.format(n="g", b=""), "home/app/h.md": MEM.format(n="h", b=""),
                 "_global/g2.md": MEM.format(n="g2", b="")}
        for i, (d, a) in enumerate([("2026-01-01", "claude"), ("2026-01-02", "codex"), ("2026-01-02", "codex")]):
            files[f"work/app/experiences/x{i}.md"] = EXP.format(i=f"x{i}", d=d, a=a)
        self.root = make_store(Path(t.name), files)
        self.store = Store(self.root)

    def ev(self, *ids):
        return [Evidence(E + i, "supports", 1.0) for i in ids]

    def test_create_idempotent_adds_evidence(self):
        e, made = edges.create(self.store, A, B, "supports", evidence=self.ev("x0"), by="t")
        self.assertTrue(made)
        self.assertRegex(e.path.name, r"^\d{4}-\d\d-\d\d-e[0-9a-f]{12}\.md$")
        self.assertEqual((e.path.parent, e.state), (self.root / "work/app/edges", "proposed"))
        e2, made2 = edges.create(self.store, A, B, "supports", evidence=self.ev("x1"), by="t")
        self.assertEqual((made2, e2.path, len(e2.evidence)), (False, e.path, 2))
        self.assertEqual(len(list((self.root / "work/app/edges").iterdir())), 1)
        self.assertGreater(confidence(self.store, e2.evidence), confidence(self.store, e.evidence))
        self.assertNotIn("confidence", e.path.read_text())
        revs = self.store.revisions()
        self.assertEqual([(r.action, r.after) for r in revs], [("create", {"type": "supports", "state": "proposed"})])
        self.assertEqual([m for m in lint(self.store) if "edges/" in m or "revisions/" in m], [])

    def test_scope_and_validation_refused(self):
        for args in ((A, H, "supports"), (G, B, "supports"), (A, A, "supports"), (A, B, "nope"),
                     (A, "work/app/experiences/x0", "supports"), (A, "work/app/missing", "supports")):
            with self.subTest(args), self.assertRaises(write.Refused):
                edges.create(self.store, *args, by="t")
        e, _ = edges.create(self.store, A, G, "related_to", by="t")
        self.assertEqual(e.path.parent, self.root / "work/app/edges")
        e, _ = edges.create(self.store, G, "_global/g2", "related_to", by="t")
        self.assertEqual(e.path.parent, self.root / "_global/edges")
        with self.assertRaises(write.Refused):
            edges.create(self.store, A, B, "supports", by="sk-" + "a" * 40)

    def test_refused_create_leaves_nothing(self):
        for kw in ({"by": "sk-" + "a" * 40}, {"by": "t", "weight": 2.0}, {"by": "t", "weight": float("nan")}):
            with self.subTest(kw), self.assertRaises(write.Refused):
                edges.create(self.store, A, B, "supports", **kw)
            self.assertFalse(list((self.root / "work/app").glob("edges/*")))
            self.assertFalse(list((self.root / "work/app").glob("revisions/*")))
        e, made = edges.create(self.store, A, B, "supports", by="t")
        self.assertTrue(made)
        self.assertEqual(len(self.store.revisions()), 1)

    def test_revision_failure_rolls_back_edge(self):
        from unittest import mock
        with mock.patch("lrdmem.revision.record", side_effect=write.Refused("boom")):
            with self.assertRaises(write.Refused):
                edges.create(self.store, A, B, "supports", by="t")
        self.assertFalse(list((self.root / "work/app").glob("edges/*")))

    def test_recreate_after_invalidate_is_deterministic(self):
        e, _ = edges.create(self.store, A, B, "supports", by="t")
        edges.set_state(self.store, e, "invalidated", "no", "t")
        e2, _ = edges.create(self.store, A, B, "supports", by="t")
        import hashlib
        h = hashlib.sha1(f"{A}|{B}|supports|{e.id}".encode()).hexdigest()[:12]
        self.assertTrue(e2.path.name.endswith(f"-e{h}.md"))

    def test_set_state_validation(self):
        e, _ = edges.create(self.store, A, B, "supports", by="t")
        e2, _ = edges.create(self.store, A, G, "supports", by="t")
        implicit = [x for x in Graph(self.store).typed_edges() if x.path == Path()][0]
        for edge, kw in ((implicit, {}), (e, {"superseded_by": "nope"}), (e, {"superseded_by": e.id}),
                         (e, {"superseded_by": "home/app/edges/x"})):
            with self.subTest(kw), self.assertRaises(write.Refused):
                edges.set_state(self.store, edge, "superseded", "r", "t", **kw)
        edges.set_state(self.store, e, "superseded", "r", "t", superseded_by=e2.id)
        self.assertEqual(self.store.node(e.ref).superseded_by, e2.id)

    def test_implicit_links_deduped_and_masked(self):
        e, _ = edges.create(self.store, A, B, "related_to", by="t")
        self.assertEqual([x for x in Graph(self.store).typed_edges() if x.path == Path()], [])

    def test_invalidate_keeps_file_and_revision(self):
        e, _ = edges.create(self.store, A, B, "supports", by="t")
        edges.set_state(self.store, e, "invalidated", "wrong", "t")
        self.assertTrue(e.path.exists())
        self.assertEqual(self.store.node(e.ref).state, "invalidated")
        self.assertEqual(sorted(r.action for r in self.store.revisions()), ["create", "invalidate"])
        e2, made = edges.create(self.store, A, B, "supports", by="t")  # invalidated does not block a new one
        self.assertTrue(made)
        self.assertNotEqual(e2.path, e.path)
        edges.set_state(self.store, e2, "validated", "ok", "t")
        self.assertIn("state_change", [r.action for r in self.store.revisions()])
        edges.set_state(self.store, self.store.node(e2.ref), "superseded", "new", "t", superseded_by=e.id)
        self.assertIn("supersede", [r.action for r in self.store.revisions()])
        self.assertEqual(self.store.node(e2.ref).superseded_by, e.id)

    def test_neighbours_and_implicit_links(self):
        e, _ = edges.create(self.store, A, G, "related_to", by="t")
        e2, _ = edges.create(self.store, B, G, "supports", by="t")
        edges.set_state(self.store, e2, "invalidated", "no", "t")
        g = Graph(self.store)
        self.assertEqual([(x.ref, o) for x, o in g.neighbours(G)], [(e.ref, A)])
        self.assertEqual(len(g.neighbours(G, states=("invalidated",))), 1)
        implicit = [x for x in g.typed_edges() if x.path == Path()]
        self.assertEqual([(x.source, x.target, x.type, x.state) for x in implicit],
                         [(A, B, "related_to", "validated")])
        self.assertEqual({o for _, o in g.neighbours(A)}, {B, G})
        self.assertEqual(len(g.typed_edges()), 3)


if __name__ == "__main__":
    unittest.main()
