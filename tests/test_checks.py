import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import checks, config
from lrdmem.cli import main
from lrdmem.store import Store
from tests.fixtures import make_store


def _ob(run, on="post-edit", paths="'**/*.php'"):
    return (f"---\ndescription: d\nmetadata:\n  type: feedback\n  kind: obligation\n"
            f"triggers:\n  paths: [{paths}]\ncheck:\n  on: {on}\n  run: {json.dumps(run)}\n---\nbody\n")


class _Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        p = mock.patch.dict(os.environ, {"LRDMEM_CONFIG": str(self.tmp / "cfg" / "c.json")})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("LRDMEM_STORE", None)

    def _store(self, run, **kw):
        self.root = make_store(self.tmp, {"work/app/phpcs.md": _ob(run, **kw)})
        self.store = Store(self.root)
        return self.store

    def _run(self, files, on="post-edit", cfg=None):
        return checks.run_checks(self.store, "work", "app", self.repo, on, files, cfg or config.load())

    def _approve(self):
        cfg = config.load()
        cfg["allow"]["work/app/phpcs"] = checks.command_hash(self.store.memories()[0])
        return cfg



class ChecksTest(_Base):
    def test_unapproved_is_skipped_then_runs_then_pending_again(self):
        self._store("printf x > ran.txt; echo {files}")
        r = self._run(["a.php"])
        self.assertEqual((r.skipped_pending, r.ran), (["phpcs"], []))
        self.assertFalse((self.repo / "ran.txt").exists())
        self.assertEqual([m.slug for m in checks.pending(self.store, config.load())], ["phpcs"])
        cfg = self._approve()
        self.assertEqual(checks.pending(self.store, cfg), [])
        r = self._run(["a.php"], cfg=cfg)
        self.assertEqual((r.ran, r.failures, r.skipped_pending), (["phpcs"], [], []))
        self.assertTrue((self.repo / "ran.txt").exists())
        (self.root / "work/app/phpcs.md").write_text(_ob("printf y > other.txt"))
        self.assertEqual([m.slug for m in checks.pending(self.store, cfg)], ["phpcs"])
        self.assertEqual(self._run(["a.php"], cfg=cfg).skipped_pending, ["phpcs"])

    def test_injection_file_name_is_one_argument(self):
        self._store('sh -c \'printf "%s\\n" "$#" "$@" > args.txt\' sh {files}')
        name = "a b;rm -rf x.php"
        (self.repo / "x.php").write_text("keep")
        r = self._run([name, "plain.txt", "b.php"], cfg=self._approve())
        self.assertEqual(r.failures, [])
        self.assertEqual((self.repo / "args.txt").read_text().splitlines(), ["2", name, "b.php"])
        self.assertEqual((self.repo / "x.php").read_text(), "keep")

    def test_failure_output_tail_and_timeout(self):
        self._store("seq 1 100; exit 3")
        r = self._run(["a.php"], cfg=self._approve())
        self.assertEqual(r.ran, [])
        self.assertEqual(r.failures[0][0], "phpcs")
        self.assertEqual(r.failures[0][1].splitlines()[-1], "100")
        self.assertEqual(len(r.failures[0][1].splitlines()), 60)

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("c", 300)
        r = checks.run_checks(self.store, "work", "app", self.repo, "post-edit", ["a.php"], self._approve(), run=boom)
        self.assertEqual(r.failures, [("phpcs", "timed out after 300s")])

    def test_on_filter_and_empty_files_and_no_placeholder(self):
        self._store("printf x > ran.txt", on="stop")
        cfg = self._approve()
        self.assertEqual(self._run(["a.php"], on="post-edit", cfg=cfg).ran, [])
        self.assertEqual(self._run(["a.txt"], on="stop", cfg=cfg).ran, [])
        self.assertEqual(self._run([], on="stop", cfg=cfg).ran, [])
        self.assertFalse((self.repo / "ran.txt").exists())
        self.assertEqual(self._run(["a.php"], on="stop", cfg=cfg).ran, ["phpcs"])
        self.assertTrue((self.repo / "ran.txt").exists())


class CliChecksTest(_Base):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self._cwd)

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_allow_and_check_flow(self):
        self._store("sh -c 'exit 3' {files}")
        os.environ["LRDMEM_STORE"] = str(self.root)
        code, out, _ = self._main("allow")
        self.assertIn("work/app/phpcs", out)
        self.assertIn("sh -c 'exit 3'", out)
        code, out, err = self._main("check", "--on", "post-edit", "--files", "a.php")
        self.assertEqual(code, 0)
        self.assertIn("phpcs", out + err)  # pending notice
        code, out, _ = self._main("allow", "phpcs")
        self.assertEqual(code, 0)
        self.assertIn("Only approve commands you have read.", out)
        self.assertIn("sh -c 'exit 3'", out)
        self.assertEqual(config.load()["allow"]["work/app/phpcs"],
                         checks.command_hash(self.store.memories()[0]))
        code, out, err = self._main("check", "--on", "post-edit", "--files", "a.php")
        self.assertEqual(code, 1)
        self.assertIn("phpcs", out + err)
        self.assertEqual(self._main("allow", "work/app/phpcs")[0], 0)
        self.assertEqual(self._main("allow", "nope")[0], 1)

    def test_check_not_opted_in_is_silent(self):
        self.root = make_store(self.tmp, {"work/other/m.md": _ob("true")})
        os.environ["LRDMEM_STORE"] = str(self.root)
        self.assertEqual(self._main("check", "--on", "stop"), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
