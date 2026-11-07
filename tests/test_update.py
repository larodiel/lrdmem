import io
import json
import os
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from lrdmem import __version__, update
from lrdmem.cli import main


def response(version):
    body = json.dumps({"info": {"version": version}}).encode()
    return mock.MagicMock(**{"read.return_value": body, "__enter__.return_value.read.return_value": body})


class InstallerTest(unittest.TestCase):
    def test_uv_and_pipx_environments_are_recognised(self):
        self.assertEqual(update.installer("/home/u/.local/share/uv/tools/lrdmem"), "uv")
        self.assertEqual(update.installer("/home/u/.local/pipx/venvs/lrdmem"), "pipx")
        self.assertEqual(update.installer("/home/u/Library/Application Support/pipx/venvs/lrdmem"), "pipx")

    def test_anything_else_is_unknown(self):
        self.assertIsNone(update.installer("/usr/local"))
        self.assertIsNone(update.installer("/home/u/work/venvs/lrdmem"))
        self.assertIsNone(update.installer("/home/u/tools/lrdmem"))


class VersionTest(unittest.TestCase):
    def test_versions_compare_numerically(self):
        self.assertTrue(update.newer("0.10.0", "0.9.9"))
        self.assertFalse(update.newer("0.2.0", "0.2.0"))
        self.assertFalse(update.newer("0.1.9", "0.2.0"))

    def test_latest_reads_the_version_from_pypi(self):
        with mock.patch("urllib.request.urlopen", return_value=response("1.2.3")) as o:
            self.assertEqual(update.latest(), "1.2.3")
        self.assertEqual(o.call_args.args[0], "https://pypi.org/pypi/lrdmem/json")
        self.assertEqual(o.call_args.kwargs["timeout"], 5)

    def test_latest_refuses_an_answer_that_is_not_a_plain_version(self):
        for bad in ("1.2.3; rm -rf /", "../../x", "1.2", "", "1.2.3rc1"):
            with mock.patch("urllib.request.urlopen", return_value=response(bad)):
                with self.assertRaises(update.UpdateError, msg=bad):
                    update.latest()

    def test_latest_turns_network_and_parse_failures_into_update_errors(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(update.UpdateError):
                update.latest()
        broken = mock.MagicMock(**{"__enter__.return_value.read.return_value": b"not json"})
        with mock.patch("urllib.request.urlopen", return_value=broken):
            with self.assertRaises(update.UpdateError):
                update.latest()


class RunTest(unittest.TestCase):
    def run_update(self, argv, **patches):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["update", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_check_reports_a_newer_version_and_exits_1(self):
        with mock.patch.object(update, "latest", return_value="9.9.9"):
            code, out, _ = self.run_update(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("9.9.9", out)
        self.assertIn("lrdmem update", out)

    def test_check_on_the_latest_version_exits_0(self):
        with mock.patch.object(update, "latest", return_value=__version__):
            code, out, _ = self.run_update(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("up to date", out)

    def test_check_offline_is_an_error_not_an_update(self):
        with mock.patch.object(update, "latest", side_effect=update.UpdateError("offline")):
            code, out, err = self.run_update(["--check"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("offline", err)

    def test_update_runs_the_installers_upgrade_with_fixed_arguments(self):
        for kind, argv in (("uv", ["uv", "tool", "upgrade", "lrdmem"]), ("pipx", ["pipx", "upgrade", "lrdmem"])):
            with mock.patch.object(update, "installer", return_value=kind), \
                    mock.patch("shutil.which", return_value="/bin/x"), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
                code, out, _ = self.run_update([])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[0], argv)
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("claude plugin update", out)

    def test_update_passes_the_installers_failure_through(self):
        with mock.patch.object(update, "installer", return_value="uv"), \
                mock.patch("shutil.which", return_value="/bin/x"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=3)):
            code, _, _ = self.run_update([])
        self.assertEqual(code, 3)

    def test_an_unknown_installation_runs_nothing_and_says_how(self):
        with mock.patch.object(update, "installer", return_value=None), \
                mock.patch("subprocess.run") as run:
            code, _, err = self.run_update([])
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("uv tool upgrade lrdmem", err)

    def test_a_missing_installer_binary_runs_nothing(self):
        with mock.patch.object(update, "installer", return_value="uv"), \
                mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run") as run:
            code, _, err = self.run_update([])
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("uv", err)

    def test_it_needs_no_store(self):
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": "/nonexistent-lrdmem", "LRDMEM_CONFIG": "/nonexistent/c.json"}), \
                mock.patch.object(update, "latest", return_value=__version__):
            code, _, _ = self.run_update(["--check"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
