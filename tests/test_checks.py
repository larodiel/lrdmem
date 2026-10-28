import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import checks, config
from lrdmem.cli import main
from lrdmem.store import Memory, Store
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

        with mock.patch.object(checks, "TIMEOUT", 1):
            (self.root / "work/app/phpcs.md").write_text(_ob("sleep 30"))
            r = self._run(["a.php"], cfg=self._approve())
        self.assertEqual(r.failures, [("phpcs", "timed out after 1s")])

    def test_timeout_kills_whole_group(self):
        self._store("sh -c 'sleep 30 & echo $! > pid; wait'")
        with mock.patch.object(checks, "TIMEOUT", 1):
            self._run(["a.php"], cfg=self._approve())
        pid = int((self.repo / "pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def _unsafe_template(self, run):
        self._store(run)
        r = self._run(["a.php"], cfg=self._approve())
        self.assertEqual((r.ran, r.failures, r.skipped_pending), ([], [], []))
        self.assertEqual([s for s, _ in r.unrunnable], ["phpcs"])
        self.assertIn("{files} must be an unquoted word", r.unrunnable[0][1])
        self.assertFalse((self.repo / "ran.txt").exists())

    def test_quoted_placeholders_are_unrunnable(self):
        for run in ('echo "{files}" > ran.txt', "sh -c 'echo {files}' > ran.txt", "echo '{files}' > ran.txt",
                    'echo x{files} > ran.txt', 'echo \\{files} > ran.txt', 'echo "$(echo {files})" > ran.txt',
                    'echo {files}; echo "{files}" > ran.txt'):
            with self.subTest(run=run):
                self._unsafe_template(run)
                shutil.rmtree(self.root)

    def test_hostile_names_create_nothing(self):
        self._store("printf '%s\\n' {files} > out.txt")
        names = ["$(touch PWNED).php", "x';touch PWNED2;'.php", "`touch PWNED3`.php", "--config=evil.php"]
        r = self._run(names, cfg=self._approve())
        self.assertEqual(r.failures, [])
        self.assertEqual((self.repo / "out.txt").read_text().splitlines(), names[:3] + ["./--config=evil.php"])
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), ["out.txt"])

    def test_non_printable_or_non_ascii_unapprovable_and_unrunnable(self):
        for run in ("echo \u202e hi", "echo \x1b[31m", "echo caf\u00e9"):
            with self.subTest(run=run):
                self.assertIn("unapprovable: non-printable or non-ASCII characters",
                              checks.problem(Memory("m", Path("x.md"), None, None, {"metadata": {"kind": "obligation"}, "check": {"run": run}}, "")))

    def test_key_includes_env(self):
        self._store("true")
        cfg = config.load()
        cfg["allow"]["other/app/phpcs"] = checks.command_hash(self.store.memories()[0])
        self.assertEqual([m.slug for m in checks.pending(self.store, cfg)], ["phpcs"])

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
        self.assertIn("Only approve commands you have read.", out)
        code, out, err = self._main("check", "--on", "post-edit", "--files", "a.php")
        self.assertEqual(code, 0)
        self.assertIn("lrdmem check skipped: phpcs is not approved", out)
        code, out, _ = self._main("allow", "phpcs")
        self.assertEqual(code, 0)
        self.assertIn("Only approve commands you have read.", out)
        self.assertIn("sh -c 'exit 3'", out)
        self.assertIn(checks.command_hash(self.store.memories()[0]), out)
        self.assertEqual(config.load()["allow"]["work/app/phpcs"],
                         checks.command_hash(self.store.memories()[0]))
        code, out, err = self._main("check", "--on", "post-edit", "--files", "a.php")
        self.assertEqual(code, 1)
        self.assertIn("lrdmem check failed: phpcs", out)
        self.assertEqual(self._main("allow", "work/app/phpcs")[0], 0)
        self.assertEqual(self._main("allow", "nope")[0], 1)

    def test_allow_refuses_unsafe_and_ambiguous(self):
        self._store('echo "{files}"')
        os.environ["LRDMEM_STORE"] = str(self.root)
        code, out, err = self._main("allow", "phpcs")
        self.assertEqual(code, 1)
        self.assertIn("{files} must be an unquoted word", err)
        self.assertEqual(config.load()["allow"], {})
        self.assertIn("unrunnable", self._main("allow")[1])
        (self.root / "work/app/phpcs.md").write_text(_ob("true"))
        (self.root / "other/app").mkdir(parents=True)
        (self.root / "other/app/phpcs.md").write_text(_ob("true"))
        code, _, err = self._main("allow", "phpcs")
        self.assertEqual(code, 2)
        self.assertIn("work/app/phpcs", err)

    def test_check_not_opted_in_is_silent(self):
        self.root = make_store(self.tmp, {"work/other/m.md": _ob("true")})
        os.environ["LRDMEM_STORE"] = str(self.root)
        self.assertEqual(self._main("check", "--on", "stop"), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
