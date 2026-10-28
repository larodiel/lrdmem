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
        # hermetic session state: private TMPDIR per test
        state = Path(tempfile.mkdtemp(dir=self._t.name))
        old = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(state)
        tempfile.tempdir = None
        self.addCleanup(self._restore_tmp, old)

    @staticmethod
    def _restore_tmp(old):
        if old is None:
            del os.environ["TMPDIR"]
        else:
            os.environ["TMPDIR"] = old
        tempfile.tempdir = None

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

    def _scoped(self, files):
        return self.mem(files)[1]

    def test_repos_filter(self):
        m = self._scoped({"_global/m.md": _mem("m", PHP + "  repos: [app]\n")})
        self.assertTrue(matches(m, "work", "app", path="a.php"))
        self.assertFalse(matches(m, "work", "web", path="a.php"))

    def test_global_scope(self):
        self.assertTrue(matches(self._scoped({"_global/m.md": _mem("m", PHP)}), "work", "app", path="a.php"))

    def test_env_global_needs_repos(self):
        self.assertFalse(matches(self._scoped({"work/_global/m.md": _mem("m", PHP)}), "work", "app", path="a.php"))

    def test_env_global_with_repos_matches(self):
        m = self._scoped({"work/_global/m.md": _mem("m", PHP + "  repos: [app]\n")})
        self.assertTrue(matches(m, "work", "app", path="a.php"))

    def test_other_env_never_matches(self):
        self.assertFalse(matches(self._scoped({"home/app/m.md": _mem("m", PHP)}), "work", "app", path="a.php"))

    def test_str_valued_triggers(self):
        m = self._scoped({"work/app/m.md": _mem("m", "  paths: '**/*.php'\n  commands: git commit\n")})
        self.assertTrue(matches(m, "work", "app", path="src/a.php"))
        self.assertTrue(matches(m, "work", "app", command="git commit -m x"))

    def test_sort_tiebreak(self):
        ctx = lambda i, ms: (f"work/app/contexts/2026-01-0{i}-c.md",
                             f"---\nid: c{i}\ndate: 2026-01-0{i}\nsource: s\nagent: a\nmemories: [{ms}]\n---\nx\n")
        files = {f"work/app/{n}.md": _mem(n, PHP) for n in ("b", "a", "c")}
        files["work/app/o.md"] = _mem("o", PHP, kind="fact")
        files.update(dict([ctx(1, "c"), ctx(2, "c"), ctx(3, "b")]))
        got = select(Store(make_store(self.tmp, files)), "work", "app", path="a.php")
        self.assertEqual([m.slug for m in got], ["c", "b", "a", "o"])

    def test_first_match_over_budget_gives_nothing(self):
        store = Store(make_store(self.tmp, {"work/app/m.md": _mem("m", PHP, body="x" * 2000)}))
        self.assertEqual(select(store, "work", "app", path="a.php", budget=100), [])

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

    def test_cli_for_not_opted_in(self):
        root = make_store(self.tmp, {"work/other/m.md": _mem("m", PHP)})
        repo = self.tmp / "app"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        old = (os.getcwd(), os.environ.get("LRDMEM_STORE"))
        os.chdir(repo)
        os.environ["LRDMEM_STORE"] = str(root)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["for", "a.php"]), 0)
            self.assertEqual(out.getvalue(), "")
        finally:
            os.chdir(old[0])
            os.environ["LRDMEM_STORE"] = old[1] if old[1] else ""
            if not old[1]:
                del os.environ["LRDMEM_STORE"]

    def test_cli_for_relative_from_subdir(self):
        root = make_store(self.tmp, {"work/app/m.md": _mem("m", "  paths: ['sub/*.php']\n")})
        repo = self.tmp / "app"
        (repo / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        old = (os.getcwd(), os.environ.get("LRDMEM_STORE"))
        os.chdir(repo / "sub")
        os.environ["LRDMEM_STORE"] = str(root)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                main(["for", "foo.php"])
            self.assertIn("rules for sub/foo.php", out.getvalue())
        finally:
            os.chdir(old[0])
            os.environ["LRDMEM_STORE"] = old[1] if old[1] else ""
            if not old[1]:
                del os.environ["LRDMEM_STORE"]

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


if __name__ == "__main__":
    unittest.main()
