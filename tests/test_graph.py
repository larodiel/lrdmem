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
