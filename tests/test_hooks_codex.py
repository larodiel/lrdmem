import json
from pathlib import Path

from lrdmem.hooks import patch_paths
from lrdmem.session import Session
from tests import test_hooks as th

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


class CodexHookTest(th._Base):
    def payload(self, name, **over):
        data = json.loads((FIXTURES / f"{name}.json").read_text().replace("{cwd}", str(self.repo)))
        data.update(over)
        return data

    def hook(self, phase, payload, agent="codex", argv=None):
        return super().hook(phase, payload, agent, argv)

    def test_session_start_without_claude_link(self):
        self.store()
        text = (self.header() + "\n\n## _global/MEMORY.md\n(empty)\n\n## work/_global/MEMORY.md\n"
                "(empty)\n\n## work/app/MEMORY.md\n- [PHP style](php-style.md) — tabs")
        self.assertEqual(json.loads(self.hook("session-start", self.payload("session_start"))),
                         self.ctx("SessionStart", text))
        self.assertEqual(self.hook(None, self.payload("session_start"), argv=["context", "--agent", "codex"]),
                         self.hook("session-start", self.payload("session_start")))
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_apply_patch_injects_once_per_session(self):
        self.store()
        text = th.recalled("src/a.php", th.PHP_STYLE)
        out = self.hook("pre-tool", self.payload("pre_tool_apply_patch"))
        self.assertEqual(json.loads(out), self.ctx("PreToolUse", text))
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_apply_patch")), "")

    def test_apply_patch_relative_to_subdir_cwd(self):
        self.store()
        (self.repo / "src").mkdir()
        p = self.payload("pre_tool_apply_patch", cwd=str(self.repo / "src"))
        p["tool_input"]["command"] = "*** Begin Patch\n*** Add File: a.php\n+y\n*** End Patch\n"
        out = json.loads(self.hook("pre-tool", p))
        self.assertIn("## lrdmem: memories for src/a.php", out["hookSpecificOutput"]["additionalContext"])

    def test_multi_file_patch_covers_every_file(self):
        self.multi_store()
        pre = self.payload("pre_tool_apply_patch", tool_input={"command": th.MULTI_PATCH})
        self.assertEqual(json.loads(self.hook("pre-tool", pre)), self.ctx("PreToolUse", self.multi_rules()))
        post = self.payload("post_tool_apply_patch", tool_input={"command": th.MULTI_PATCH})
        self.assertEqual(json.loads(self.hook("post-tool", post)),
                         self.ctx("PostToolUse", "lrdmem check failed: lint-all\ngot src/a.php src/b.php lib/c.js"))
        self.assertEqual(Session("s1").edited, th.MULTI)
        self.assertEqual(json.loads(self.hook("stop", self.payload("stop"))),
                         {"decision": "block", "reason": "lrdmem check failed: stop-all\nlate src/a.php src/b.php lib/c.js"})

    def test_pre_command(self):
        self.store()
        text = th.recalled("composer install", th.PHP_STYLE[:3] + ("command trigger composer",))
        self.assertEqual(json.loads(self.hook("pre-tool", self.payload("pre_tool_bash"))),
                         self.ctx("PreToolUse", text))

    def test_other_tool_and_bad_patch_input_are_noops(self):
        self.store()
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_mcp")), "")
        self.assertEqual(self.hook("post-tool", self.payload("pre_tool_bash")), "")
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_apply_patch", tool_input={"command": 1})), "")
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_apply_patch", tool_input="x")), "")

    def test_post_edit_reports_failures(self):
        store = self.store({"work/app/lint-php.md": th._ob("echo bad {files} && exit 1", "post-edit")})
        self.approve(store)
        out = self.hook("post-tool", self.payload("post_tool_apply_patch"))
        self.assertEqual(json.loads(out), self.ctx("PostToolUse", "lrdmem check failed: lint-php\nbad src/a.php"))

    def test_prompt(self):
        self.store()
        self.assertEqual(json.loads(self.hook("prompt", self.payload("prompt"))),
                         self.ctx("UserPromptSubmit", th.PROMPT_LINE))

    def test_stop_blocks_twice_then_warns(self):
        store = self.store({"work/app/lint-php.md": th._ob("echo bad {files} && exit 1", "stop")})
        self.approve(store)
        self.assertEqual(self.hook("post-tool", self.payload("post_tool_apply_patch")), "")
        text = "lrdmem check failed: lint-php\nbad src/a.php"
        block = {"decision": "block", "reason": text}
        self.assertEqual(json.loads(self.hook("stop", self.payload("stop"))), block)
        self.assertEqual(json.loads(self.hook("stop", self.payload("stop"))), block)
        self.assertEqual(json.loads(self.hook("stop", self.payload("stop"))), {"systemMessage": text})

    def test_stop_hook_active_never_blocks(self):
        store = self.store({"work/app/lint-php.md": th._ob("exit 1", "stop")})
        self.approve(store)
        self.hook("post-tool", self.payload("post_tool_apply_patch"))
        out = json.loads(self.hook("stop", self.payload("stop", stop_hook_active=True)))
        self.assertEqual(out, {"systemMessage": "lrdmem check failed: lint-php\n"})

    def test_stop_clean_prints_nothing(self):
        self.store()
        self.assertEqual(self.hook("stop", self.payload("stop")), "")


class PatchPathsTest(th.unittest.TestCase):
    def test_every_added_or_updated_file(self):
        self.assertEqual(patch_paths(th.MULTI_PATCH), th.MULTI)
        self.assertEqual(patch_paths("*** Add File: dir/b c.py\n+x\n*** Update File: dir/b c.py\n"), ["dir/b c.py"])

    def test_move_only_renames_an_update_and_deletes_are_skipped(self):
        self.assertEqual(patch_paths("*** Update File: a.py\n*** Delete File: x\n*** Move to: b.py\n"), ["a.py"])
        self.assertEqual(patch_paths("*** Begin Patch\n*** Delete File: x\n*** End Patch"), [])
        self.assertEqual(patch_paths(""), [])


if __name__ == "__main__":
    th.unittest.main()
