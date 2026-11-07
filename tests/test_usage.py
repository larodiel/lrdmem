import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lrdmem import config, usage
from lrdmem.cli import main
from tests.test_hooks import STYLE, _Base

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def line(event, ts, **kw):
    return json.dumps({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **kw}) + "\n"


class UsageTest(_Base):
    def log(self) -> Path:
        return config._path().parent / "recalls.jsonl"

    def entries(self):
        return [json.loads(x) for x in self.log().read_text().splitlines()]

    def test_record_appends_one_json_line_and_never_raises(self):
        usage.record("inject", session="s1", env="work", repo="app", refs=["work/app/a"])
        usage.record("inject", session="s1", env="work", repo="app", refs=["work/app/b"])
        got = self.entries()
        self.assertEqual([e["refs"] for e in got], [["work/app/a"], ["work/app/b"]])
        self.assertEqual((got[0]["event"], got[0]["session"], got[0]["repo"]), ("inject", "s1", "app"))
        with mock.patch.dict(os.environ, {"LRDMEM_CONFIG": "/proc/none/x/c.json"}):
            usage.record("inject", session="s", env="w", repo="r", refs=["x"])  # unwritable: silent

    def test_record_trims_the_oldest_half_past_the_cap(self):
        for n in range(40):
            usage.record("inject", session="s", env="w", repo="r", refs=[f"w/r/m{n}"], cap=1000)
        refs = [e["refs"][0] for e in self.entries()]
        self.assertLess(self.log().stat().st_size, 1200)
        self.assertEqual(refs[-1], "w/r/m39")
        self.assertNotIn("w/r/m0", refs)

    def test_hooks_log_session_start_and_injection(self):
        self.store()
        self.hook("session-start", self.payload("session_start"))
        self.hook("pre-tool", self.payload("pre_tool_edit"))
        start, inject = self.entries()
        self.assertEqual((start["event"], start["repo"], start["env"]), ("start", "app", "work"))
        self.assertEqual(start["listed"], 1)
        self.assertEqual((inject["event"], inject["refs"]), ("inject", ["work/app/php-style"]))

    def test_session_start_logs_how_many_lines_scope_hid(self):
        scoped = STYLE.replace("triggers:\n", "triggers:\n  repos: [other]\n").replace("php-style", "o")
        self.store({"work/_global/MEMORY.md": "- [O](o.md) — x\n", "work/_global/o.md": scoped})
        self.hook("session-start", self.payload("session_start"))
        start = self.entries()[0]
        self.assertEqual((start["listed"], start["hidden"]), (1, 1))

    def test_report_counts_windows_and_splits_the_unused(self):
        store = self.store({"work/app/js.md": STYLE.replace("php-style", "js"),
                            "work/app/notes.md": STYLE.replace("php-style", "notes").replace(
                                "triggers:\n  paths: ['**/*.php']\n  commands: [composer]\n", "")})
        old, new = NOW - timedelta(days=40), NOW - timedelta(days=2)
        self.log().parent.mkdir(parents=True, exist_ok=True)
        self.log().write_text(line("start", new, session="s", env="work", repo="app", listed=3, hidden=1)
                              + line("inject", new, session="s", env="work", repo="app", refs=["work/app/php-style"])
                              + line("inject", new, session="t", env="work", repo="app", refs=["work/app/php-style"])
                              + line("inject", old, session="u", env="work", repo="app", refs=["work/app/js"])
                              + "not json\n")
        r = usage.report(store, days=30, now=NOW)
        self.assertEqual(r["sessions"], {"app": 1})
        self.assertEqual(r["injected"], [("work/app/php-style", 2, "2026-11-05")])
        self.assertEqual(r["unused_triggered"], ["work/app/js"])
        self.assertEqual(r["index_only"], ["work/app/notes"])

    def test_cli_usage_prints_the_report(self):
        self.store()
        self.log().parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc)
        self.log().write_text(line("inject", ts, session="s", env="work", repo="app", refs=["work/app/php-style"]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["usage"]), 0)
        self.assertIn("work/app/php-style  1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["usage", "--json", "--days", "7"]), 0)
        self.assertEqual(json.loads(out.getvalue())["injected"][0][:2], ["work/app/php-style", 1])


if __name__ == "__main__":
    unittest.main()
