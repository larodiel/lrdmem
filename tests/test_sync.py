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
from lrdmem.sync import SyncResult, autopush_enabled, pull, sync, toplevel

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
        self.assertEqual(g(a.root / "work", "log", "-1", "--format=%s").strip(), "feat(memory): learn 1 memories, 0 experiences")
        self.assertIn("A web/m.md", g(a.root / "work", "log", "-1", "--format=%b"))
        b = self.clone("b")
        self.assertEqual((b.root / "work" / "web" / "m.md").read_text(), "hello\n")
        write(b.root / "work" / "n.md", "from b\n")
        self.assertIsNone(sync(b)[0].error)
        self.assertIsNone(pull(a, "work"))
        self.assertTrue((a.root / "work" / "n.md").exists())

    def test_crash_left_temp_files_are_never_committed(self):
        a = self.clone("a")
        write(a.root / "work" / "web" / ".m.md.123.abcd.tmp", "partial")
        write(a.root / "work" / "web" / "m.md", "hello\n")
        self.assertIsNone(sync(a)[0].error)
        self.assertNotIn(".tmp", g(a.root / "work", "ls-files"))

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
        self.assertIn("conflict", res.error)
        self.assertIn("git pull --rebase", res.error)  # the way out, since the rebase was aborted
        self.assertNotIn("git rebase --abort", res.error)  # no raw git hints that no longer apply
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
        for kw in (k for k in calls if "GIT_TERMINAL_PROMPT" in k["env"]):  # all but the core.sshCommand probe
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kw["env"]["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes -o ConnectTimeout=3")

    def test_user_ssh_command_is_respected(self):
        a = self.clone("a")
        g(a.root / "work", "config", "core.sshCommand", "ssh -i /nonexistent")
        calls = []
        real = subprocess.run
        def spy(*args, **kw):
            if args[0][args[0].index("-C") + 1].endswith("work"):
                calls.append(kw)
            return real(*args, **kw)
        with mock.patch("lrdmem.sync.subprocess.run", spy), mock.patch.dict(os.environ):
            os.environ.pop("GIT_SSH_COMMAND", None)
            sync(a)
        pulled = [kw for kw in calls if "GIT_TERMINAL_PROMPT" in kw["env"]]
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

    def test_global_target_stages_only_global_folder(self):
        root = self.tmp / "s"
        write(root / "README.md", "r")
        write(root / "_global" / "m.md", "m")
        write(root / "plain" / "_global" / "x.md", "x")
        g(root, "init", "-q")
        g(root, "init", "-q", "own")
        write(root / "own" / "y.md", "y")
        self.assertEqual(sync(Store(root), "_global"), [SyncResult("_global", True, False, None)])
        self.assertEqual(g(root, "ls-files").split(), ["_global/m.md"])
        self.assertIn("?? y.md", g(root / "own", "status", "--porcelain"))
        self.assertIsNone(pull(Store(root), "_global"))

    def test_root_sync_with_gitignored_env_repos(self):
        root = self.tmp / "s"
        write(root / ".gitignore", "work/\npersonal\n")
        write(root / "_global" / "m.md", "m")
        g(root, "init", "-q")
        g(root, "init", "-q", "work")
        write(root / "work" / "x.md", "x")
        other = self.tmp / "personal-repo"
        g(self.tmp, "init", "-q", str(other))
        write(other / "y.md", "y")
        (root / "personal").symlink_to(other)
        res = {r.env: r for r in sync(Store(root))}
        self.assertEqual(res["_global"], SyncResult("_global", True, False, None))
        self.assertEqual(sorted(g(root, "ls-files").split()), [".gitignore", "_global/m.md"])
        self.assertEqual(g(root, "status", "--porcelain"), "")

    def test_skip_idle_does_no_network_when_nothing_to_push(self):
        a = self.clone("a")
        write(a.root / "work" / "x.md", "x")
        sync(a)
        g(a.root / "work", "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertEqual(sync(a, "work", skip_idle=True), [SyncResult("work", False, False, None)])
        self.assertIsNotNone(sync(a, "work")[0].error)  # a manual sync still pulls
        write(a.root / "work" / "y.md", "y")
        [res] = sync(a, "work", skip_idle=True)
        self.assertTrue(res.committed)
        self.assertIn("pull failed", res.error)

    def test_planted_repo_in_store_content_is_never_run(self):
        store = self.clone("a")
        marker = self.tmp / "pwned"
        exp = store.root / "work/app/experiences"
        exp.mkdir(parents=True)
        g(exp, "init", "--bare", "-q", ".")
        with (exp / "config").open("a") as f:
            f.write(f"[core]\n\tfsmonitor = touch {marker}\n")
        write(exp / "x.md", "---\nid: x\n---\n")
        sync(store)
        from lrdmem.lint import lint
        lint(store, git_checks=True)
        self.assertFalse(marker.exists())

    def plant_filter_repo(self, folder: Path, marker: Path, as_file: bool):
        """A non-bare repo (or a .git file to an outside gitdir) whose required clean filter touches marker."""
        gitdir_holder = folder if not as_file else self.tmp / "evil"
        gitdir_holder.mkdir(parents=True, exist_ok=True)
        g(gitdir_holder, "init", "-q")
        with (gitdir_holder / ".git" / "config").open("a") as f:
            f.write(f"[filter \"x\"]\n\tclean = \"touch {marker}; cat\"\n\trequired = true\n")
        write(folder / ".gitattributes", "* filter=x\n")
        write(folder / "q.md", "---\nid: q\n---\n")
        if as_file:
            (folder / ".git").write_text(f"gitdir: {gitdir_holder}/.git\n")
        g(folder, "add", "-A")  # control: an unhardened git does fire the planted filter
        self.assertTrue(marker.exists(), "the plant is not live")
        marker.unlink()

    def test_nested_repo_with_filter_driver_is_refused(self):
        for as_file in (False, True):
            with self.subTest(as_file=as_file):
                store = self.clone(f"n{as_file}")
                marker = self.tmp / f"pwned{as_file}"
                self.plant_filter_repo(store.root / "work/app/queue", marker, as_file)
                for _ in range(2):
                    (res,) = sync(store, "work")
                    self.assertIn("nested git repository inside the store", res.error)
                    self.assertIn("move it out of the store", res.error)
                    self.assertIn("work/app/queue", res.error)
                self.assertEqual(g(store.root / "work", "diff", "--cached", "--name-only"), "")
                self.assertFalse(marker.exists())
                self.assertIsNone(pull(store, "work"))
                self.assertFalse(marker.exists())

    def test_gitlink_staged_by_other_means_is_unstaged(self):
        store = self.clone("g")
        work = store.root / "work"
        write(work / "ok.md", "x")
        from lrdmem import sync as sync_mod
        real = sync_mod._git
        def plant(repo, *args, **kw):  # a repo appears between the scan and `git add`
            out = real(repo, *args, **kw)
            if args[:1] == ("add",):
                real(repo, "update-index", "--add", "--cacheinfo", "160000", "a" * 40, "sub")
            return out
        with mock.patch("lrdmem.sync._git", plant):
            (res,) = sync(store, "work")
        self.assertIn("gitlink", res.error)
        self.assertIn("git rm --cached", res.error)
        self.assertNotIn("sub", g(work, "diff", "--cached", "--name-only"))

    def test_git_calls_are_hardened(self):
        from lrdmem import sync as sync_mod
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="x", stderr="")
            sync_mod._git(self.tmp, "status")
        argv = run.call_args_list[-1].args[0]
        for opt in ("diff.ignoreSubmodules=all", "status.submoduleSummary=false", "submodule.recurse=false",
                    "safe.bareRepository=explicit", "core.fsmonitor=false", "core.hooksPath=/dev/null"):
            self.assertIn(opt, argv)
        env = run.call_args_list[-1].kwargs["env"]
        self.assertEqual(env["GIT_CEILING_DIRECTORIES"], str(self.tmp.parent))
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_toplevel_is_public(self):
        root = self.tmp / "s"
        g(self.tmp, "init", "-q", str(root))
        (root / "d").mkdir()
        self.assertEqual(toplevel(root / "d"), root.resolve())
        self.assertIsNone(toplevel(self.tmp))

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
