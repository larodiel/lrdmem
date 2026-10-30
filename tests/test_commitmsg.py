import re
import tempfile
import unittest
from pathlib import Path

from lrdmem.commitmsg import build

SUBJECT = re.compile(r"^(feat|refactor|revert|chore)\(memory\): [a-z0-9].{0,60}$")


def rev(root: Path, rel: str, action: str, after: str = "") -> tuple[str, str]:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nid: {p.stem}\ntarget: acme/web/x\naction: {action}\nafter:\n  {after}\n---\n\nwhy\n")
    return "A", rel


class BuildTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)

    def subject(self, msg: str) -> str:
        first = msg.splitlines()[0]
        self.assertRegex(first, SUBJECT)
        self.assertLessEqual(len(first), 72)
        return first

    def test_revert_wins(self):
        changed = [rev(self.root, "acme/web/revisions/2026-10-30-rabc12345.md", "revert", "state: active"),
                   ("M", "acme/web/x.md")]
        self.assertEqual(self.subject(build(changed, "mac", "acme", self.root)),
                         "revert(memory): 2026-10-30-rabc12345")

    def test_consolidation_counts(self):
        changed = [rev(self.root, "acme/web/revisions/r1.md", "merge", "evidence: acme/web/y"),
                   rev(self.root, "acme/web/revisions/r2.md", "state_change", "state: active"),
                   rev(self.root, "acme/web/revisions/r3.md", "state_change", "state: active"),
                   rev(self.root, "acme/web/revisions/r4.md", "state_change", "state: contested"),
                   rev(self.root, "acme/web/revisions/r5.md", "supersede", "state: superseded"),
                   ("M", "acme/web/x.md"), ("A", "acme/web/queue/q-1.md")]
        msg = build(changed, "mac", "acme", self.root)
        self.assertEqual(self.subject(msg), "refactor(memory): consolidate — merge 1, promote 2, contest 1")
        self.assertIn("acme/web/x.md", msg)

    def test_learning(self):
        changed = [("A", "acme/web/rule.md"), ("A", "acme/web/other.md"), ("A", "acme/web/experiences/e1.md"),
                   ("A", "acme/web/edges/2026-10-30-e1.md"), ("M", "acme/web/MEMORY.md"),
                   rev(self.root, "acme/web/revisions/c1.md", "create", "state: active")]
        msg = build(changed, "mac", "acme", self.root)
        self.assertEqual(self.subject(msg), "feat(memory): learn 2 memories, 1 experiences")
        self.assertIn("1 edge", msg)

    def test_chore_and_body_cap(self):
        changed = [("M", f"acme/web/m{i}.md") for i in range(40)]
        msg = build(changed, "Victor's Mac", "acme", self.root)
        self.assertEqual(self.subject(msg), "chore(memory): sync acme from victor's mac")
        self.assertLessEqual(len(msg.splitlines()), 2 + 21)

    def test_long_labels_stay_short(self):
        msg = build([("M", "x.md")], "d" * 200, "E" * 200, self.root)
        self.subject(msg)


if __name__ == "__main__":
    unittest.main()
