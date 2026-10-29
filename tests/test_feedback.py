import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import cli, evidence, experience
from lrdmem.store import Memory, Store
from tests.fixtures import make_store

MEM = ("---\nname: {0}\ndescription: d\nmetadata:\n  type: feedback\n  state: active\n  author: me\n"
       "  updated: 2026-01-01\n---\n\nbody\n\n**Why:** w\n**How to apply:** h\n")


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {
            "_global/MEMORY.md": "", "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": "",
            "acme/web/rule.md": MEM.format("rule"), "acme/_global/envrule.md": MEM.format("envrule"),
            "other/_global/MEMORY.md": "", "other/_global/far.md": MEM.format("far")})
        self.repo = self.tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.store = Store(self.root)

    def fb(self, ref="acme/web/rule", verdict="useful", **kw):
        return experience.feedback(self.store, self.repo, ref, verdict, agent=kw.pop("agent", "claude"), **kw)

    def mem(self, ref="acme/web/rule"):
        node = Store(self.root).node(ref)
        assert isinstance(node, Memory)
        return node

    def test_incorrect_refutes_and_keeps_state(self):
        before = self.mem()
        e = self.fb(verdict="incorrect", note="wrong on Windows")
        m = self.mem()
        self.assertEqual(m.state, before.state)
        self.assertEqual([(i.ref, i.kind, i.weight) for i in m.evidence], [(e.ref, "refutes", 0.5)])
        self.assertEqual((e.kind, e.data["verdict"], e.data["source"], e.data["ref"]), ("feedback", "incorrect", "chat", "acme/web/rule"))
        self.assertEqual(len(list((self.root / "acme/web/revisions").glob("*.md"))), 1)

    def test_useful_supports(self):
        e = self.fb()
        self.assertEqual([(i.kind, i.weight) for i in self.mem().evidence], [("supports", 0.5)])
        self.assertIn("feedback: useful on acme/web/rule", e.path.read_text())

    def test_irrelevant_lowers_priority_only(self):
        base = evidence.priority(self.store, self.mem())
        e = self.fb(verdict="irrelevant")
        m = self.mem()
        self.assertEqual([(i.kind, i.weight) for i in m.evidence], [("observed_in", 0.0)])
        self.assertLess(evidence.priority(Store(self.root), m), base)
        self.assertEqual(m.state, "active")
        self.assertEqual(e.data["verdict"], "irrelevant")

    def test_idempotent_same_day(self):
        a = self.fb()
        b = self.fb()
        self.assertEqual(a.ref, b.ref)
        self.assertEqual(len(self.mem().evidence), 1)
        self.assertEqual(len(list(self.root.rglob("experiences/*.md"))), 1)
        self.assertEqual(len(list((self.root / "acme/web/revisions").glob("*.md"))), 1)

    def test_env_scope_memory(self):
        e = self.fb("acme/_global/envrule")
        self.assertEqual(e.path.parent, self.root / "acme/_global/experiences")

    def test_refusals(self):
        for ref, verdict in (("acme/web/nope", "useful"), ("other/far", "useful"), ("acme/web/rule", "meh")):
            with self.assertRaises(experience.Refused):
                self.fb(ref, verdict)
        self.assertEqual(list(self.root.rglob("experiences/*.md")), [])

    def test_verdict_only_on_feedback(self):
        with self.assertRaises(experience.Refused):
            experience.add(self.store, self.repo, kind="decision", agent="a", text="x y z", verdict="useful")
        with self.assertRaises(experience.Refused):
            experience.add(self.store, self.repo, kind="feedback", agent="a", text="x y z", verdict="bogus")

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"LRDMEM_STORE": str(self.root), "LRDMEM_CONFIG": str(self.tmp / "cfg.json")}
        with mock.patch.dict(os.environ, env), mock.patch("pathlib.Path.cwd", return_value=self.repo), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["feedback", *argv])
        return code, out.getvalue().strip(), err.getvalue()

    def test_cli(self):
        code, out, _ = self.run_cli(["acme/web/rule", "useful", "--note", "saved me"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^acme/web/experiences/\d{4}-\d\d-\d\d-req-[0-9a-f]{16}$")
        code, _, err = self.run_cli(["acme/web/nope", "useful"])
        self.assertEqual((code, bool(err)), (1, True))


if __name__ == "__main__":
    unittest.main()
