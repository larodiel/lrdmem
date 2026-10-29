import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.lint import has_secret, lint, printable
from lrdmem.store import Store
from tests.fixtures import make_store


def mem(name="a", extra="", meta="", body="**Why:** w\n**How to apply:** h\n", fm=""):
    return (f"---\nname: {name}\ndescription: d\n{fm}metadata:\n  type: feedback\n  author: me\n"
            f"  updated: 2026-01-01\n{meta}{extra}---\n{body}")


def index(*names):
    return "".join(f"- [{n}]({n}.md) — hook\n" for n in names)


CTX = "---\nid: c1\ndate: 2026-01-02\nsource: chat\nmemories: [a]\n---\nbody\n"


def clean(**over):
    files = {
        "_global/MEMORY.md": index("g"), "_global/g.md": mem("g"),
        "work/_global/MEMORY.md": "", "work/app/MEMORY.md": index("a"),
        "work/app/a.md": mem("a", body="**Why:** w\n**How to apply:** h [[g]]\n"),
        "work/app/contexts/2026-01-02-x.md": CTX,
    }
    files.update(over)
    return files


class LintTest(unittest.TestCase):
    def run_lint(self, files, env=None):
        with tempfile.TemporaryDirectory() as t:
            return lint(Store(make_store(Path(t), files)), env)


    def test_unsupported_frontmatter_syntax(self):
        msg = "unsupported frontmatter syntax; use inline lists [a, b]"
        body = "**Why:** w\n**How to apply:** h [[g]]\n"
        out = self.run_lint(clean(**{"work/app/a.md": mem(
            "a", extra="triggers:\n  paths:\n    - '*.php'\n# note\nsupersedes: |\n  old\n", body=body)}))
        self.assertEqual(out, [f"work/app/a.md:{n}: {msg}" for n in (10, 11, 12, 13)])
    def has(self, files, text):
        out = self.run_lint(files)
        self.assertTrue(any(text in p for p in out), out)

    def test_clean(self):
        self.assertEqual(self.run_lint(clean()), [])

    def test_index_problems(self):
        self.has(clean(**{"work/app/MEMORY.md": "junk\n"}), "work/app/MEMORY.md:1:")
        self.has(clean(**{"work/app/MEMORY.md": index("a", "zz")}), "points to missing zz.md")
        self.has(clean(**{"work/app/MEMORY.md": ""}), "work/app/a.md: not listed")
        files = clean()
        del files["work/app/MEMORY.md"]
        self.has(files, "missing MEMORY.md")

    def test_link_to_other_repo_same_env_resolves(self):
        files = clean(**{"work/other/MEMORY.md": index("o"), "work/other/o.md": mem("o"),
                         "work/app/a.md": mem(body="**Why:** w\n**How to apply:** h [[o]]\n")})
        self.assertEqual(self.run_lint(files), [])

    def test_nothing_checked_is_a_problem(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), clean())
            self.assertEqual(lint(Store(root), "ghost"), ["ghost: no such environment"])
            self.assertEqual(lint(Store(Path(t) / "nope")), [f"{Path(t) / 'nope'}: store not found"])

    def test_vanishing_file_is_a_problem(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), clean())
            real = Path.read_text
            def flaky(self, *a, **k):
                if self.name == "a.md":
                    raise FileNotFoundError(self)
                return real(self, *a, **k)
            with mock.patch.object(Path, "read_text", flaky):
                out = lint(Store(root))
        self.assertTrue(any("a.md: unreadable" in p for p in out), out)

    def test_printable(self):
        self.assertEqual(printable("a\x1b[31m\x7f\x85b\té"), "a?[31m??b?é")

    def test_cli_strips_escapes(self):
        files = clean(**{"work/app/a.md": mem(body="**Why:** w\n**How to apply:** [[\x1b]0;x]]\n")})
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), files)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(root)}), redirect_stdout(out), redirect_stderr(io.StringIO()):
                main(["lint"])
        self.assertNotIn("\x1b", out.getvalue())

    def test_memory_fields(self):
        self.has(clean(**{"work/app/a.md": mem("zz")}), "name is not 'a'")
        self.has(clean(**{"work/app/a.md": mem().replace("description: d", "description:")}), "no description")
        self.has(clean(**{"work/app/a.md": mem().replace("feedback", "bogus")}), "metadata.type")
        self.has(clean(**{"work/app/a.md": mem().replace("  author: me\n", "")}), "metadata.author")
        self.has(clean(**{"work/app/a.md": mem().replace("2026-01-01", "soon")}), "metadata.updated")
        self.has(clean(**{"work/app/a.md": mem(body="nothing\n")}), "**Why:**")

    def test_kind_and_check(self):
        self.has(clean(**{"work/app/a.md": mem(meta="  kind: weird\n")}), "metadata.kind")
        chk = "check:\n  run: ls\n  on: stop\n"
        self.has(clean(**{"work/app/a.md": mem(meta="  kind: pattern\n", fm="", extra="").replace("---\nname", "---\n" + chk + "name", 1)}),
                 "only allowed on kind: obligation")
        ok = mem(meta="  kind: obligation\n").replace("---\nname", "---\n" + chk + "name", 1)
        self.assertEqual(self.run_lint(clean(**{"work/app/a.md": ok})), [])
        bad = ok.replace("run: ls", "run:").replace("on: stop", "on: never")
        out = self.run_lint(clean(**{"work/app/a.md": bad}))
        self.assertTrue(any("check.run" in p for p in out) and any("check.on" in p for p in out), out)

    def test_context_rules(self):
        c = "work/app/contexts/2026-01-02-x.md"
        self.has(clean(**{c: CTX.replace("id: c1\n", "")}), "no id")
        self.has(clean(**{c: CTX.replace("2026-01-02", "x")}), "date is not")
        self.has(clean(**{c: CTX.replace("chat", "email")}), "source must be")
        self.has(clean(**{c: CTX.replace("[a]", "[]")}), "memories is empty")
        self.has(clean(**{c: CTX.replace("[a]", "[ghost]")}), "'ghost' does not exist")

    def test_links_scope(self):
        files = clean(**{"work/app/a.md": mem(body="**Why:** w\n**How to apply:** h [[x]]\n"),
                         "home/app/MEMORY.md": index("x"), "home/_global/MEMORY.md": "",
                         "home/app/x.md": mem("x")})
        self.has(files, "[[x]] does not resolve")
        files = clean(**{"_global/g.md": mem("g", body="**Why:** w\n**How to apply:** h [[a]]\n")})
        self.has(files, "[[a]] does not resolve")

    def test_secret_and_path(self):
        self.has(clean(**{"work/app/a.md": mem(body="**Why:** w\n**How to apply:** password: hunter22x\n")}), "credential")
        self.has(clean(**{"work/app/a.md": mem(body="**Why:** w\n**How to apply:** `/Volumes/nope/zz`\n")}), "no longer exists")
        self.assertTrue(has_secret("token = abcdefgh"))
        self.assertFalse(has_secret("hello"))

    def test_v2_secret_patterns(self):
        seg = "A1b2C3d4E5f6G7h8"  # built at runtime so the repo itself holds no token-shaped strings
        hits = {
            "jwt": "eyJ" + seg + ".eyJ" + seg + "." + seg,
            "stripe live": "sk_" + "live_" + seg + "x",
            "stripe test": "sk_" + "test_" + seg,
            "stripe restricted": "rk_" + "live_" + seg,
            "npm": "npm_" + "a" * 36,
            "slack webhook": "https://hooks.slack.com/services/" + "T000/B000/" + seg,
            "google oauth": "ya29." + seg + "-_xyz",
            "bearer": "Authorization: Bearer " + seg,
            "bearer lower": "authorization:bearer " + seg + ".x",
        }
        for name, text in hits.items():
            with self.subTest(name):
                self.assertTrue(has_secret(f"see {text} here"))
        misses = {
            "stripe publishable": "pk_" + "live_" + seg,
            "stripe short": "sk_" + "live_" + "abc",
            "eyJ alone": "the eyJ prefix marks base64 JSON",
            "one jwt segment": "eyJ" + seg,
            "npm short": "npm_" + "a" * 20,
            "slack docs": "https://hooks.slack.com/services/",
            "ya29 short": "ya29.abc",
            "bearer word": "Authorization: Bearer token",
            "bearer prose": "use a Bearer token in the Authorization header",
        }
        for name, text in misses.items():
            with self.subTest(name):
                self.assertFalse(has_secret(text))

    def test_broken_files_reported_and_lint_continues(self):
        files = clean(**{"work/app/b.md": "no frontmatter\n", "work/app/c.md": "---\nunterminated\n",
                         "work/app/a.md": mem("zz")})
        files["work/app/MEMORY.md"] = index("a", "b", "c")
        out = self.run_lint(files)
        self.assertTrue(any("b.md: no frontmatter" in p for p in out), out)
        self.assertTrue(any("c.md: no frontmatter" in p for p in out), out)
        self.assertTrue(any("name is not 'a'" in p for p in out), out)

    def test_undecodable_file(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), clean())
            (root / "work/app/bad.md").write_bytes(b"\xff\xfe")
            (root / "work/app/MEMORY.md").write_text(index("a", "bad"))
            out = lint(Store(root))
        self.assertTrue(any("bad.md: unreadable" in p for p in out), out)

    def test_env_filter(self):
        files = clean(**{"home/app/MEMORY.md": "junk\n", "home/_global/MEMORY.md": ""})
        self.assertEqual([p for p in self.run_lint(files, "work")], [])
        self.assertTrue(self.run_lint(files, "home"))

    def test_cli(self):
        for files, code, err in ((clean(), 0, "ok"), (clean(**{"work/app/MEMORY.md": "junk\n"}), 1, "2 problem(s)")):
            with tempfile.TemporaryDirectory() as t:
                root = make_store(Path(t), files)
                out, e = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(root)}), redirect_stdout(out), redirect_stderr(e):
                    self.assertEqual(main(["lint"]), code)
            self.assertEqual(e.getvalue().strip(), err)
            self.assertEqual(bool(out.getvalue()), code == 1)


