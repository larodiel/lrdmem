import json
from pathlib import Path

from lrdmem.hooks import patch_path
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
        text = (th.ClaudeHookTest.header(self) + "\n\n## _global/MEMORY.md\n(empty)\n\n## work/_global/MEMORY.md\n"
                "(empty)\n\n## work/app/MEMORY.md\n- [PHP style](php-style.md) — tabs")
        self.assertEqual(json.loads(self.hook("session-start", self.payload("session_start"))),
                         self.ctx("SessionStart", text))
        self.assertEqual(self.hook(None, self.payload("session_start"), argv=["context", "--agent", "codex"]),
                         self.hook("session-start", self.payload("session_start")))
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_apply_patch_injects_once_per_session(self):
        self.store()
        text = "## lrdmem: rules for src/a.php\n\n### php-style\n" + th.STYLE_BODY.strip() + "\n"
        out = self.hook("pre-tool", self.payload("pre_tool_apply_patch"))
        self.assertEqual(json.loads(out), self.ctx("PreToolUse", text))
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_apply_patch")), "")

    def test_apply_patch_relative_to_subdir_cwd(self):
        self.store()
        (self.repo / "src").mkdir()
        p = self.payload("pre_tool_apply_patch", cwd=str(self.repo / "src"))
        p["tool_input"]["command"] = "*** Begin Patch\n*** Add File: a.php\n+y\n*** End Patch\n"
        out = json.loads(self.hook("pre-tool", p))
        self.assertIn("## lrdmem: rules for src/a.php", out["hookSpecificOutput"]["additionalContext"])

    def test_pre_command(self):
        self.store()
        text = "## lrdmem: rules for composer install\n\n### php-style\n" + th.STYLE_BODY.strip() + "\n"
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


class PatchPathTest(th.unittest.TestCase):
    def test_first_added_or_updated_file(self):
        self.assertEqual(patch_path("*** Begin Patch\n*** Update File: a.py\n@@\n*** Add File: b.py\n"), "a.py")
        self.assertEqual(patch_path("*** Add File: dir/b c.py\n+x\n"), "dir/b c.py")

    def test_move_target_and_deletes_skipped(self):
        self.assertEqual(patch_path("*** Delete File: x\n*** Update File: a.py\n*** Move to: b.py\n"), "b.py")
        self.assertEqual(patch_path("*** Update File: a.py\n*** Delete File: x\n*** Move to: b.py\n"), "a.py")
        self.assertIsNone(patch_path("*** Begin Patch\n*** Delete File: x\n*** End Patch"))
        self.assertIsNone(patch_path(""))


if __name__ == "__main__":
    th.unittest.main()
