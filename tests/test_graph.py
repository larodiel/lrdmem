import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from lrdmem.cli import main
from lrdmem.graph import AmbiguousSlug, Graph
from lrdmem.store import Context, Store
from tests.fixtures import make_store


def _mem(name, body="body", extra=""):
    return f"---\nname: {name}\ndescription: about {name}\nmetadata:\n  type: feedback\n{extra}---\n{body}\n"


def _ctx(i, date, mems):
    ms = "[" + ", ".join(mems) + "]"
    return f"---\nid: {i}\ndate: {date}\nsource: chat\nagent: claude\nmemories: {ms}\n---\nfirst line of {i}\nmore\n"


def _files():
    f = {
        "work/app/phpcs.md": _mem("phpcs", "Run phpcs.\nSecond line. See [[style]]."),
        "work/app/other.md": _mem("other"),
        "work/app/lonely.md": _mem("lonely"),
        "_global/style.md": _mem("style"),
        "home/app/phpcs.md": _mem("phpcs"),
    }
    for i, d in enumerate(["2026-03-01", "2026-01-01", "2026-02-01"]):
        f[f"work/app/contexts/{d}-c{i}.md"] = _ctx(f"c{i}", d, ["phpcs", "other"] if i == 0 else ["phpcs"])
    return f


class GraphTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = make_store(Path(t.name), _files())
        self.g = Graph(Store(self.root))

    def test_memories_accessor(self):
        self.assertEqual(sorted((m.env or "", m.slug) for m in self.g.memories()),
                         [("", "style"), ("home", "phpcs"), ("work", "lonely"), ("work", "other"), ("work", "phpcs")])
        import inspect
        from lrdmem import export
        self.assertNotIn("_nodes", inspect.getsource(export))

    def test_strength_and_hot(self):
        self.assertEqual(self.g.strength("phpcs", env="work"), 3)
        self.assertEqual(self.g.strength("phpcs", env="home"), 0)
        self.assertEqual(self.g.strength("other", env="work"), 1)
        self.assertEqual([(m.slug, n) for m, n in self.g.hot()], [("phpcs", 3)])
        self.assertEqual(self.g.hot(4), [])

    def test_hot_ranks_by_confidence_times_independent_support(self):
        ev = ("evidence: [{ref: work/app/experiences/e1, kind: supports, weight: 0.2}, "
              "{ref: work/app/experiences/e2, kind: supports, weight: 0.2}]\n")
        files = {"work/app/many.md": _mem("many"), "work/app/same.md": _mem("same"),
                 "work/app/withev.md": _mem("withev").replace("---\nwithev", "---\nwithev")}
        files["work/app/withev.md"] = _mem("withev").replace("---\nbody", ev + "---\nbody")
        for i, d in enumerate(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]):
            files[f"work/app/contexts/{d}-c{i}.md"] = _ctx(f"c{i}", d, ["many", "same"] if i < 3 else ["same"])
        for i in (1, 2):
            files[f"work/app/experiences/e{i}.md"] = (
                f"---\nid: e{i}\ndate: 2026-02-0{i}\nkind: error\nagent: codex\n---\nx\n")
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        g = Graph(Store(make_store(Path(t.name), files)))
        self.assertEqual([(m.slug, n) for m, n in g.hot(2)], [("same", 4), ("many", 3), ("withev", 2)])
        self.assertEqual([m.slug for m, _ in g.hot(4)], ["same"])

    def _ranked(self, mems, nexp=6):
        """mems: slug -> [(experience index, kind, weight)]; returns hot(1) slugs."""
        files = {}
        for i in range(nexp):
            files[f"work/app/experiences/e{i}.md"] = (
                f"---\nid: e{i}\ndate: 2026-02-0{i + 1}\nkind: error\nagent: codex\n---\nx\n")
        for slug, items in mems.items():
            ev = ", ".join(f"{{ref: work/app/experiences/e{i}, kind: {k}, weight: {w}}}" for i, k, w in items)
            files[f"work/app/{slug}.md"] = _mem(slug).replace("---\nbody", f"evidence: [{ev}]\n---\nbody")
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return [m.slug for m, _ in Graph(Store(make_store(Path(t.name), files))).hot(1)]

    def test_hot_multiplier_not_confidence_alone(self):
        # conf alone: one > many (.63 > .59); conf x n: many (1.78) > one (.63)
        got = self._ranked({"one": [(0, "supports", 1.0)], "many": [(i, "supports", 0.3) for i in (1, 2, 3)]})
        self.assertEqual(got, ["many", "one"])

    def test_hot_confidence_not_count_alone(self):
        # n alone: weak (3) > strong (1); conf x n: strong .63 > weak .42
        got = self._ranked({"strong": [(0, "supports", 1.0)], "weak": [(i, "supports", 0.05) for i in (1, 2, 3)]})
        self.assertEqual(got, ["strong", "weak"])

    def test_hot_refutes_lower_rank(self):
        sup = [(i, "supports", 1.0) for i in (0, 1, 2)]
        got = self._ranked({"refuted": sup + [(i, "refutes", 1.0) for i in (3, 4, 5)],
                            "plain": [(3, "supports", 1.0), (4, "supports", 1.0)]})
        self.assertEqual(got[0], "plain")

    def test_contexts_sorted_by_date(self):
        self.assertEqual([c.date for c in self.g.contexts_of("phpcs", env="work")],
                         ["2026-01-01", "2026-02-01", "2026-03-01"])

    def test_related(self):
        self.assertEqual(self.g.related("phpcs", env="work"), ["other", "style"])
        self.assertEqual(self.g.related("style"), ["phpcs"])

    def test_find_body_match(self):
        hits = self.g.find("PHPCS")
        self.assertEqual([getattr(h, "slug", None) for h in hits][:2], ["phpcs", "phpcs"])
        self.assertTrue(any(isinstance(h, Context) for h in self.g.find("first line of c1")))

    def test_ambiguous(self):
        with self.assertRaises(AmbiguousSlug):
            self.g.resolve("phpcs")
        self.assertIsNone(self.g.resolve("nope"))