def _exp(**over):
    f = {"id": "2026-10-29-x", "date": "2026-10-29T14:03:00Z", "kind": "error", "source": "run"}
    f.update(over)
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\nobserved\n"


EXP = "work/app/experiences/2026-10-29-x.md"
EDGE = "work/app/edges/e1.md"


def _edge(**over):
    f = {"id": "e1", "source": "work/app/a", "target": "work/app/experiences/2026-10-29-x", "type": "supports",
         "state": "proposed"}
    f.update(over)
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\n"


def v2files(**over):
    files = clean()
    files.update({EXP: _exp(), EDGE: _edge(), ".lrdmem-format": "2\n"})
    files.update(over)
    return files


class LintV2Test(unittest.TestCase):
    run_lint = LintTest.run_lint
    has = LintTest.has

    def test_clean_v2(self):
        self.assertEqual(self.run_lint(v2files()), [])

    def test_v2_memory_fields(self):
        meta = "  memory_type: nope\n  state: bad\n  valid_from: x\n  valid_until: y\n  created: z\n"
        out = self.run_lint(v2files(**{"work/app/a.md": mem(meta=meta, body="**Why:** w\n**How to apply:** h\n")}))
        for msg in ("memory_type", "state", "valid_from", "valid_until", "created"):
            self.assertTrue(any(f"metadata.{msg}" in p for p in out), (msg, out))

    def test_memory_evidence(self):
        ev = "evidence: [{ref: work/app/ghost, kind: supports, weight: 1}, {ref: x, kind: bad, weight: 1}, {ref: feedback:f1, kind: refutes, weight: 1}, {ref: work/app/experiences/2026-10-29-x, kind: supports, weight: 1}]\n"
        out = self.run_lint(v2files(**{"work/app/a.md": mem(extra=ev)}))
        self.assertEqual(len(out), 2, out)
        self.assertTrue(any("evidence ref 'work/app/ghost' does not resolve" in p for p in out), out)
        self.assertTrue(any("invalid evidence" in p for p in out), out)

    def test_experience_problems(self):
        self.has(v2files(**{EXP: _exp(kind="bogus")}), "kind must be one of")
        self.has(v2files(**{EXP: _exp(source="bogus")}), "source must be one of")
        self.has(v2files(**{EXP: _exp(date="nope")}), "date is not")
        self.assertEqual(self.run_lint(v2files(**{EXP: _exp(date="2026-10-29")})), [])

    def test_edge_problems(self):
        self.has(v2files(**{EDGE: _edge(type="likes")}), "type must be one of")
        self.has(v2files(**{EDGE: _edge(state="x")}), "state must be one of")
        self.has(v2files(**{EDGE: _edge(target="work/app/ghost")}), "target 'work/app/ghost' does not exist")
        self.has(v2files(**{EDGE: _edge(source="")}), "source")
        self.has(v2files(**{EDGE: _edge(weight="heavy")}), "weight")
        self.has(v2files(**{EDGE: _edge(evidence="[{ref: a, kind: zz, weight: 1}]")}), "invalid evidence")

    def test_edge_endpoint_outside_scope(self):
        files = v2files(**{"other/app/MEMORY.md": index("o"), "other/app/o.md": mem("o"), EDGE: _edge(target="other/app/o")})
        self.has(files, "does not exist")

    def test_revision_and_queue_shape(self):
        rev = "work/app/revisions/2026-10-29-r1.md"
        good = ("---\nid: 2026-10-29-r1\ntarget: work/app/a\naction: state_change\nbefore:\n  state: candidate\n"
                "after:\n  state: active\nreason: r\nby: consolidate\ndate: 2026-10-29\n---\n")
        self.assertEqual(self.run_lint(v2files(**{rev: good})), [])
        self.has(v2files(**{rev: good.replace("state_change", "zap")}), "action must be one of")
        self.has(v2files(**{rev: good.replace("work/app/a", "work/app/ghost")}), "target 'work/app/ghost' does not exist")
        self.has(v2files(**{rev: good.replace("by: consolidate\n", "")}), "no by")
        self.has(v2files(**{rev: good.replace("date: 2026-10-29", "date: no")}), "date is not")
        q = "work/app/queue/q1.md"
        qgood = "---\nid: q1\nkind: merge\nsubjects: [work/app/a]\nstatus: open\n---\n"
        self.assertEqual(self.run_lint(v2files(**{q: qgood})), [])
        self.has(v2files(**{q: qgood.replace("open", "weird")}), "status must be one of")
        self.has(v2files(**{q: qgood.replace("[work/app/a]", "[]")}), "subjects is empty")
        self.has(v2files(**{q: qgood.replace("kind: merge\n", "")}), "no kind")
        self.has(v2files(**{q: qgood.replace("open", "resolved")}), "resolved without a resolution")

    def test_secrets_in_all_entities(self):
        key = "ghp_" + "a" * 36
        for path, text in ((EXP, _exp().replace("observed", key)), (EDGE, _edge() + key + "\n"),
                           ("work/app/revisions/r.md", f"---\nid: r\nafter:\n  x: {key}\n---\n"),
                           ("work/app/queue/q.md", f"---\nid: q\n---\n{key}\n")):
            self.has(v2files(**{path: text}), "looks like it holds a credential")

    def test_escaped_secret_in_frontmatter_value(self):
        pw = "pass" + "word\\n=hunter22xyz"  # as dump writes it: a JSON-escaped newline
        for path, text in ((EXP, _exp(note=f'"{pw}"')),
                           ("work/app/revisions/r.md", f"---\nid: r\nafter:\n  x: \"{pw}\"\n---\n"),
                           ("work/app/a.md", mem(extra=f"evidence: [{{ref: \"x:{pw}\", kind: supports, weight: 1}}]\n"))):
            with self.subTest(path):
                self.has(v2files(**{path: text}), "looks like it holds a credential")

    def test_marker_missing_warns_only_with_v2_data(self):
        files = v2files()
        del files[".lrdmem-format"]
        self.has(files, ".lrdmem-format: missing")
        files = clean()
        self.assertEqual(self.run_lint(files), [])
        files[".lrdmem-format"] = "3\n"
        self.has(files, ".lrdmem-format")
        files = clean(**{"work/app/queue/q.md": "---\nid: q\nkind: k\nsubjects: [work/app/a]\nstatus: open\n---\n"})
        self.assertEqual(self.run_lint(files), [])


