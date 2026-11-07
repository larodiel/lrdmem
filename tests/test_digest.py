import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import digest
from lrdmem.cli import main
from lrdmem.store import Store
from tests.fixtures import make_store
from tests.test_lint import mem

DECISION = mem("due", meta="  kind: decision\n  revisit: 2020-01-01\n",
               body="**Why:** w\n**How to apply:** h\n**Alternatives rejected:** x\n")
STALE = mem("old", body="**Why:** w\n**How to apply:** h `/Volumes/nowhere-lrdmem/x`\n")
QUEUE = "---\nid: q1\nkind: merge\nsubjects: [work/app/due]\nstatus: open\n---\nbody\n"


class DigestTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        p = mock.patch.dict(os.environ, {"LRDMEM_CONFIG": str(self.tmp / "cfg" / "c.json")})
        p.start()
        self.addCleanup(p.stop)

    def store(self, **extra):
        files = {"_global/MEMORY.md": "", "work/_global/MEMORY.md": "", "work/app/MEMORY.md": "- [ok](ok.md) — x\n",
                 "work/app/ok.md": mem("ok")}
        files.update(extra)
        self.root = make_store(self.tmp, files)
        return Store(self.root)

    def test_a_clean_store_has_nothing_to_report(self):
        r = digest.build(self.store())
        self.assertEqual(r, {"lint": 0, "queue": 0, "stale": 0, "decisions_due": [], "never_injected": None})
        self.assertEqual(digest.lines(r), [])

    def test_everything_that_needs_a_human_is_counted_with_the_command_to_look(self):
        store = self.store(**{"work/app/MEMORY.md": "- [ok](ok.md) — x\n- [due](due.md) — d\n- [old](old.md) — o\n",
                              "work/app/due.md": DECISION, "work/app/old.md": STALE, "work/app/queue/q1.md": QUEUE,
                              "work/app/junk.md": "no frontmatter\n"})
        r = digest.build(store)
        self.assertEqual((r["queue"], r["stale"], r["decisions_due"]), (1, 1, ["work/app/due"]))
        self.assertGreaterEqual(r["lint"], 2)
        text = "\n".join(digest.lines(r))
        for needle in ("lrdmem lint", "lrdmem consolidate --queue", "lrdmem stale", "work/app/due"):
            self.assertIn(needle, text)

    def test_never_injected_counts_only_once_the_log_has_sessions(self):
        store = self.store()
        log = self.tmp / "cfg" / "recalls.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text('{"ts":"%s","event":"start","session":"s","env":"work","repo":"app","listed":1,"hidden":0}\n'
                       % __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(digest.build(store)["never_injected"], 0)  # ok has no triggers: index only, not counted

    def test_cli_exit_code_and_json(self):
        store = self.store(**{"work/app/queue/q1.md": QUEUE})
        env = {"LRDMEM_STORE": str(self.root)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(main(["digest"]), 1)
        self.assertIn("lrdmem consolidate --queue", out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(main(["digest", "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue())["queue"], 1)
        (self.root / "work/app/queue/q1.md").unlink()
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(main(["digest"]), 0)
        self.assertEqual(out.getvalue().strip(), "ok: nothing needs attention")


if __name__ == "__main__":
    unittest.main()
