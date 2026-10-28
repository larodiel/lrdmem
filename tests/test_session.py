import os
import stat
import tempfile
import unittest
from pathlib import Path

from lrdmem.session import Session


class SessionTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        old = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = t.name
        tempfile.tempdir = None
        self.addCleanup(self._restore, old)
        self.dir = self.tmp / f"lrdmem-{os.getuid()}"

    @staticmethod
    def _restore(old):
        if old is None:
            del os.environ["TMPDIR"]
        else:
            os.environ["TMPDIR"] = old
        tempfile.tempdir = None

    def _saved(self, id="abc"):
        s = Session(id)
        s.injected.add("x")
        s.edited.append("a.php")
        s.stop_blocks = 2
        s.save()
        return s

    def test_round_trip(self):
        self._saved()
        t = Session("abc")
        self.assertEqual((t.injected, t.edited, t.stop_blocks), ({"x"}, ["a.php"], 2))
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.dir / "abc.json").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["abc.json"])  # no temp leftovers

    def test_corrupt_and_empty_id(self):
        self._saved()
        (self.dir / "abc.json").write_text("{bad")
        self.assertEqual(Session("abc").injected, set())
        Session("").save()

    def test_hostile_id_stays_in_dir(self):
        Session("../../evil").save()
        self.assertTrue((self.dir / "______evil.json").exists())

    def test_symlinked_dir_is_in_memory_only(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.dir.symlink_to(target)
        self._saved()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(Session("abc").injected, set())

    def test_symlinked_file_is_ignored(self):
        self._saved()
        real = self.tmp / "real.json"
        real.write_text('{"injected": ["evil"], "edited": [], "stop_blocks": 0}')
        (self.dir / "abc.json").unlink()
        (self.dir / "abc.json").symlink_to(real)
        self.assertEqual(Session("abc").injected, set())

    def test_save_never_raises(self):
        self.dir.write_text("a file where the dir should be")
        self._saved()  # must not raise
        self.dir.unlink()
        self.dir.mkdir(mode=0o700)
        self.dir.chmod(0o500)
        self.addCleanup(self.dir.chmod, 0o700)
        self._saved()  # read-only dir: must not raise


if __name__ == "__main__":
    unittest.main()
