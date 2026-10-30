import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from lrdmem.cli import main
from tests.fixtures import make_store


def _mem(name, desc, state="", mtype="", repos=""):
    meta = (f"  memory_type: {mtype}\n" if mtype else "") + (f"  state: {state}\n" if state else "")
    trig = f"triggers:\n  repos: [{repos}]\n" if repos else ""
    return (f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: feedback\n{meta}  author: me\n"
            f"  updated: 2026-01-01\n{trig}---\nbody\n\n**Why:** w\n**How to apply:** h\n")


FILES = {
    "_global/me.md": _mem("me", "About me"),
    "work/_global/style.md": _mem("style", "Work style", repos="app"),
    "work/_global/elsewhere.md": _mem("elsewhere", "Only for api", repos="api"),
    "work/app/tabs.md": _mem("tabs", "Use tabs", mtype="procedural"),
    "work/app/old.md": _mem("old", "Old rule", state="obsolete"),
    "work/app/guess.md": _mem("guess", "Maybe the cache", state="candidate", mtype="hypothesis"),
    "work/api/routes.md": _mem("routes", "API routes"),
    "home/blog/posts.md": _mem("posts", "Blog posts"),
}


class ListTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, FILES)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        os.environ["LRDMEM_STORE"] = str(self.root)
        self.addCleanup(os.environ.pop, "LRDMEM_STORE", None)
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["list", *argv])
        return code, out.getvalue(), err.getvalue()

    def refs(self, *argv):
        code, out, err = self.run_cli("--json", *argv)
        self.assertEqual(code, 0, err)
        return [d["ref"] for d in json.loads(out)]

    def test_default_is_what_applies_to_this_repo(self):
        self.assertEqual(self.refs(), ["_global/me", "work/_global/style", "work/app/guess", "work/app/tabs"])

    def test_all_env_states_and_types(self):
        self.assertEqual(len(self.refs("--all")), 7)  # everything but the obsolete one
        self.assertIn("work/app/old", self.refs("--states", "all"))
        self.assertEqual(self.refs("--states", "obsolete"), ["work/app/old"])
        self.assertEqual(self.refs("--types", "procedural"), ["work/app/tabs"])
        self.assertEqual(self.refs("--env", "home"), ["_global/me", "home/blog/posts"])

    def test_human_output_groups_by_folder(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("## work/app\n", out)
        self.assertRegex(out, r"  tabs +procedural/active +- +Use tabs\n")  # no evidence: no confidence
        self.assertIn("4 memories", out)

    def test_json_fields(self):
        _, out, _ = self.run_cli("--json", "--types", "procedural")
        [d] = json.loads(out)
        self.assertEqual({k: d[k] for k in ("ref", "folder", "slug", "type", "state", "confidence", "description")},
                         {"ref": "work/app/tabs", "folder": "work/app", "slug": "tabs", "type": "procedural",
                          "state": "active", "confidence": None, "description": "Use tabs"})

    def test_outside_an_opted_in_repo_lists_everything(self):
        os.chdir(self.tmp)
        self.assertEqual(len(self.refs()), 7)


if __name__ == "__main__":
    unittest.main()
