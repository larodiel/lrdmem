import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lrdmem.cli import main
from lrdmem.store import Store
from lrdmem.triggers import matches, render, select
from tests.fixtures import make_store


def _mem(name, trig, kind="pattern", body="body"):
    return (f"---\nname: {name}\ndescription: d\nmetadata:\n  type: feedback\n  kind: {kind}\n"
            f"triggers:\n{trig}---\n{body}\n")


PHP = "  paths: ['**/*.php']\n"


class TriggersTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def mem(self, files, slug="m", env="work"):
        store = Store(make_store(self.tmp, files))
        return store, next(m for m in store.memories() if m.slug == slug)

    def test_globs(self):
        _, m = self.mem({"work/app/m.md": _mem("m", PHP)})
        self.assertTrue(matches(m, "work", "app", path="src/a.php"))
        self.assertTrue(matches(m, "work", "app", path="a.php"))
        self.assertFalse(matches(m, "work", "app", path="a.js"))
        self.assertFalse(matches(m, "work", "app"))

    def test_command_prefix(self):
        _, m = self.mem({"work/app/m.md": _mem("m", "  commands: ['git commit']\n")})
        self.assertTrue(matches(m, "work", "app", command="git commit -m x"))
        self.assertFalse(matches(m, "work", "app", command="git status"))

    def test_repos_and_scopes(self):
        _, m = self.mem({"_global/m.md": _mem("m", PHP + "  repos: [app]\n")})
        self.assertTrue(matches(m, "work", "app", path="a.php"))
        self.assertFalse(matches(m, "work", "web", path="a.php"))
        self.tmp = Path(tempfile.mkdtemp(dir=self._t.name))
        _, g = self.mem({"_global/m.md": _mem("m", PHP)})
        self.assertTrue(matches(g, "work", "app", path="a.php"))
        self.tmp = Path(tempfile.mkdtemp(dir=self._t.name))
        _, e = self.mem({"work/_global/m.md": _mem("m", PHP)})
        self.assertFalse(matches(e, "work", "app", path="a.php"))
        self.tmp = Path(tempfile.mkdtemp(dir=self._t.name))
        _, e = self.mem({"work/_global/m.md": _mem("m", PHP + "  repos: [app]\n")})
        self.assertTrue(matches(e, "work", "app", path="a.php"))
        self.tmp = Path(tempfile.mkdtemp(dir=self._t.name))
        _, o = self.mem({"home/app/m.md": _mem("m", PHP)})
        self.assertFalse(matches(o, "work", "app", path="a.php"))

    def test_select_order_budget_exclude(self):
        files = {f"work/app/m{i}.md": _mem(f"m{i}", PHP, body="x" * 2000) for i in range(10)}
        files["work/app/ob.md"] = _mem("ob", PHP, kind="obligation", body="y")
        store = Store(make_store(self.tmp, files))
        got = select(store, "work", "app", path="a.php", budget=1500)
        self.assertEqual(got[0].slug, "ob")
        self.assertLess(len(got), 11)
        self.assertLessEqual(len(render(got, "a.php")) // 4, 1500)
        self.assertEqual([m.slug for m in select(store, "work", "app", path="a.php", exclude={"ob"}, budget=9999)][:1], ["m0"])
        self.assertTrue(render(got[:1], "a.php").startswith("## lrdmem: rules for a.php\n\n### ob\ny"))

    def test_cli_for(self):
        root = make_store(self.tmp, {"work/app/m.md": _mem("m", PHP)})
        repo = self.tmp / "app"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        old = (os.getcwd(), os.environ.get("LRDMEM_STORE"))
        os.chdir(repo)
        os.environ["LRDMEM_STORE"] = str(root)
        try:
            def run(*a):
                out = io.StringIO()
                with redirect_stdout(out):
                    main(["for", *a])
                return out.getvalue()
            self.assertIn("rules for src/a.php", run(str(repo / "src/a.php")))
            self.assertEqual(run("a.js"), "")
            self.assertIn("### m", run("a.php", "--session", "t5"))
            self.assertEqual(run("a.php", "--session", "t5"), "")
        finally:
            os.chdir(old[0])
            if old[1] is None:
                del os.environ["LRDMEM_STORE"]
            else:
                os.environ["LRDMEM_STORE"] = old[1]
            (Path(tempfile.gettempdir()) / "lrdmem" / "t5.json").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
