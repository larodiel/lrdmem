import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from tests.fixtures import make_store
from tests.test_graph import _files


class VisualizeTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        root = make_store(self.tmp, _files())
        patch = mock.patch.dict(os.environ, {"LRDMEM_STORE": str(root)})
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("webbrowser.open") as opened:
            code = main(["visualize", *argv])
        return code, out.getvalue().strip(), opened

    def test_output_file_is_written_and_opened(self):
        target = self.tmp / "brain.html"
        code, out, opened = self._run("-o", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(out, str(target))
        self.assertIn("phpcs", target.read_text())
        opened.assert_called_once_with(target.as_uri())

    def test_no_open_only_writes(self):
        target = self.tmp / "brain.html"
        code, out, opened = self._run("-o", str(target), "--no-open")
        self.assertEqual((code, out), (0, str(target)))
        self.assertTrue(target.exists())
        opened.assert_not_called()

    def test_default_writes_a_private_temp_page_and_opens_it(self):
        code, out, opened = self._run()
        self.addCleanup(lambda: Path(out).unlink(missing_ok=True))
        self.assertEqual(code, 0)
        page = Path(out)
        self.assertIn("phpcs", page.read_text())
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)
        opened.assert_called_once_with(page.as_uri())

    def test_headless_machine_still_succeeds(self):
        with mock.patch("webbrowser.open", return_value=False):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["visualize"]), 0)
        Path(out.getvalue().strip()).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
