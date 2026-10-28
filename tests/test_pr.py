import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.frontmatter import parse
from lrdmem.pr import fetch_review_comments, learn_pr
from lrdmem.store import Store
from tests.fixtures import make_store

URL = "https://github.com/o/r/pull/12"
MEM = "---\nname: {0}\ndescription: {1}\nmetadata:\n  type: feedback\n---\n\nbody\n"


def comment(id, body, author="alice", path="a.py"):
    return {"id": id, "html_url": f"{URL}#discussion_r{id}", "path": path, "body": body, "user": {"login": author}}


def fake_run(inline, reviews=()):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        out = inline if args[:2] == ["gh", "api"] else {"reviews": list(reviews)}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(out), stderr="")
    run.calls = calls
    return run


class PrTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        tmp = Path(t.name)
        self.root = make_store(tmp, {
            "_global/MEMORY.md": "",
            "acme/_global/MEMORY.md": "",
            "acme/web/MEMORY.md": "",
            "acme/web/spaces.md": MEM.format("spaces", "Indent code with spaces never tabs"),
            "acme/other/MEMORY.md": "",
            "acme/other/tabs.md": MEM.format("tabs", "Indent code with spaces never tabs"),
        })
        self.repo = tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.store = Store(self.root)

    def test_fetch_parses_url_and_args(self):
        run = fake_run([comment(1, "hi"), comment(2, "bot", "ci[bot]")],
                       [{"author": {"login": "bob"}, "body": "review text", "url": URL + "#pullrequestreview-9"},
                        {"author": {"login": "bob"}, "body": "", "url": "x"}])
        for u in (URL, URL + "#discussion_r1", URL + "/files#diff"):
            run.calls.clear()
            out = fetch_review_comments(u, run)
            self.assertEqual(run.calls[0][0], ["gh", "api", "repos/o/r/pulls/12/comments", "--paginate"])
            self.assertEqual(run.calls[1][0], ["gh", "pr", "view", URL, "--json", "reviews"])
            self.assertTrue(all(not kw.get("shell") for _, kw in run.calls))
        self.assertEqual([c["id"] for c in out], [1, 2, None])
        self.assertEqual(out[0], {"id": 1, "url": URL + "#discussion_r1", "path": "a.py", "body": "hi", "author": "alice"})
        self.assertEqual(out[2]["author"], "bob")

    def test_bad_urls(self):
        for u in ("http://github.com/o/r/pull/1", "https://github.com/o/r/issues/1", "https://evil.com/o/r/pull/1",
                  "https://github.com/o/r/pull/x", "https://github.com/o/r/pull/1;rm", "https://github.com/o/r/pull/1?x=1"):
            with self.assertRaises(ValueError, msg=u):
                fetch_review_comments(u, fake_run([]))

    def test_linked_unmatched_bot_and_secret(self):
        run = fake_run([
            comment(1, "Please indent with spaces, tabs are wrong here.\nthanks"),
            comment(2, "Rename the variable to something clearer", path="b.py"),
            comment(3, "indent spaces tabs", "dependabot[bot]"),
            comment(4, "indent spaces tabs token = abcdef123456"),
        ])
        err = io.StringIO()
        with redirect_stderr(err):
            r = learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(r.linked, [(URL + "#discussion_r1", "acme/web/spaces.md")])
        self.assertEqual([c["id"] for c in r.unmatched], [2])
        self.assertIn(URL + "#discussion_r4", err.getvalue())
        ctxs = list((self.root / "acme/web/contexts").glob("*.md"))
        self.assertEqual(len(ctxs), 1)
        c = parse(ctxs[0].read_text())
        self.assertEqual((c["source"], c["pr"], c["files"], c["memories"]), ("pr", URL + "#discussion_r1", ["a.py"], ["spaces"]))
        text = ctxs[0].read_text()
        self.assertIn("PR review by alice", text)
        self.assertIn("**Mistake:** Please indent with spaces, tabs are wrong here. thanks", text)
        self.assertFalse((self.root / "acme/other/contexts").exists())

    def test_mistake_truncated(self):
        run = fake_run([comment(1, "indent spaces tabs " + "x" * 500)])
        r = learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(len(r.linked), 1)
        line = next(ln for ln in next((self.root / "acme/web/contexts").glob("*.md")).read_text().splitlines()
                    if ln.startswith("**Mistake:**"))
        self.assertEqual(len(line), len("**Mistake:** ") + 300)

    def test_scope_from_memory(self):
        (self.root / "acme/_global/envrule.md").write_text(MEM.format("envrule", "Always sanitize request payload fields"))
        (self.root / "_global/globalrule.md").write_text(MEM.format("globalrule", "Prefer immutable dataclasses everywhere"))
        run = fake_run([comment(1, "sanitize the request payload fields"), comment(2, "use immutable dataclasses")])
        r = learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(r.linked, [(URL + "#discussion_r1", "acme/_global/envrule.md"), (URL + "#discussion_r2", "_global/globalrule.md")])
        self.assertEqual(len(list((self.root / "acme/_global/contexts").glob("*.md"))), 1)
        self.assertEqual(len(list((self.root / "_global/contexts").glob("*.md"))), 1)

    def test_not_opted_in(self):
        other = self.repo.parent / "nope"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        with self.assertRaises(LookupError):
            learn_pr(self.store, other, URL, "claude", fake_run([]))

    def test_cli(self):
        run = fake_run([comment(1, "indent with spaces please"), comment(2, "Rename it\nmore")])
        out, err = io.StringIO(), io.StringIO()
        real, run_ = fetch_review_comments, run
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), \
                mock.patch("lrdmem.pr.fetch_review_comments", lambda u, run=None: real(u, run_)), \
                mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stdout(out), redirect_stderr(err):
            code = main(["learn-pr", URL, "--agent", "claude"])
            first = err.getvalue()
            err.truncate(0)
            err.seek(0)
            bad = main(["learn-pr", "https://example.com/x", "--agent", "c"])
        self.assertEqual(code, 0, first)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(f"linked {URL}#discussion_r1 -> acme/web/spaces.md"), lines)
        self.assertEqual(lines[1:], [URL + "#discussion_r2", "  a.py: Rename it"])
        self.assertEqual(bad, 2)
        self.assertIn("expected https://github.com", err.getvalue())


if __name__ == "__main__":
    unittest.main()
