import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lrdmem import cli, experience
from lrdmem.frontmatter import parse
from lrdmem.store import Store
from tests.fixtures import make_store

WHEN = datetime(2026, 10, 29, 14, 3, 0, tzinfo=timezone.utc)
KW = dict(kind="decision", agent="claude", text="Chose spaces over tabs for indentation in the whole repo")


class _Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": ""})
        self.repo = self.tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.store = Store(self.root)

    def add(self, **kw):
        return experience.add(self.store, kw.pop("cwd", self.repo), **{**KW, **kw})

    def files(self):
        return sorted(p for p in self.root.rglob("experiences/*.md"))


class ExperienceTest(_Base):
    def test_writes_file_with_spec_fields(self):
        e, created = self.add(when=WHEN, files=["a.py"], ref="http://x/1", request_id="r1", device="mac")
        self.assertTrue(created)
        path = self.root / "acme/web/experiences/2026-10-29-chose-spaces-over-tabs-for-indentation.md"
        self.assertEqual(e.path, path)
        data = parse(path.read_text())
        self.assertEqual(data, {"id": path.stem, "date": "2026-10-29T14:03:00Z", "kind": "decision", "agent": "claude",
                                "device": "mac", "repo": "web", "files": ["a.py"], "source": "chat",
                                "ref": "http://x/1", "request_id": "r1"})
        self.assertIn(KW["text"], path.read_text().split("---\n")[-1])
        self.assertEqual(e.ref, "acme/web/experiences/" + path.stem)
        self.assertEqual([x.path for x in self.store.experiences()], [path])

    def test_same_day_same_text_gets_unique_id(self):
        a, _ = self.add(when=WHEN)
        b, _ = self.add(when=WHEN)
        self.assertNotEqual(a.path, b.path)
        self.assertTrue(b.path.stem.endswith("-2"))

    def test_scopes_and_empty_slug(self):
        self.add(scope="env", text="!!!")
        self.add(scope="global", text="x", when=WHEN)
        self.assertTrue((self.root / "acme/_global/experiences").is_dir())
        self.assertEqual(len(list((self.root / "acme/_global/experiences").glob("*-experience.md"))), 1)
        self.assertEqual(len(list((self.root / "_global/experiences").glob("*.md"))), 1)

    def test_request_id_dedupes_within_scope(self):
        a, c1 = self.add(request_id="r1")
        b, c2 = self.add(request_id="r1", text="something else entirely")
        self.assertEqual((c1, c2, b.path), (True, False, a.path))
        self.assertEqual(len(self.files()), 1)
        _, c3 = self.add(request_id="r1", scope="global")  # other scope: its own experience
        self.assertTrue(c3)
        self.add(); self.add()  # no request_id: no dedupe
        self.assertEqual(len(self.files()), 4)

    def test_refusals_write_nothing(self):
        bad = [dict(kind="nope"), dict(source="nope"), dict(agent=" "), dict(text=" "), dict(scope="x"),
               dict(text="token ghp_" + "a" * 36), dict(agent="password=hunter2hunter2"),
               dict(files=["/etc/passwd"]), dict(files=["a/../../b"]), dict(env="../evil"), dict(env="nope"),
               dict(ref="a\nb"), dict(cwd=self.tmp)]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(experience.Refused):
                self.add(**kw)
        self.assertEqual(self.files(), [])
        self.assertFalse(list(self.root.glob("**/experiences")))

    def test_refused_is_write_refused_and_no_edit_api(self):
        from lrdmem import write
        self.assertTrue(issubclass(experience.Refused, write.Refused))
        self.assertEqual([n for n in dir(experience) if n in ("update", "edit", "set", "delete", "remove")], [])


class ExperienceCliTest(ExperienceTest):
    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        sin = io.StringIO(stdin)
        env = {"LRDMEM_STORE": str(self.root), "LRDMEM_CONFIG": str(self.tmp / "cfg.json")}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", sin), mock.patch.object(sin, "isatty", return_value=False, create=True), \
                mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["experience", "add", *argv])
        return code, out.getvalue().strip(), err.getvalue()

    def test_text_argument(self):
        code, out, _ = self.run_cli(["--kind", "error", "--agent", "claude", "boom happened"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^acme/web/experiences/\d{4}-\d\d-\d\d-boom-happened$")
        self.assertEqual(len(self.files()), 1)

    def test_json_stdin_and_existing(self):
        payload = json.dumps({"text": "found it", "kind": "discovery", "agent": "a", "scope": "env",
                              "files": ["x.py"], "request_id": "q"})
        code, out, _ = self.run_cli([], payload)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("acme/_global/experiences/"))
        code, out2, _ = self.run_cli([], payload)
        self.assertEqual((code, out2), (0, out + " (existing)"))
        self.assertEqual(len(self.files()), 1)

    def test_text_and_stdin_refused(self):
        code, _, err = self.run_cli(["--kind", "error", "--agent", "a", "t"], '{"text": "u"}')
        self.assertEqual(code, 1)
        self.assertIn("not both", err)
        self.assertEqual(self.files(), [])

    def test_bad_input_refused(self):
        for stdin in ("not json", "[1]", '{"text": "t", "bogus": "x"}', '{"text": "t", "kind": 3, "agent": "a"}',
                      '{"kind": "error", "agent": "a"}', '{"text": "pw", "kind": "error", "agent": "a", "files": "x"}'):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_cli([], stdin)[0], 1)
        code, _, err = self.run_cli(["--kind", "error", "--agent", "a", "token ghp_" + "a" * 36])
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()
