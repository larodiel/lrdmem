import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.store import Store
from lrdmem.sync import SyncResult, autopush_enabled, pull, sync

HERMETIC = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def g(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class SyncTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, HERMETIC)
        patcher.start()
        self.addCleanup(patcher.stop)
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.remote = self.tmp / "remote.git"
        g(self.tmp, "init", "--bare", str(self.remote))

    def clone(self, name: str) -> Store:
        """A store whose environment `work` is a clone of the bare remote."""
        root = self.tmp / name
        root.mkdir()
        g(root, "clone", "-q", str(self.remote), "work")
        return Store(root)

    def assertClean(self, repo: Path):
        self.assertEqual(g(repo, "status", "--porcelain"), "")
        for d in ("rebase-merge", "rebase-apply"):
            self.assertFalse((repo / g(repo, "rev-parse", "--git-path", d).strip()).exists())

    def test_push_then_other_clone_sees_file(self):
        a = self.clone("a")
        write(a.root / "work" / "web" / "m.md", "hello\n")
        self.assertEqual(sync(a), [SyncResult("work", True, True, None)])
        self.assertIn("lrdmem:", g(a.root / "work", "log", "-1", "--format=%s"))
        b = self.clone("b")
        self.assertEqual((b.root / "work" / "web" / "m.md").read_text(), "hello\n")
        write(b.root / "work" / "n.md", "from b\n")
        self.assertIsNone(sync(b)[0].error)
        self.assertIsNone(pull(a, "work"))
        self.assertTrue((a.root / "work" / "n.md").exists())

    def test_nothing_to_commit_and_custom_message(self):
        a = self.clone("a")
        write(a.root / "work" / "x.md", "x")
        sync(a, message="custom msg")
        self.assertEqual(g(a.root / "work", "log", "-1", "--format=%s").strip(), "custom msg")
        self.assertEqual(sync(a), [SyncResult("work", False, True, None)])

    def test_no_remote_commits_without_pushing(self):
        root = self.tmp / "s"
        g(root.parent, "init", "-q", str(root / "local"))
        write(root / "local" / "x.md", "x")
        self.assertEqual(sync(Store(root)), [SyncResult("local", True, False, None)])
        self.assertIsNone(pull(Store(root), "local"))

    def test_conflict_aborts_and_leaves_clean(self):
        a = self.clone("a")
        write(a.root / "work" / "f.md", "base\n")
        sync(a)
        b = self.clone("b")
        write(a.root / "work" / "f.md", "from a\n")
        sync(a)
        write(b.root / "work" / "f.md", "from b\n")
        [res] = sync(b)
        self.assertFalse(res.pushed)
        self.assertIn("work", res.error)
        self.assertClean(b.root / "work")
        self.assertEqual(g(self.remote, "show", "main:f.md"), "from a\n")
        self.assertIsNotNone(pull(b, "work"))
        self.assertClean(b.root / "work")

    def test_env_argument_touches_only_that_env(self):
        a = self.clone("a")
        g(a.root, "init", "-q", "local")
        write(a.root / "work" / "x.md", "x")
        write(a.root / "local" / "y.md", "y")
        self.assertEqual([r.env for r in sync(a, "work")], ["work"])
        self.assertIn("?? y.md", g(a.root / "local", "status", "--porcelain"))
        self.assertEqual(sorted(r.env for r in sync(a)), ["local", "work"])

    def test_not_a_git_repository_does_not_stop_others(self):
        a = self.clone("a")
        (a.root / "plain").mkdir()
        write(a.root / "work" / "x.md", "x")
        res = {r.env: r for r in sync(a)}
        self.assertEqual(res["plain"].error, "not a git repository")
        self.assertIsNone(res["work"].error)

    def test_root_repo_covers_global_and_nested_envs(self):
        root = self.tmp / "s"
        write(root / "_global" / "m.md", "m")
        write(root / "env" / "web" / "n.md", "n")
        g(root, "init", "-q")
        write(root / "_global" / "new.md", "new")
        self.assertEqual(sync(Store(root)), [SyncResult("_global", True, False, None)])
        self.assertEqual(g(root, "ls-files").count("new.md"), 1)

    def test_pull_reports_network_error(self):
        a = self.clone("a")
        write(a.root / "work" / "x.md", "x")
        sync(a)
        g(a.root / "work", "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertIsNotNone(pull(a, "work"))
        self.assertIsNotNone(sync(a)[0].error)

    def conflicted_clones(self):
        """Clones a and b where a already pushed an edit of f.md that b has not pulled."""
        a = self.clone("a")
        write(a.root / "work" / "f.md", "base\n")
        sync(a)
        b = self.clone("b")
        write(a.root / "work" / "f.md", "from a\n")
        sync(a)
        return a, b

    def test_hook_pull_skips_dirty_tree_without_stash(self):
        a, b = self.conflicted_clones()
        f = b.root / "work" / "f.md"
        write(f, "local edit\n")
        err = pull(b, "work")
        self.assertIn("skipped: uncommitted changes", err)
        self.assertEqual(f.read_text(), "local edit\n")
        self.assertEqual(g(b.root / "work", "stash", "list"), "")
        self.assertEqual(g(b.root / "work", "ls-files", "-u"), "")

    def test_sync_refuses_unmerged_paths(self):
        a, b = self.conflicted_clones()
        repo = b.root / "work"
        write(repo / "f.md", "from b\n")
        g(repo, "commit", "-qam", "b edit")
        g(repo, "fetch", "-q")
        subprocess.run(["git", "merge", "origin/main"], cwd=repo, capture_output=True)
        self.assertNotEqual(g(repo, "ls-files", "-u"), "")
        head = g(repo, "rev-parse", "HEAD")
        [res] = sync(b)
        self.assertIn("unmerged", res.error)
        self.assertFalse(res.pushed)
        self.assertEqual(g(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(g(self.remote, "show", "main:f.md"), "from a\n")

    def test_existing_rebase_is_not_aborted(self):
        a, b = self.conflicted_clones()
        repo = b.root / "work"
        write(repo / "f.md", "from b\n")
        g(repo, "commit", "-qam", "b edit")
        subprocess.run(["git", "pull", "--rebase"], cwd=repo, capture_output=True)
        in_rebase = (repo / g(repo, "rev-parse", "--git-path", "rebase-merge").strip()).exists()
        self.assertTrue(in_rebase)
        self.assertIn("rebase in progress", sync(b)[0].error)
        self.assertIn("rebase in progress", pull(b, "work"))
        self.assertTrue((repo / g(repo, "rev-parse", "--git-path", "rebase-merge").strip()).exists())

    def test_git_never_prompts(self):
        a = self.clone("a")
        calls = []
        real = subprocess.run
        def spy(*args, **kw):
            calls.append(kw)
            return real(*args, **kw)
        with mock.patch("lrdmem.sync.subprocess.run", spy):
            sync(a)
        self.assertTrue(calls)
        for kw in (k for k in calls if "env" in k):
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertIn("BatchMode=yes", kw["env"]["GIT_SSH_COMMAND"])

    def test_user_ssh_command_is_respected(self):
        a = self.clone("a")
        g(a.root / "work", "config", "core.sshCommand", "ssh -i /nonexistent")
        calls = []
        real = subprocess.run
        def spy(*args, **kw):
            if args[0][2].endswith("work"):
                calls.append(kw)
            return real(*args, **kw)
        with mock.patch("lrdmem.sync.subprocess.run", spy), mock.patch.dict(os.environ):
            os.environ.pop("GIT_SSH_COMMAND", None)
            sync(a)
        pulled = [kw for kw in calls if "env" in kw]
        self.assertTrue(pulled)
        self.assertTrue(all("GIT_SSH_COMMAND" not in kw["env"] for kw in pulled))

    def test_scoped_sync_with_dirty_other_env_does_not_pull(self):
        root = self.tmp / "s"
        write(root / "e1" / "x.md", "x")
        write(root / "e2" / "y.md", "y")
        g(root, "init", "-q")
        g(root, "add", "-A")
        g(root, "commit", "-qm", "init")
        g(root, "remote", "add", "origin", str(self.remote))
        write(root / "e1" / "x.md", "x2")
        write(root / "e2" / "y.md", "y2")
        [res] = sync(Store(root), "e1")
        self.assertEqual(res.error, "committed e1; not pulled/pushed: uncommitted changes outside e1")
        self.assertTrue(res.committed and not res.pushed)
        self.assertEqual((root / "e2" / "y.md").read_text(), "y2")

    def test_exclude_is_literal_and_nested_repo_not_gitlink(self):
        root = self.tmp / "s"
        write(root / "ab" / "x.md", "x")
        write(root / "a*" / "y.md", "y")
        g(root, "init", "-q")
        g(root, "init", "-q", "a*")
        res = {r.env: r for r in sync(Store(root))}
        self.assertTrue(res["_global"].committed and res["a*"].committed)
        files = g(root, "ls-files", "-s")
        self.assertIn("ab/x.md", files)
        self.assertNotIn("160000", files)

    def test_global_target_is_the_root_repo_only(self):
        root = self.tmp / "s"
        write(root / "README.md", "r")
        g(root, "init", "-q")
        g(root, "init", "-q", "own")
        write(root / "own" / "y.md", "y")
        self.assertEqual(sync(Store(root), "_global"), [SyncResult("_global", True, False, None)])
        self.assertEqual(g(root, "ls-files").split(), ["README.md"])
        self.assertIn("?? y.md", g(root / "own", "status", "--porcelain"))
        self.assertIsNone(pull(Store(root), "_global"))

    def test_plain_envs_in_root_repo_are_one_result(self):
        root = self.tmp / "s"
        write(root / "e1" / "x.md", "x")
        write(root / "e2" / "y.md", "y")
        g(root, "init", "-q")
        self.assertEqual([r.env for r in sync(Store(root))], ["_global"])

    def test_env_arg_in_root_repo_stages_only_that_env(self):
        root = self.tmp / "s"
        write(root / "e1" / "x.md", "x")
        write(root / "e2" / "y.md", "y")
        g(root, "init", "-q")
        self.assertEqual(sync(Store(root), "e1"), [SyncResult("e1", True, False, None)])
        self.assertEqual(g(root, "ls-files").split(), ["e1/x.md"])

    def test_first_push_sets_upstream(self):
        a = self.clone("a")
        write(a.root / "work" / "x.md", "x")
        self.assertTrue(sync(a)[0].pushed)
        self.assertEqual(g(a.root / "work", "rev-parse", "--abbrev-ref", "@{u}").strip(), "origin/main")
        self.assertIn("x.md", g(self.remote, "ls-tree", "--name-only", "main"))

    def test_timeout_returns_error(self):
        a = self.clone("a")
        write(a.root / "work" / "x.md", "x")
        sync(a)
        with mock.patch("lrdmem.sync.PULL_TIMEOUT", 0.001):
            self.assertIn("timed out", pull(a, "work"))
            [res] = sync(a)
        self.assertIn("timed out", res.error)
        self.assertFalse(res.pushed)

    def test_cli_says_committed_when_error_follows(self):
        a = self.clone("a")
        sync(a)
        g(a.root / "work", "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        write(a.root / "work" / "x.md", "x")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(a.root)}), redirect_stdout(out):
            self.assertEqual(main(["sync", "work"]), 1)
        self.assertTrue(out.getvalue().startswith("work: committed, error: "))

    def test_autopush_enabled(self):
        self.assertFalse(autopush_enabled({"autopush": {}}, "work"))
        self.assertTrue(autopush_enabled({"autopush": {"*": True}}, "work"))
        self.assertFalse(autopush_enabled({"autopush": {"*": True, "work": False}}, "work"))
        self.assertTrue(autopush_enabled({"autopush": {"work": True}}, "work"))

    def test_cli_sync(self):
        a = self.clone("a")
        g(a.root, "init", "-q", "local")
        write(a.root / "work" / "x.md", "x")
        write(a.root / "local" / "y.md", "y")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(a.root)}), redirect_stdout(out):
            self.assertEqual(main(["sync"]), 0)
        self.assertEqual(out.getvalue().splitlines(),
                         ["local: committed, not pushed (no remote)", "work: committed, pushed"])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(a.root)}), redirect_stdout(out):
            self.assertEqual(main(["sync", "work"]), 0)
        self.assertEqual(out.getvalue().strip(), "work: nothing to commit, pushed")

    def test_cli_sync_error_exit_code(self):
        a = self.clone("a")
        (a.root / "plain").mkdir()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(a.root)}), redirect_stdout(out):
            self.assertEqual(main(["sync", "plain"]), 1)
        self.assertEqual(out.getvalue().strip(), "plain: error: not a git repository")


if __name__ == "__main__":
    unittest.main()
