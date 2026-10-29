import json
import os
import subprocess
from io import StringIO
from contextlib import redirect_stdout
from unittest import mock

from lrdmem.cli import main
from lrdmem.lint import lint
from lrdmem.store import Store
from tests.fixtures import make_store
from tests.test_hooks import _Base

SCOPED = ("---\nname: pr-gate\ndescription: 'TEC repos: proposal and design before every PR'\nmetadata:\n"
          "  type: feedback\n  author: claude-code\n  updated: 2026-10-12\n---\n"
          "Write the plan first; see [[app-notes]].\n\n\n**Why:** w\n**How to apply:** h\n")
NOTES = ("---\nname: app-notes\ndescription: Notes about the app\nmetadata:\n  type: project\n"
         "  author: claude-code\n  updated: 2026-10-12\n---\nSee [[pr-gate]] too.\n\n\n**Why:** w\n**How to apply:** h\n")
PHP = NOTES.replace("app-notes", "php").replace("[[pr-gate]]", "tabs").replace("---\nSee", "triggers:\n  paths: ['**/*.php']\n---\nSee")
PERSONAL = ("---\nname: tone\ndescription: reply tone\nmetadata:\n  type: user\n  author: claude-code\n"
            "  updated: 2026-10-12\n---\nBe brief.\n\n\n**Why:** w\n**How to apply:** h\n")


class CompatTest(_Base):
    """A store shaped like the bin/memory one: README and bin/ at the root, a symlinked env, a git-repo env."""

    def setUp(self):
        super().setUp()
        self.root = make_store(self.tmp, {
            "README.md": "# Memories\n", "bin/memory": "#!/usr/bin/env python3\n", ".gitignore": "personal\n",
            "_global/MEMORY.md": "- [Tone](tone.md) — reply tone\n", "_global/tone.md": PERSONAL,
            "work/_global/MEMORY.md": "- [PR gate](pr-gate.md) — TEC repos: plan first\n",
            "work/_global/pr-gate.md": SCOPED, "work/README.md": "copy of the root readme\n",
            "work/app/MEMORY.md": "- [App notes](app-notes.md) — notes\n- [PHP](php.md) — php\n", "work/app/app-notes.md": NOTES,
            "work/app/php.md": PHP,
            "elsewhere/_global/MEMORY.md": "", "elsewhere/other/MEMORY.md": "",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.root / "work", check=True)
        (self.root / "personal").symlink_to(self.root / "elsewhere")
        os.environ["LRDMEM_STORE"] = str(self.root)

    def test_lint_is_clean(self):
        self.assertEqual(lint(Store(self.root)), [])

    def test_session_start_lists_all_three_indexes(self):
        payload = self.payload("session_start")
        with mock.patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["hook", "session-start", "--agent", "claude"]), 0)
        text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        for header in ("## _global/MEMORY.md", "## work/_global/MEMORY.md", "## work/app/MEMORY.md"):
            self.assertIn(header, text)
        self.assertIn("TEC repos: plan first", text)

    def test_env_global_not_trigger_injected(self):
        out = self.hook("pre-tool", self.payload("pre_tool_edit"))
        self.assertIn("tabs", out)
        self.assertNotIn("Write the plan first", out)
