import os
import tempfile
import unittest
from pathlib import Path

from lrdmem import write
from lrdmem.frontmatter import parse
from lrdmem.model import Evidence
from lrdmem.store import Store, StoreFormatError
from tests.fixtures import make_store

SECRET = "token = " + "abcdef123456"
MEM = ("---\nname: m\ndescription: d\nmetadata:\n  type: feedback\n  state: candidate\n  updated: 2020-01-01\n"
       "triggers:\n  paths: ['*.py']\n---\nbody\n")


class WriteTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {"_global/MEMORY.md": "", "acme/web/MEMORY.md": "", "acme/web/m.md": MEM})
        self.store = Store(self.root)
        self.folder = self.root / "acme/web"
        self.mem = self.folder / "m.md"

    # target

    def test_target_inside_scope(self):
        self.assertEqual(write.target(self.store, self.folder, "contexts/x.md"), self.folder / "contexts/x.md")
        self.assertEqual(write.target(self.store, self.root / "_global", "y.md"), self.root / "_global/y.md")
        self.assertEqual((self.root / ".lrdmem-format").read_text(), "2\n")  # format guard ran

    def test_target_refuses_escapes_and_symlinks(self):
        evil = self.tmp / "evil"
        evil.mkdir()
        for rel in ("../x.md", "/etc/x.md", "a/../../x.md", ""):
            with self.subTest(rel), self.assertRaises(write.Refused):
                write.target(self.store, self.folder, rel)
        with self.assertRaises(write.Refused):
            write.target(self.store, self.tmp, "x.md")  # folder outside the store
        (self.folder / "contexts").symlink_to(evil)
        with self.assertRaises(write.Refused):
            write.target(self.store, self.folder, "contexts/x.md")
        (self.folder / "s.md").symlink_to(evil / "s.md")
        with self.assertRaises(write.Refused):
            write.target(self.store, self.folder, "s.md")
        (self.root / "acme/other").symlink_to(self.tmp)
        with self.assertRaises(write.Refused):
            write.target(self.store, self.root / "acme/other", "x.md")
        self.assertEqual(list(evil.iterdir()), [])

    def test_target_refuses_newer_store_format(self):
        (self.root / ".lrdmem-format").write_text("3\n")
        with self.assertRaises(StoreFormatError):
            write.target(self.store, self.folder, "x.md")

    # create / unique

    def test_create_exclusive_and_nofollow(self):
        p = self.folder / "n.md"
        self.assertTrue(write.create(p, "a"))
        self.assertFalse(write.create(p, "b"))
        self.assertEqual(p.read_text(), "a")
        (self.folder / "l.md").symlink_to(self.tmp / "target.md")
        self.assertFalse(write.create(self.folder / "l.md", "x"))  # O_EXCL: a (dangling) symlink counts as existing
        self.assertFalse((self.tmp / "target.md").exists())

    def test_unique_suffixes(self):
        stem = self.folder / "x"
        self.assertEqual(write.unique(stem), self.folder / "x.md")
        (self.folder / "x.md").write_text("")
        (self.folder / "x-2.md").write_text("")
        self.assertEqual(write.unique(stem), self.folder / "x-3.md")

    # set_fields

    def test_set_fields_edits_only_named_lines(self):
        self.assertTrue(write.set_fields(self.mem, {"state": "active", "description": "new d"}))
        self.assertEqual(self.mem.read_text(),
                         MEM.replace("state: candidate", "state: active").replace("description: d", "description: new d"))

    def test_set_fields_inserts_missing_key_under_metadata(self):
        self.assertTrue(write.set_fields(self.mem, {"valid_until": "2027-01-01"}))
        self.assertEqual(self.mem.read_text(), MEM.replace("2020-01-01\n", "2020-01-01\n  valid_until: 2027-01-01\n"))
        self.assertEqual(parse(self.mem.read_text())["metadata"]["valid_until"], "2027-01-01")

    def test_set_fields_adds_metadata_section_when_absent(self):
        p = self.folder / "bare.md"
        p.write_text("---\nname: bare\ndescription: d\n---\nb\n")
        self.assertTrue(write.set_fields(p, {"state": "obsolete"}))
        self.assertEqual(p.read_text(), "---\nname: bare\ndescription: d\nmetadata:\n  state: obsolete\n---\nb\n")

    def test_set_fields_top_level_for_edges_and_crlf(self):
        edge = self.folder / "edges/e.md"
        edge.parent.mkdir()
        original = "---\r\nid: e\r\nsource: a\r\nstate: proposed\r\n---\r\nbody\r\n"
        edge.write_bytes(original.encode())
        self.assertTrue(write.set_fields(edge, {"state": "validated", "superseded_by": "x"}))
        self.assertEqual(edge.read_bytes().decode(),
                         "---\r\nid: e\r\nsource: a\r\nstate: validated\r\nsuperseded_by: x\r\n---\r\nbody\r\n")

    def test_set_fields_quotes_odd_values(self):
        self.assertTrue(write.set_fields(self.mem, {"description": "[not a list]", "state": ""}))
        data = parse(self.mem.read_text())
        self.assertEqual((data["description"], data["metadata"]["state"]), ("[not a list]", ""))

    def test_set_fields_refuses_what_it_cannot_edit(self):
        for text in ("no frontmatter\n", "---\nunterminated\n"):
            self.mem.write_text(text)
            self.assertFalse(write.set_fields(self.mem, {"state": "active"}))
            self.assertEqual(self.mem.read_text(), text)
        self.mem.write_text(MEM)
        for key in ("triggers", "metadata"):  # a section, not a scalar
            self.assertFalse(write.set_fields(self.mem, {key: "x"}))
        listed = MEM.replace("description: d", "description: [a, b]")
        self.mem.write_text(listed)
        self.assertFalse(write.set_fields(self.mem, {"description": "x"}))
        self.assertFalse(write.set_fields(self.mem, {"bad key": "x"}))
        self.assertEqual(self.mem.read_text(), listed)

    def test_set_fields_never_writes_computed_fields(self):
        for key in ("confidence", "priority"):
            with self.assertRaises(write.Refused):
                write.set_fields(self.mem, {key: "0.9"})
        self.assertEqual(self.mem.read_text(), MEM)

    def test_set_fields_refuses_symlink(self):
        real = self.tmp / "real.md"
        real.write_text(MEM)
        (self.folder / "l.md").symlink_to(real)
        with self.assertRaises(OSError):
            write.set_fields(self.folder / "l.md", {"state": "active"})
        self.assertEqual(real.read_text(), MEM)

    # add_evidence

    def test_add_evidence_creates_then_appends_idempotently(self):
        a = Evidence("acme/web/experiences/x", "supports", 1.0)
        b = Evidence("feedback:y", "refutes", 0.5)
        self.assertTrue(write.add_evidence(self.mem, a))
        self.assertFalse(write.add_evidence(self.mem, a))
        self.assertTrue(write.add_evidence(self.mem, b))
        self.assertFalse(write.add_evidence(self.mem, Evidence(b.ref, b.kind, 0.1)))  # same ref+kind
        text = self.mem.read_text()
        self.assertEqual(text, MEM.replace(
            "---\nbody", "evidence: [{ref: acme/web/experiences/x, kind: supports, weight: 1.0}, "
                         "{ref: \"feedback:y\", kind: refutes, weight: 0.5}]\n---\nbody"))
        self.assertEqual([Evidence.from_dict(d) for d in parse(text)["evidence"]], [a, b])

    def test_add_evidence_refuses_block_list(self):
        block = MEM.replace("---\nbody", "evidence:\n  - {ref: x, kind: supports, weight: 1}\n---\nbody")
        self.mem.write_text(block)
        self.assertFalse(write.add_evidence(self.mem, Evidence("y", "supports", 1.0)))
        self.assertEqual(self.mem.read_text(), block)

    # index_line

    def test_index_line_appends_once(self):
        write.index_line(self.folder, "- [m](m.md) — d")
        write.index_line(self.folder, "- [m](m.md) — d")
        self.assertEqual((self.folder / "MEMORY.md").read_text(), "- [m](m.md) — d\n")
        with self.assertRaises(write.Refused):
            write.index_line(self.folder, "a\nb")

    # central secret guard

    def test_every_write_path_refuses_secrets(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with self.assertRaises(write.Refused):
            write.create(self.folder / "new.md", f"---\nname: new\n---\n{SECRET}\n")
        with self.assertRaises(write.Refused):
            write.set_fields(self.mem, {"description": SECRET})
        with self.assertRaises(write.Refused):
            write.add_evidence(self.mem, Evidence("https://x/?" + SECRET.replace(" ", ""), "supports", 1.0))
        with self.assertRaises(write.Refused):
            write.index_line(self.folder, f"- [m](m.md) — {SECRET}")
        self.assertTrue(issubclass(write.Refused, ValueError))
        self.assertFalse((self.folder / "new.md").exists())
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
