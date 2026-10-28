import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import config
from lrdmem.cli import main
from lrdmem.store import store_root


class ConfigTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.path = self.tmp / "cfgdir" / "config.json"
        p = mock.patch.dict(os.environ, {"LRDMEM_CONFIG": str(self.path)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("LRDMEM_STORE", None)

    def test_defaults_and_corrupt(self):
        cfg = config.load()
        self.assertEqual((cfg["store"], cfg["autopush"], cfg["allow"]), (None, {}, {}))
        self.assertTrue(cfg["device"])
        self.path.parent.mkdir()
        for bad in ("{nope", "[1]", '{"allow": 3, "autopush": "x"}'):
            self.path.write_text(bad)
            self.assertEqual(config.load()["allow"], {})
            self.assertEqual(config.load()["autopush"], {})

    def test_save_modes_and_unknown_keys(self):
        cfg = config.load()
        cfg["extra"] = {"a": 1}
        config.save(cfg)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(config.load()["extra"], {"a": 1})
        self.assertEqual(json.loads(self.path.read_text())["extra"], {"a": 1})

    def test_save_cleans_temp_and_dir_mode_rules(self):
        self.path.parent.mkdir(mode=0o755)
        os.chmod(self.path.parent, 0o755)
        with mock.patch("os.replace", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                config.save(config.load())
        self.assertEqual(list(self.path.parent.iterdir()), [])
        config.save(config.load())
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)  # not ours to tighten

    def test_save_tightens_existing_lrdmem_dir(self):
        d = self.tmp / "lrdmem"
        d.mkdir()
        os.chmod(d, 0o755)
        with mock.patch.dict(os.environ, {"LRDMEM_CONFIG": str(d / "config.json")}):
            config.save(config.load())
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)

    def test_store_root_falls_back_to_config(self):
        with self.assertRaises(RuntimeError):
            store_root()
        config.save({**config.load(), "store": "/some/store"})
        self.assertEqual(store_root(), Path("/some/store"))
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": "/env/store"}):
            self.assertEqual(store_root(), Path("/env/store"))

    def test_cli_config(self):
        def run(*a):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["config", *a]), 0)
            return out.getvalue()
        run("set", "store", "rel/dir")
        run("set", "device", "box")
        run("autopush", "on")
        run("autopush", "off", "--env", "work")
        shown = json.loads(run("show"))
        self.assertEqual(shown["store"], str(Path("rel/dir").resolve()))
        self.assertEqual((shown["device"], shown["autopush"]), ("box", {"*": True, "work": False}))


if __name__ == "__main__":
    unittest.main()
