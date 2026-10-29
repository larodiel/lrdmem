import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from lrdmem import revision, write
from lrdmem.cli import main
from lrdmem.lint import lint
from lrdmem.store import Store
from tests.fixtures import make_store

TODAY = date.today().isoformat()
MEM = ("---\nname: m\ndescription: d\nmetadata:\n  type: feedback\n  author: me\n  state: candidate\n  updated: 2020-01-01\n"
       "---\nbody\n\n**Why:** w\n**How to apply:** h\n")


class RevisionTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = make_store(Path(t.name), {"_global/MEMORY.md": "", "acme/web/MEMORY.md": "- [m](m.md) — d\n",
                                              "acme/web/m.md": MEM})
        self.store = Store(self.root)
        self.folder = self.root / "acme/web"
        self.mem = self.folder / "m.md"

    def record(self, **kw):
        args = dict(target_ref="acme/web/m", action="state_change", before={"state": "candidate"},
                    after={"state": "active"}, reason="2 supports", by="consolidate")
        args.update(kw)
        return revision.record(self.store, self.folder, **args)

    def test_record_writes_parseable_revision(self):
        p = self.record()
        self.assertRegex(p.name, rf"^{TODAY}-r[0-9a-f]{{8}}\.md$")
        r = self.store.revisions()[0]
        self.assertEqual((r.id, r.target, r.action, r.before, r.after, r.by, r.date),
                         (p.stem, "acme/web/m", "state_change", {"state": "candidate"}, {"state": "active"},
                          "consolidate", TODAY))
        self.assertEqual(lint(self.store), [])

    def test_same_content_is_one_file(self):
        a = self.record()
        b = self.record(reason="other words", by="claude")  # id hashes target, action, before and after only
        self.assertEqual(a, b)
        self.assertEqual(len(list((self.folder / "revisions").iterdir())), 1)
        c = self.record(after={"state": "contested"})
        self.assertNotEqual(a, c)

    def test_record_validates(self):
        for kw in ({"action": "nope"}, {"target_ref": "acme/web/missing"}, {"before": {"bad key": "x"}}):
            with self.subTest(kw), self.assertRaises(write.Refused):
                self.record(**kw)
        self.assertFalse((self.folder / "revisions").exists())

    def test_record_refuses_secrets(self):
        secret = "token = " + "abcdef123456"
        for kw in ({"before": {"description": secret}}, {"after": {"description": secret}}, {"reason": secret}):
            with self.subTest(kw), self.assertRaises(write.Refused):
                self.record(**kw)
        self.assertFalse(list((self.folder / "revisions").glob("*.md")))

    def test_revert_restores_and_records(self):
        write.set_fields(self.mem, {"state": "active"})
        rev = self.record()
        out = revision.revert(self.store, rev.stem, "claude")
        self.assertEqual(self.mem.read_text(), MEM)  # only the state line went back
        back = self.store.node(out.relative_to(self.root).with_suffix("").as_posix())
        self.assertEqual((back.action, back.before, back.after, back.by),
                         ("revert", {"state": "active"}, {"state": "candidate"}, "claude"))
        self.assertIn(rev.stem, back.reason)
        with self.assertRaises(write.Refused):  # already reverted: current no longer equals `after`
            revision.revert(self.store, rev.stem, "claude")

    def test_revert_refused_after_newer_change(self):
        write.set_fields(self.mem, {"state": "active"})
        rev = self.record()
        write.set_fields(self.mem, {"state": "obsolete"})
        changed = self.mem.read_text()
        with self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "claude")
        self.assertEqual(self.mem.read_text(), changed)
        self.assertEqual(len(self.store.revisions()), 1)

    def test_revert_edge_state(self):
        edge = self.folder / "edges/e.md"
        edge.parent.mkdir()
        edge.write_text("---\nid: e\nsource: acme/web/m\ntarget: acme/web/m\ntype: related_to\nstate: validated\n---\n")
        rev = self.record(target_ref="acme/web/edges/e", before={"state": "proposed"}, after={"state": "validated"})
        revision.revert(self.store, rev.stem, "claude")
        self.assertIn("state: proposed\n", edge.read_text())

    def test_revert_unknown_or_empty(self):
        with self.assertRaises(write.Refused):
            revision.revert(self.store, "2020-01-01-rnope", "claude")
        rev = self.record(action="create", before={}, after={"state": "candidate"})
        with self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "claude")

    def test_cli_revert(self):
        write.set_fields(self.mem, {"state": "active"})
        rev = self.record()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["revert", rev.stem]), 0)
            new = out.getvalue().strip()
            self.assertRegex(new, rf"^acme/web/revisions/{TODAY}-r[0-9a-f]{{8}}$")
            self.assertEqual(self.store.node(new).action, "revert")
            self.assertEqual(main(["revert", rev.stem]), 1)
        self.assertIn("changed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