class CliReadTest(unittest.TestCase):
    def run_cli(self, *argv, files=None):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root = make_store(Path(t.name), files or _files())
        os.environ["LRDMEM_STORE"] = str(root)
        self.addCleanup(os.environ.pop, "LRDMEM_STORE", None)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_find(self):
        code, out, _ = self.run_cli("find", "phpcs")
        self.assertEqual(code, 0)
        self.assertIn("pattern work/app/phpcs (3) — about phpcs", out)
        self.assertIn("  related: other, style", out)

    def test_show_and_why(self):
        code, out, _ = self.run_cli("show", "phpcs", "--env", "work")
        self.assertEqual(code, 0)
        self.assertIn("Run phpcs.\nSecond line.", out)
        self.assertIn("Contexts:", out)
        self.assertIn("2026-01-01 claude chat — first line of c1", out)
        _, out, _ = self.run_cli("why", "phpcs", "--env", "work")
        self.assertLess(out.index("2026-01-01"), out.index("2026-03-01"))
        self.assertIn("first line of c1\nmore", out)

    def test_show_and_why_v2_fields(self):
        exp = ("---\nid: e1\ndate: 2026-10-20T10:00:00Z\nkind: discovery\nagent: codex\ndevice: d\n"
               "source: chat\n---\n\nmigration failed\n")
        mem = ("---\nname: deps\ndescription: deps first\nmetadata:\n  type: feedback\n  memory_type: procedural\n"
               "  state: candidate\n  author: t\n  created: 2026-10-20\n  updated: 2026-10-20\n"
               "evidence: [{ref: work/web/experiences/e1, kind: supports, weight: 1.0}]\n---\nUpdate deps first.\n")
        edge = ("---\nid: x1\nsource: work/web/deps\ntarget: work/web/other\ntype: caused_by\nstate: proposed\n---\n")
        rev = ("---\nid: 2026-10-20-r1\ntarget: work/web/deps\naction: create\nby: codex\nreason: new\n"
               "before:\nafter:\n  state: candidate\n---\n")
        files = {"work/web/MEMORY.md": "", "work/web/deps.md": mem, "work/web/other.md": _mem("other"),
                 "work/web/experiences/e1.md": exp, "work/web/edges/x1.md": edge,
                 "work/web/revisions/2026-10-20-r1.md": rev}
        code, out, err = self.run_cli("show", "deps", "--env", "work", files=files)
        self.assertEqual(code, 0, err)
        self.assertIn("type: procedural  state: candidate  confidence: 0.63", out)
        self.assertIn("Evidence:\n  supports 1.0 work/web/experiences/e1", out)
        self.assertIn("Relations:\n  caused_by → work/web/other (proposed)", out)
        self.assertIn("Revisions: 1", out)
        _, out, _ = self.run_cli("why", "deps", "--env", "work", files=files)
        self.assertIn("2026-10-20T10:00:00Z codex chat — migration failed", out)
        self.assertIn("Revisions:\n  2026-10-20 create by codex — new", out)
        _, out, _ = self.run_cli("find", "migration", files=files)
        self.assertIn("context work/web/experiences/e1 — migration failed", out)

    def test_ambiguous_and_unknown(self):
        code, _, err = self.run_cli("show", "phpcs")
        self.assertEqual(code, 2)
        self.assertIn("work/app/phpcs", err)
        code, _, err = self.run_cli("why", "nope")
        self.assertEqual(code, 1)

    def test_hot(self):
        code, out, _ = self.run_cli("hot")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "work/app/phpcs  3  [candidate: obligation]")
        code, out, _ = self.run_cli("hot", "--min", "2")
        self.assertNotIn("other", out)  # 1 context
        self.assertEqual(self.run_cli("hot", "--min", "4")[1], "")

    def test_global_and_env_same_slug(self):
        files = {"_global/style.md": _mem("style", "global style"), "work/app/style.md": _mem("style", "app style"),
                 "work/app/contexts/2026-01-01-c.md": _ctx("c", "2026-01-01", ["style"])}
        code, out, _ = self.run_cli("find", "style", files=files)
        self.assertEqual(code, 0)
        self.assertIn("_global/style (0)", out)
        self.assertIn("work/app/style (1)", out)
        code, out, _ = self.run_cli("show", "style", "--env", "home", files=files)
        self.assertEqual((code, "global style" in out), (0, True))
        code, out, _ = self.run_cli("why", "style", "--env", "work", files=files)
        self.assertEqual((code, "first line of c" in out), (0, True))


if __name__ == "__main__":
    unittest.main()
