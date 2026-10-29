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



class RevisionFixRound1Test(unittest.TestCase):
    setUp = RevisionTest.setUp
    record = RevisionTest.record

    def promote(self, state="active", old="candidate"):
        write.set_fields(self.mem, {"state": state})
        return self.record(before={"state": old}, after={"state": state})

    def test_escaped_secrets_refused(self):
        pw = "pass" + "word\n=hunter22xyz"
        for kw in ({"before": {"description": pw}}, {"after": {"description": pw}}, {"reason": pw}):
            with self.subTest(kw), self.assertRaises(write.Refused):
                self.record(**kw)
        self.assertFalse(list((self.folder / "revisions").glob("*.md")))

    def test_repeated_same_day_changes_each_recorded(self):
        r1 = self.promote()
        a = revision.revert(self.store, r1.stem, "h")
        r2 = self.promote()
        b = revision.revert(self.store, r2.stem, "h")
        self.assertEqual(len({r1, a, r2, b}), 4)
        self.assertEqual(len(list((self.folder / "revisions").glob("*.md"))), 4)
        self.assertIn("  state: candidate\n", self.mem.read_text())
        self.assertEqual(self.record(before={"state": "active"}, after={"state": "candidate"},
                                     action="revert", reason="again", by="x").name, b.name)  # same tip: no-op

    def test_independent_stores_with_same_history_get_same_ids(self):
        def history(root):
            store = Store(root)
            folder, mem = root / "acme/web", root / "acme/web/m.md"
            ids = []
            for old, new in (("candidate", "active"), ("active", "contested"), ("contested", "active")):
                write.set_fields(mem, {"state": new})
                ids.append(revision.record(store, folder, "acme/web/m", "state_change", {"state": old},
                                           {"state": new}, "r", "c").name)
            return ids
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        other = make_store(Path(t.name), {"_global/MEMORY.md": "", "acme/web/MEMORY.md": "", "acme/web/m.md": MEM})
        self.assertEqual(history(self.root), history(other))

    def test_revert_refuses_target_outside_revision_scope(self):
        other = self.root / "beta/app"
        other.mkdir(parents=True)
        (other / "MEMORY.md").write_text("")
        rev = self.promote()
        planted = other / "revisions" / rev.name
        planted.parent.mkdir()
        rev.rename(planted)  # a revision in env beta that targets acme/web/m
        with self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "h")
        self.assertIn("  state: active\n", self.mem.read_text())

    def test_revert_restores_only_allowlisted_fields(self):
        write.set_fields(self.mem, {"author": "evil"})
        rev = self.record(action="content_change", before={"author": "me"}, after={"author": "evil"})
        with self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "h")
        self.assertIn("  author: evil\n", self.mem.read_text())
        write.set_fields(self.mem, {"description": "d2", "valid_until": "2027-01-01"})
        rev = self.record(action="content_change", before={"description": "d", "valid_until": ""},
                          after={"description": "d2", "valid_until": "2027-01-01"})
        revision.revert(self.store, rev.stem, "h")
        self.assertIn("\ndescription: d\n", self.mem.read_text())

    def test_revert_validates_its_revision_before_editing(self):
        rev = self.promote()
        real = self.root / "elsewhere"
        (self.folder / "revisions").rename(real)
        (self.folder / "revisions").symlink_to(real)  # readable through the link, but the revert must not write there
        with self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "h")
        self.assertIn("  state: active\n", self.mem.read_text())
        self.assertEqual(len(list(real.iterdir())), 1)

    def test_expect_closes_the_check_to_edit_race(self):
        rev = self.promote()
        real = write.set_fields

        def racing(path, changes, expect=None):
            real(path, {"state": "obsolete"})  # another writer lands between revert's check and its edit
            return real(path, changes, expect=expect)

        with mock.patch.object(write, "set_fields", racing), self.assertRaises(write.Refused):
            revision.revert(self.store, rev.stem, "h")
        self.assertIn("  state: obsolete\n", self.mem.read_text())
        self.assertEqual(len(list((self.folder / "revisions").glob("*.md"))), 1)



class RevisionFixRound2Test(unittest.TestCase):
    setUp = RevisionTest.setUp
    record = RevisionTest.record
    promote = RevisionFixRound1Test.promote

    def plant(self, name, text):
        other = self.root / "beta/app"
        (other / "revisions").mkdir(parents=True, exist_ok=True)
        (other / "MEMORY.md").write_text("")
        p = other / "revisions" / f"{name}.md"
        p.write_text(text)
        return p

    def test_planted_foreign_revisions_cannot_drive_a_revert(self):
        write.set_fields(self.mem, {"state": "active"})
        self.plant("2020-01-01-ra", "---\nid: 2020-01-01-ra\ndate: 2020-01-01\ntarget: acme/web/m\n"
                   "action: state_change\nbefore:\n  state: candidate\nafter:\n  state: active\nreason: r\nby: x\n---\n")
        self.plant("2099-01-01-rb", "---\nid: 2099-01-01-rb\ndate: 2099-01-01\ntarget: acme/web/m\naction: revert\n"
                   "before:\n  state: active\nafter:\n  state: candidate\nreason: revert 2020-01-01-ra\nby: h\n---\n")
        with self.assertRaises(write.Refused):
            revision.revert(self.store, "2020-01-01-ra", "h")
        self.assertIn("  state: active\n", self.mem.read_text())
        self.assertFalse((self.folder / "revisions").exists())

    def test_foreign_revisions_do_not_salt_ids(self):
        a = self.promote()
        a.unlink()
        self.plant("2099-01-01-rz", "---\nid: 2099-01-01-rz\ndate: 2099-01-01\ntarget: acme/web/m\n"
                   "action: state_change\nbefore:\n  state: x\nafter:\n  state: y\nreason: r\nby: x\n---\n")
        self.assertEqual(self.promote().name, a.name)

    def test_revert_refuses_when_its_revision_already_exists(self):
        rev = self.promote()
        b = revision.revert(self.store, rev.stem, "h")
        write.set_fields(self.mem, {"state": "active"})  # changed back by hand, nothing recorded
        with self.assertRaises(write.Refused) as cm:
            revision.revert(self.store, rev.stem, "h")
        self.assertIn("nothing to revert", str(cm.exception))
        self.assertIn("  state: active\n", self.mem.read_text())
        self.assertEqual(len(list((self.folder / "revisions").glob("*.md"))), 2)
        self.assertTrue(b.exists())

    def test_non_string_values_refused(self):
        for kw in ({"before": {"state": {"nested": "x"}}}, {"after": {"state": ["a"]}}, {"after": {"state": 1}}):
            with self.subTest(kw), self.assertRaises(write.Refused):
                self.record(**kw)
        self.assertFalse((self.folder / "revisions").exists())


if __name__ == "__main__":
    unittest.main()
