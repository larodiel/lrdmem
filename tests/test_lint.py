import io
import os
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


if __name__ == "__main__":
    unittest.main()
