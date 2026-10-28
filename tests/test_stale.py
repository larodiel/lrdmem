import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.stale import stale
from lrdmem.store import Store
from tests.fixtures import make_store
from tests.test_graph import _mem


class StaleTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        (self.tmp / "repo" / "src").mkdir(parents=True)
        (self.tmp / "repo" / "src" / "a.php").write_text("x")
        body = "See `/Users/nope/x.php`, `~/nope-dir/y`, `src/a.php`, `src/gone.php`, `gone.md`, `word`."
        self.root = make_store(self.tmp, {"work/app/m.md": _mem("m", body)})
        self.store = Store(self.root)

    def test_absolute_only_without_repo_root(self):
        self.assertEqual([p for _, p in stale(self.store, None)], ["/Users/nope/x.php", "~/nope-dir/y"])

    def test_relative_with_repo_root(self):
        found = [p for _, p in stale(self.store, self.tmp / "repo")]
        self.assertEqual(found, ["/Users/nope/x.php", "~/nope-dir/y", "src/gone.php", "gone.md"])

    def test_existing_absolute_not_flagged(self):
        f = self.tmp / "real.txt"
        f.write_text("x")
        (self.root / "work/app/m.md").write_text(_mem("m", f"`{f}`"))
        self.assertEqual(stale(self.store, None), [])

    def test_cli_exit_code_and_output(self):
        out = io.StringIO()
        env = {"LRDMEM_STORE": str(self.root)}
        cwd = os.getcwd()
        os.chdir(self.tmp)  # not a git repo
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["stale"])
        self.assertEqual(code, 1)
        self.assertIn("work/app/m: /Users/nope/x.php", out.getvalue())

    def test_cli_graph_output_file(self):
        target = self.tmp / "g.json"
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["graph", "--format", "json", "-o", str(target)]), 0)
        self.assertIn('"nodes"', target.read_text())


if __name__ == "__main__":
    unittest.main()