class LintFixRound1Test(unittest.TestCase):
    run_lint = LintTest.run_lint
    has = LintTest.has

    def test_stored_confidence_and_priority_are_problems(self):
        for field in ("confidence", "priority"):
            self.has(v2files(**{"work/app/a.md": mem(extra=f"{field}: 0.9\n")}), "computed on read; remove this field")
            self.has(v2files(**{"work/app/a.md": mem(meta=f"  {field}: 0.9\n")}), "computed on read; remove this field")
            self.has(v2files(**{EDGE: _edge(**{field: "0.4"})}), "computed on read; remove this field")

    def test_id_must_equal_stem(self):
        self.has(v2files(**{EXP: _exp(id="other")}), "id 'other' is not '2026-10-29-x'")
        self.has(v2files(**{EDGE: _edge(id="zz")}), "id 'zz' is not 'e1'")
        self.assertEqual(self.run_lint(v2files(**{"work/app/contexts/2026-01-02-x.md": CTX})), [])  # legacy: id free

    def test_external_evidence_schemes_only(self):
        def ev(ref):
            return mem(extra=f"evidence: [{{ref: {ref}, kind: supports, weight: 1}}]\n")
        for ok in ("feedback:f1", "https://x.io/a", "http://x.io/a"):
            self.assertEqual(self.run_lint(v2files(**{"work/app/a.md": ev(ok)})), [], ok)
        self.has(v2files(**{"work/app/a.md": ev("ftp:x")}), "does not resolve")

    def test_secret_in_file_without_frontmatter(self):
        self.has(v2files(**{"work/app/queue/q.md": "ghp_" + "a" * 36 + "\n"}), "looks like it holds a credential")

    def test_nested_git_repository_is_a_problem(self):
        self.has(v2files(**{"work/app/queue/.git/HEAD": "ref: refs/heads/main\n"}), "nested git repository")
        self.has(v2files(**{"work/app/queue/.git": "gitdir: /elsewhere\n"}), "nested git repository")
        self.has(v2files(**{"work/app/queue/.GIT": "gitdir: /elsewhere\n"}), "nested git repository")

    def test_bad_markers(self):
        for bad in ("0", "-1", "2_0", "+2", " ", "x"):
            self.has(v2files(**{".lrdmem-format": bad}), ".lrdmem-format")


