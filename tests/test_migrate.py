import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import migrate
from lrdmem.cli import main
from lrdmem.frontmatter import parse
from lrdmem.store import Store
from tests.fixtures import make_store

BODY = "Edit `src/Tickets/Order.php` and `.puprc`, never `src/**/*.min.js` or `and/or` or `~/x.php` or `.ts` or `VALID_*` or `with_*()` or `page.goto`.\n\n**Why:** w\n**How to apply:** h\n"
MISPLACED = "---\nname: {n}\ndescription: d\nmetadata:\n  type: feedback\nauthor: claude-code\nupdated: 2026-10-30\n{extra}---\n" + BODY


def files(**over):
    base = {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "- [co](co.md) — x\n", "acme/app/MEMORY.md": "- [a](a.md) — x\n- [ok](ok.md) — x\n",
            "acme/_global/co.md": MISPLACED.format(n="co", extra=""),
            "acme/app/a.md": MISPLACED.format(n="a", extra=""),
            "acme/app/ok.md": ("---\nname: ok\ndescription: d\nmetadata:\n  type: feedback\n  author: me\n  updated: 2026-01-01\n"
                               "triggers:\n  paths: [x/*.py]\n---\n" + BODY)}
    base.update(over)
    return base


class MigrateTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)

    def store(self, **over):
        self.root = make_store(self.tmp, files(**over))
        return Store(self.root)

    def test_plan_finds_misplaced_metadata_and_infers_paths_only_where_injection_can_happen(self):
        store = self.store()
        by = {c.memory.ref: c for c in migrate.plan(store, infer=True)}
        self.assertEqual(sorted(by), ["acme/_global/co", "acme/app/a"])  # ok is clean and already triggered
        self.assertEqual(by["acme/app/a"].hoist, ["author", "updated"])
        self.assertEqual(by["acme/app/a"].paths, ["**/src/Tickets/Order.php", "**/.puprc", "src/**/*.min.js"])
        self.assertEqual(by["acme/_global/co"].paths, [])  # env-wide without repos: never injected, nothing to infer

    def test_plan_without_infer_only_hoists_and_dry_run_writes_nothing(self):
        store = self.store()
        before = {p: p.read_bytes() for p in self.root.rglob("*.md")}
        plan = migrate.plan(store)
        self.assertTrue(all(c.paths == [] for c in plan))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*.md")}, before)

    def test_apply_writes_both_and_a_second_run_finds_nothing(self):
        store = self.store()
        migrate.apply(store, migrate.plan(store, infer=True))
        data = parse((self.root / "acme/app/a.md").read_text())
        self.assertEqual(data["metadata"]["author"], "claude-code")
        self.assertNotIn("author", data)
        self.assertEqual(data["triggers"]["paths"], ["**/src/Tickets/Order.php", "**/.puprc", "src/**/*.min.js"])
        self.assertEqual(migrate.plan(Store(self.root), infer=True), [])

    def test_large_memories_are_reported(self):
        big = MISPLACED.format(n="a", extra="") + "x" * 4100
        store = self.store(**{"acme/app/a.md": big})
        self.assertTrue(next(c for c in migrate.plan(store) if c.memory.slug == "a").large)

    def test_cli_is_a_dry_run_until_apply(self):
        self.store()
        env = {"LRDMEM_STORE": str(self.root)}
        out = io.StringIO()
        with mock.patch.dict("os.environ", env), redirect_stdout(out):
            self.assertEqual(main(["migrate", "--infer-triggers"]), 0)
        self.assertIn("acme/app/a: move author, updated into metadata", out.getvalue())
        self.assertIn("dry run", out.getvalue())
        self.assertIn("author: claude-code\nupdated", (self.root / "acme/app/a.md").read_text())
        with mock.patch.dict("os.environ", env), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["migrate", "--infer-triggers", "--apply", "--env", "acme"]), 0)
        self.assertEqual(migrate.plan(Store(self.root), infer=True), [])


if __name__ == "__main__":
    unittest.main()
