import os
import tempfile
import unittest
from pathlib import Path

from lrdmem.session import Session


class SessionTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            old, os.environ["TMPDIR"] = os.environ.get("TMPDIR"), d
            tempfile.tempdir = None
            try:
                s = Session("abc")
                s.injected.add("x")
                s.edited.append("a.php")
                s.stop_blocks = 2
                s.save()
                t = Session("abc")
                self.assertEqual((t.injected, t.edited, t.stop_blocks), ({"x"}, ["a.php"], 2))
                (Path(d) / "lrdmem" / "abc.json").write_text("{bad")
                self.assertEqual(Session("abc").injected, set())
                Session("../../evil").save()
                self.assertTrue((Path(d) / "lrdmem" / "______evil.json").exists())
                Session("").save()  # in-memory only
            finally:
                if old is None:
                    del os.environ["TMPDIR"]
                else:
                    os.environ["TMPDIR"] = old
                tempfile.tempdir = None


if __name__ == "__main__":
    unittest.main()
