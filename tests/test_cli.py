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


if __name__ == "__main__":
    unittest.main()
