import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from lrdmem.frontmatter import parse
from lrdmem.graph import Graph
from lrdmem.learn import LearnRefused, learn
from lrdmem.store import Store
from tests.fixtures import make_store

TODAY = date.today().isoformat()
KW = dict(mistake="used tabs", correction="use spaces", scope="repo", agent="claude")
NEW = dict(KW, description="Indent with spaces always", body="**Why:** style.\n**How to apply:** spaces.")


class LearnTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": ""})
        self.repo = self.tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.store = Store(self.root)

    def learn(self, **kw):
        return learn(self.store, kw.pop("cwd", self.repo), **kw)

    def test_creates_memory_context_and_index(self):
        r = self.learn(memory="spaces", files=["a.py"], pr="http://x/1", **NEW)
        self.assertTrue(r.created)
        self.assertEqual(r.memory, self.root / "acme/web/spaces.md")
        self.assertEqual(r.context, self.root / f"acme/web/contexts/{TODAY}-spaces.md")
        m = parse(r.memory.read_text())
        self.assertEqual(m["metadata"]["author"], "claude")
        self.assertEqual(m["metadata"]["updated"], TODAY)
        c = parse(r.context.read_text())
        self.assertEqual((c["memories"], c["files"], c["repo"], c["source"]), (["spaces"], ["a.py"], "web", "chat"))
        self.assertNotIn("mistake", c)
        self.assertIn("**Mistake:** used tabs", r.context.read_text())
        self.assertIn("**Correction:** use spaces", r.context.read_text())
        self.assertIn("- [spaces](spaces.md) — Indent with spaces always\n", (self.root / "acme/web/MEMORY.md").read_text())

    def test_second_call_adds_context_only(self):
        self.learn(memory="spaces", **NEW)
        r = self.learn(memory="spaces", **KW)
        self.assertFalse(r.created)
        self.assertEqual(r.context.name, f"{TODAY}-spaces-2.md")
        self.assertEqual(parse(r.context.read_text())["id"], f"{TODAY}-spaces-2")
        self.assertEqual((self.root / "acme/web/MEMORY.md").read_text().count("spaces.md"), 1)
        self.assertEqual(Graph(self.store).strength("spaces", "acme"), 2)

    def test_existing_memory_needs_no_description(self):
        with self.assertRaises(LearnRefused):
            self.learn(memory="fresh", **KW)

    def test_duplicates_refused_and_overridden(self):
        self.learn(memory="spaces", **NEW)
        dup = dict(NEW, description="Always indent using spaces")
        with self.assertRaises(LearnRefused) as cm:
            self.learn(memory="indent-spaces", **dup)
        self.assertEqual(cm.exception.duplicates, ["spaces"])
        self.assertFalse((self.root / "acme/web/indent-spaces.md").exists())
        r = self.learn(memory="indent-spaces", new=True, **dup)
        self.assertTrue(r.created)
        self.assertEqual(r.duplicates, ["spaces"])

    def test_secret_refused_nothing_written(self):
        secret = "token = abcdef123456"
        for field in ("mistake", "correction", "body", "description", "files", "pr", "agent"):
            kw = dict(NEW)
            if field == "files":
                kw["files"] = [secret]
            elif field == "pr":
                kw["pr"] = secret
            else:
                kw[field] = secret
            with self.subTest(field), self.assertRaises(LearnRefused):
                self.learn(memory="spaces", **kw)
        self.assertFalse((self.root / "acme/web/spaces.md").exists())
        self.assertFalse((self.root / "acme/web/contexts").exists())

    def test_env_must_be_a_known_environment(self):
        for bad in ("..", str(self.tmp), "nope"):
            with self.subTest(bad), self.assertRaises(LearnRefused):
                self.learn(memory="spaces", env=bad, **NEW)
        self.assertEqual(list(self.tmp.glob("*.md")), [])

    def test_env_escape_via_symlink_refused(self):
        outside = self.tmp / "outside"
        (outside / "web").mkdir(parents=True)
        (self.root / "link").symlink_to(outside)
        r = self.learn(memory="spaces", env="link", **NEW)  # symlinked env dir itself is allowed
        self.assertEqual(r.memory.parent.resolve(), (outside / "web").resolve())
        (self.root / "acme/web").rename(self.root / "acme/web-real")
        (self.root / "acme/web").symlink_to(self.tmp)
        with self.assertRaises(LearnRefused):
            self.learn(memory="other-thing", env="acme", **dict(NEW, description="Entirely different words here"))

    def test_symlinks_refused(self):
        evil = self.tmp / "evil"
        evil.mkdir()
        folder = self.root / "acme/web"
        (folder / "MEMORY.md").unlink()
        (folder / "MEMORY.md").symlink_to(evil / "idx.md")
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **NEW)
        (folder / "MEMORY.md").unlink()
        (folder / "MEMORY.md").write_text("")
        (folder / "contexts").symlink_to(evil)
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **NEW)
        (folder / "contexts").unlink()
        (folder / "spaces.md").symlink_to(evil / "m.md")
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **KW)
        self.assertEqual(list(evil.iterdir()), [])

    def test_same_slug_in_other_visible_scope_refused(self):
        self.learn(memory="tabs", **dict(NEW, scope="global", description="Tabs are wrong"))
        with self.assertRaises(LearnRefused) as cm:
            self.learn(memory="tabs", new=True, **dict(NEW, description="Zebra giraffe lemur"))
        self.assertIn("_global/tabs.md", str(cm.exception))
        self.assertFalse((self.root / "acme/web/tabs.md").exists())

    def test_reserved_slugs(self):
        for bad in ("memory", "readme", "contexts"):
            with self.subTest(bad), self.assertRaises(LearnRefused):
                self.learn(memory=bad, **NEW)

    def test_home_path_env_vs_global(self):
        home = dict(NEW, mistake=f"edited {Path.home()}/x")
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **home)
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **dict(home, scope="env"))
        r = self.learn(memory="spaces", **dict(home, scope="global"))
        self.assertEqual(r.memory.parent, self.root / "_global")

    def test_links_must_resolve(self):
        with self.assertRaises(LearnRefused):
            self.learn(memory="spaces", **dict(NEW, body="**Why:** [[nope]]"))
        self.learn(memory="base", **NEW)
        self.learn(memory="other", **dict(NEW, description="Unrelated topic entirely", body="see [[base]]"))

    def test_bad_slug_and_scope(self):
        for bad in ("../../x", "Up", "a/b", "", "-a"):
            with self.assertRaises(LearnRefused):
                self.learn(memory=bad, **NEW)
        with self.assertRaises(LearnRefused):
            self.learn(memory="ok", **dict(NEW, scope="nope"))

    def test_not_opted_in(self):
        other = self.tmp / "unlisted"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        with self.assertRaises(LearnRefused):
            self.learn(cwd=other, memory="spaces", **NEW)
        self.assertTrue(self.learn(cwd=other, memory="spaces", **dict(NEW, scope="global")).created)

    def test_scope_env(self):
        r = self.learn(memory="spaces", **dict(NEW, scope="env"))
        self.assertEqual(r.memory.parent, self.root / "acme/_global")
        self.assertNotIn("repo", parse(r.context.read_text()))

    def test_updates_existing_metadata(self):
        self.learn(memory="spaces", **NEW)
        p = self.root / "acme/web/spaces.md"
        p.write_text(p.read_text().replace(TODAY, "2020-01-01"))
        self.learn(memory="spaces", **KW)
        text = p.read_text()
        self.assertEqual(parse(text)["metadata"]["updated"], TODAY)
        self.assertIn("**Why:** style.", text)

    def test_unparseable_memory_untouched(self):
        (self.root / "acme/web/spaces.md").write_text("no frontmatter\n")
        self.learn(memory="spaces", **KW)
        self.assertEqual((self.root / "acme/web/spaces.md").read_text(), "no frontmatter\n")

    def test_odd_characters_roundtrip(self):
        r = self.learn(memory="spaces", files=["it's.py", 'a"b.py'], **NEW)
        self.assertEqual(parse(r.context.read_text())["files"], ["it's.py", 'a"b.py'])

    def test_concurrent_distinct_slugs_both_indexed(self):
        errs = []

        def run(slug, desc):
            try:
                self.learn(memory=slug, **dict(NEW, description=desc))
            except Exception as e:  # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=run, args=(f"slug-{i}", f"zebra{i} giraffe{i} lemur{i}")) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        idx = (self.root / "acme/web/MEMORY.md").read_text()
        for i in range(8):
            self.assertIn(f"(slug-{i}.md)", idx)

    def test_cli_prints_paths_and_refusals(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from lrdmem.cli import main
        argv = ["learn", "--memory", "spaces", "--mistake", "m", "--correction", "c", "--scope", "repo",
                "--agent", "claude", "--description", "Indent with spaces always", "--body", "b", "--files", "a.py", "b.py"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), mock.patch("os.getcwd", return_value=str(self.repo)), \
                mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(argv), 0)
            self.assertEqual(out.getvalue().split(), [f"acme/web/contexts/{TODAY}-spaces.md", "acme/web/spaces.md"])
            self.assertEqual(main(argv[:2] + ["indent-spaces"] + argv[3:]), 1)
        self.assertEqual(err.getvalue().split("\n")[-2], "spaces")


if __name__ == "__main__":
    unittest.main()
