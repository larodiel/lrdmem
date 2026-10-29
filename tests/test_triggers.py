import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lrdmem.cli import main
from lrdmem.store import Store
from lrdmem.triggers import matches
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
            self.assertIn("memories for sub/foo.php", out.getvalue())
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
            self.assertIn("memories for src/a.php", run(str(repo / "src/a.php")))
            self.assertEqual(run("a.js"), "")
            self.assertIn("### work/app/m —", run("a.php", "--session", "t5"))
            self.assertEqual(run("a.php", "--session", "t5"), "")
        finally:
            os.chdir(old[0])
            if old[1] is None:
                del os.environ["LRDMEM_STORE"]
            else:
                os.environ["LRDMEM_STORE"] = old[1]


if __name__ == "__main__":
    unittest.main()
