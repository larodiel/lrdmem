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

URL = "https://github.com/o/web/pull/12"
MEM = "---\nname: {0}\ndescription: {1}\nmetadata:\n  type: feedback\n---\n\nbody\n"


def comment(id, body, author="alice", path="a.py", assoc="MEMBER"):
    return {"id": id, "html_url": f"{URL}#discussion_r{id}", "path": path, "body": body, "user": {"login": author},
            "author_association": assoc}


def fake_run(inline, reviews=()):
    """inline/reviews: a list (one page) or a list of lists wrapped in Pages (several pages, back to back)."""
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        data = inline if args[2].endswith("/comments") else reviews
        pages = data.pages if isinstance(data, Pages) else [data]
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(json.dumps(list(p)) for p in pages), stderr="")
    run.calls = calls
    return run


class Pages:
    def __init__(self, *pages):
        self.pages = pages


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
            "acme/other/aaa.md": MEM.format("aaa", "Indent code with spaces never tabs"),
        })
        self.repo = tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.remote("origin", "https://github.com/o/web.git")
        self.store = Store(self.root)

    def remote(self, name, url):
        subprocess.run(["git", "remote", "add", name, url], cwd=self.repo, check=True)

    def test_fetch_parses_url_and_args(self):
        run = fake_run([comment(1, "hi"), comment(2, "bot", "ci[bot]")],
                       [{"id": 9, "user": {"login": "bob"}, "body": "review text", "html_url": URL + "#pullrequestreview-9",
                          "author_association": "OWNER"},
                         {"id": 8, "user": {"login": "bob"}, "body": "", "html_url": "x", "author_association": "OWNER"}])
        for u in (URL, URL + "#discussion_r1", URL + "/files#diff"):
            run.calls.clear()
            out = fetch_review_comments(u, run)
            self.assertEqual(run.calls[0][0], ["gh", "api", "repos/o/web/pulls/12/comments", "--paginate"])
            self.assertEqual(run.calls[1][0], ["gh", "api", "repos/o/web/pulls/12/reviews", "--paginate"])
            self.assertTrue(all(not kw.get("shell") for _, kw in run.calls))
        self.assertEqual([c["id"] for c in out], [1, 2, 9])
        self.assertEqual(out[0], {"id": 1, "url": URL + "#discussion_r1", "path": "a.py", "body": "hi", "author": "alice",
                               "association": "MEMBER"})
        self.assertEqual(out[2]["author"], "bob")

    def test_bad_urls(self):
        for u in ("http://github.com/o/web/pull/1", "https://github.com/o/web/issues/1", "https://evil.com/o/r/pull/1",
                  "https://github.com/o/web/pull/x", "https://github.com/o/web/pull/1;rm", "https://github.com/../../pull/1", "https://github.com/././/pull/1", "https://github.com/o/web/pull/1?x=1"):
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
        self.assertFalse((self.root / "acme/other/contexts").exists())  # aaa matches equally well but is another repo's

    def test_tie_first_by_slug(self):
        (self.root / "acme/web/alpha-rule.md").write_text(MEM.format("alpha-rule", "Indent code with spaces never tabs"))
        r = learn_pr(self.store, self.repo, URL, "claude", fake_run([comment(1, "indent code with spaces")]))
        self.assertEqual(r.linked, [(URL + "#discussion_r1", "acme/web/alpha-rule.md")])

    def test_untrusted_authors_not_stored(self):
        body = "indent with spaces, tabs are wrong"
        run = fake_run([comment(i, body, assoc=a) for i, a in enumerate(("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", None), 1)],
                       [{"id": 7, "user": {"login": "eve"}, "body": body, "html_url": URL + "#r7", "author_association": "NONE"}])
        r = learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(r.linked, [])
        self.assertEqual([(c["id"], c["note"]) for c in r.unmatched],
                         [(1, "CONTRIBUTOR"), (2, "NONE"), (3, "FIRST_TIME_CONTRIBUTOR"), (4, "none"), (7, "NONE")])
        self.assertFalse((self.root / "acme/web/contexts").exists())

    def test_other_repo_pr_refused(self):
        for u in (URL.replace("/web/", "/other/"), URL.replace("/web/", "/WEB/")):
            run = fake_run([comment(1, "indent with spaces")])
            if "WEB" in u:
                self.assertEqual(len(learn_pr(self.store, self.repo, u, "claude", run).linked), 1)
            else:
                with self.assertRaises(LookupError):
                    learn_pr(self.store, self.repo, u, "claude", run)
        self.assertEqual(len(list((self.root / "acme/web/contexts").glob("*.md"))), 1)

    def test_remote_forms_accepted(self):
        for url in ("git@github.com:O/Web.git", "ssh://git@github.com/o/web", "https://github.com/o/web"):
            subprocess.run(["git", "remote", "set-url", "origin", url], cwd=self.repo, check=True)
            r = learn_pr(self.store, self.repo, URL, "claude", fake_run([comment(1, "Rename it")]))
            self.assertEqual(len(r.unmatched), 1, url)

    def test_same_name_other_owner_refused_before_gh(self):
        run = fake_run([comment(1, "indent with spaces")])
        with self.assertRaises(LookupError):
            learn_pr(self.store, self.repo, URL.replace("/o/", "/evil/"), "claude", run)
        self.assertEqual(run.calls, [])
        self.assertFalse((self.root / "acme/web/contexts").exists())

    def test_other_remote_of_checkout_accepted_non_github_ignored(self):
        subprocess.run(["git", "remote", "set-url", "origin", "https://gitlab.com/evil/web.git"], cwd=self.repo, check=True)
        self.remote("upstream", "git@github.com:up/web.git")
        run = fake_run([comment(1, "Rename it")])
        learn_pr(self.store, self.repo, URL.replace("/o/", "/up/"), "claude", run)
        self.assertTrue(run.calls)
        run = fake_run([])
        with self.assertRaises(LookupError):
            learn_pr(self.store, self.repo, URL, "claude", run)  # o/web only matched the gitlab remote
        self.assertEqual(run.calls, [])

    def test_no_github_remote_refused(self):
        subprocess.run(["git", "remote", "remove", "origin"], cwd=self.repo, check=True)
        run = fake_run([comment(1, "x")])
        with self.assertRaisesRegex(LookupError, "GitHub remote"):
            learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(run.calls, [])

    def test_multi_page(self):
        run = fake_run(Pages([comment(1, "indent with spaces")], [comment(2, "Rename it")]))
        r = learn_pr(self.store, self.repo, URL, "claude", run)
        self.assertEqual(len(r.linked), 1)
        self.assertEqual([c["id"] for c in r.unmatched], [2])

    def test_garbage_output_is_runtime_error(self):
        run = lambda a, **kw: subprocess.CompletedProcess(a, 0, stdout="not json", stderr="")
        with self.assertRaises(RuntimeError):
            fetch_review_comments(URL, run)

    def test_unexpected_learn_error_skipped(self):
        with mock.patch("lrdmem.pr.learn", side_effect=OSError("disk")), redirect_stderr(io.StringIO()) as err:
            r = learn_pr(self.store, self.repo, URL, "claude", fake_run([comment(1, "indent with spaces")]))
        self.assertEqual(r.linked, [])
        self.assertIn("discussion_r1", err.getvalue())

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
        self.assertEqual(err.getvalue().count("expected"), 1)
        self.assertIn("expected https://github.com", err.getvalue())

    def test_cli_errors(self):
        def failing(a, **kw):
            raise subprocess.CalledProcessError(1, a, stderr="gh: not logged in")
        real = fetch_review_comments
        for label, runner, want, rc in (("gh", failing, "gh: not logged in", 1),
                                        ("json", lambda a, **kw: subprocess.CompletedProcess(a, 0, stdout="x", stderr=""), "could not parse gh output", 1)):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), \
                    mock.patch("lrdmem.pr.fetch_review_comments", lambda u, run=None, r=runner: real(u, r)), \
                    mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stderr(err):
                self.assertEqual(main(["learn-pr", URL, "--agent", "c"]), rc, label)
            self.assertIn(want, err.getvalue())

    def test_cli_empty_path_and_note(self):
        run = fake_run([], [{"id": 7, "user": {"login": "e"}, "body": "hello\nmore", "html_url": URL + "#r7",
                             "author_association": "NONE"}])
        out = io.StringIO()
        real = fetch_review_comments
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root)}), \
                mock.patch("lrdmem.pr.fetch_review_comments", lambda u, run=None: real(u, run_)), \
                mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stdout(out):
            run_ = run
            main(["learn-pr", URL, "--agent", "c"])
        self.assertEqual(out.getvalue().splitlines(), [URL + "#r7", "  hello (not stored: NONE)"])


if __name__ == "__main__":
    unittest.main()
