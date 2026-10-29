import io
import unittest
from contextlib import redirect_stdout

from lrdmem.cli import main


class CliTest(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "lrdmem 0.1.0")


    def test_unconfigured_store_is_one_clear_line(self):
        import os
        from contextlib import redirect_stderr
        from unittest import mock
        for argv in (["find", "x"], ["lint"], ["sync"]):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {}), redirect_stderr(err):
                os.environ.pop("LRDMEM_STORE", None)
                self.assertEqual(main(argv), 1)
            self.assertEqual(err.getvalue(), "lrdmem: no store configured — run 'lrdmem config set store <path>' "
                                             "or set LRDMEM_STORE\n")


    def test_version_has_one_source(self):
        import tomllib
        from pathlib import Path
        data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
        self.assertNotIn("version", data["project"])
        self.assertIn("version", data["project"]["dynamic"])
        self.assertEqual(data["tool"]["setuptools"]["dynamic"]["version"], {"attr": "lrdmem.__version__"})


    def test_docs_document_the_cli(self):
        from pathlib import Path
        root = Path(__file__).parent.parent
        text = (root / "README.md").read_text() + (root / "docs" / "guide.md").read_text()
        for needle in ("kind: obligation", "check:", "lrdmem allow", "lrdmem check --on", "config autopush",
                       "learn-pr", "collaborators", "GitHub remote", "--why", "--how", "--paths", "--repos",
                       "install --agent opencode` copies the plugin"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