class LintGitTest(unittest.TestCase):
    ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **self.ENV})

    def test_edited_experience(self):
        with tempfile.TemporaryDirectory() as t:
            files = v2files()
            root = make_store(Path(t), files)
            work = root / "work"
            self.git(work, "init", "-q")
            self.git(work, "add", "-A")
            self.git(work, "commit", "-qm", "init")
            with mock.patch.dict(os.environ, self.ENV):
                self.assertEqual(lint(Store(root), git_checks=True), [])
                (root / EXP).write_text(_exp().replace("observed", "rewritten"))
                self.assertEqual(lint(Store(root)), [])  # off by default
                out = lint(Store(root), git_checks=True)
                self.assertEqual(len(out), 1, out)
                self.assertIn(f"{EXP}: experience changed since its first commit", out[0])
                self.git(work, "add", "-A")
                self.git(work, "commit", "-qm", "edit")
                self.assertEqual(len(lint(Store(root), git_checks=True)), 1)

    def test_planted_repo_inside_experiences_is_never_consulted(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), v2files())
            marker = Path(t) / "pwned"
            exp_dir = root / "work/app/experiences"
            with mock.patch.dict(os.environ, self.ENV):
                self.git(root / "work", "init", "-q")  # real repo; the experience is untracked there
                planted = Path(t) / "planted"
                planted.mkdir()
                self.git(planted, "init", "-q")
                (planted / EXP.rsplit("/", 1)[1]).write_text("different\n")
                self.git(planted, "add", "-A")
                self.git(planted, "commit", "-qm", "x")
                self.git(planted, "config", "core.fsmonitor", f"touch {marker}")
                (planted / ".git").rename(exp_dir / ".git")
                self.assertEqual(lint(Store(root), git_checks=True),
                                 ["work/app/experiences/.git: nested git repository inside the store; move it out of the store"])
            self.assertFalse(marker.exists())

    def test_untracked_or_outside_git_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), v2files())
            with mock.patch.dict(os.environ, self.ENV):
                self.assertEqual(lint(Store(root), git_checks=True), [])
                self.git(root / "work", "init", "-q")
                self.assertEqual(lint(Store(root), git_checks=True), [])


if __name__ == "__main__":
    unittest.main()
