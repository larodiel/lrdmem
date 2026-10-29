import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.install import InstallError, install

# A realistic entry: several hooks in one group, unknown keys, the old tool next to an unrelated hook.
UNRELATED = {"matcher": "Bash", "hooks": [
    {"type": "command", "command": "/usr/local/bin/rtk-rewrite.sh", "timeout": 5},
    {"type": "command", "command": "~/.claude/hooks/guard.py --strict", "statusMessage": "guarding"}]}
SETTINGS = {
    "model": "opus",
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "/Volumes/x/memories/bin/memory context"}]},
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}],
        "PreToolUse": [
            UNRELATED,
            {"matcher": "Edit", "hooks": [
                {"type": "command", "command": "~/memories/bin/memory context --agent claude"},
                {"type": "command", "command": "prettier-check"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
    "enabledPlugins": {"x@y": True},
}


def commands(data, event):
    return [h["command"] for g in data["hooks"].get(event, []) for h in g["hooks"]]


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)

    def put(self, rel, data):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n")
        return p

    def snapshot(self):
        return {p.relative_to(self.home): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in self.home.rglob("*")}


class ClaudeInstallTest(_Home):
    def test_adds_hooks_removes_old_tool_keeps_the_rest(self):
        p = self.put(".claude/settings.json", SETTINGS)
        before = p.read_text()
        changes = install("claude", self.home)
        self.assertTrue(changes)
        data = json.loads(p.read_text())
        self.assertEqual(list(data), ["model", "hooks", "enabledPlugins"])
        self.assertEqual(data["enabledPlugins"], {"x@y": True})
        for event, phase in (("SessionStart", "session-start"), ("PreToolUse", "pre-tool"),
                             ("PostToolUse", "post-tool"), ("UserPromptSubmit", "prompt"), ("Stop", "stop")):
            ours = [c for c in commands(data, event) if f" hook {phase} --agent claude" in c]
            self.assertEqual(len(ours), 1, event)
        self.assertFalse(any("bin/memory context" in c for e in data["hooks"] for c in commands(data, e)))
        groups = {g.get("matcher"): g for g in data["hooks"]["PreToolUse"]}
        self.assertEqual(groups["Edit|Write|MultiEdit|NotebookEdit|Bash"]["hooks"][0]["type"], "command")
        self.assertEqual(data["hooks"]["PreToolUse"][0], UNRELATED)
        self.assertEqual(groups["Edit"]["hooks"], [{"type": "command", "command": "prettier-check"}])
        self.assertEqual(data["hooks"]["Notification"], SETTINGS["hooks"]["Notification"])
        # The old SessionStart group became empty: our group takes its place, the other one stays.
        self.assertEqual(data["hooks"]["SessionStart"][0]["matcher"], "startup|resume|clear|compact")
        self.assertEqual(data["hooks"]["SessionStart"][1], SETTINGS["hooks"]["SessionStart"][1])
        self.assertEqual(data["hooks"]["PostToolUse"][0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit")
        self.assertNotIn("matcher", data["hooks"]["Stop"][0])
        # The unrelated entry is kept byte for byte.
        block = json.dumps(UNRELATED, indent=2).replace("\n", "\n      ")
        self.assertIn(block, before)
        self.assertIn(block, p.read_text())
        bak = p.with_name(f"settings.json.bak-{date.today().isoformat()}")
        self.assertEqual(bak.read_text(), before)
        skill = (self.home / ".claude/skills/lrdmem/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: lrdmem\n"))

    def test_second_run_changes_nothing(self):
        p = self.put(".claude/settings.json", SETTINGS)
        install("claude", self.home)
        text, snap = p.read_text(), self.snapshot()
        self.assertEqual(install("claude", self.home), [])
        self.assertEqual(p.read_text(), text)
        self.assertEqual(self.snapshot(), snap)

    def test_absolute_quoted_command_replaces_earlier_entry(self):
        p = self.put(".claude/settings.json", SETTINGS)
        install("claude", self.home)
        with mock.patch.object(sys, "argv", ["/opt/my tools/bin/lrdmem"]):
            changes = install("claude", self.home)
        self.assertIn(f"{p.resolve()}: Stop remove `lrdmem hook stop --agent claude`", changes)
        self.assertEqual(commands(json.loads(p.read_text()), "Stop"),
                         ["'/opt/my tools/bin/lrdmem' hook stop --agent claude"])

    def test_backup_not_overwritten(self):
        p = self.put(".claude/settings.json", SETTINGS)
        bak = p.with_name(f"settings.json.bak-{date.today().isoformat()}")
        bak.write_text("older")
        install("claude", self.home)
        self.assertEqual(bak.read_text(), "older")

    def test_dry_run_writes_nothing(self):
        self.put(".claude/settings.json", SETTINGS)
        snap = self.snapshot()
        changes = install("claude", self.home, dry_run=True)
        self.assertTrue(any("bin/memory context" in c for c in changes))
        self.assertTrue(any("SKILL.md" in c for c in changes))
        self.assertEqual(self.snapshot(), snap)

    def test_missing_settings_created(self):
        install("claude", self.home)
        data = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(list(data["hooks"]), ["SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"])

    def test_symlinked_settings_edited_through_link(self):
        real = self.put("dotfiles/settings.json", SETTINGS)
        os.chmod(real, 0o640)
        link = self.home / ".claude/settings.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        install("claude", self.home)
        self.assertTrue(link.is_symlink())
        self.assertIn("lrdmem", real.read_text())
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o640)
        self.assertTrue(real.with_name(f"settings.json.bak-{date.today().isoformat()}").exists())

    def test_invalid_json_refused_unchanged(self):
        p = self.home / ".claude/settings.json"
        p.parent.mkdir(parents=True)
        p.write_text("{ not json")
        snap = self.snapshot()
        with self.assertRaises(InstallError) as cm:
            install("claude", self.home)
        self.assertIn(str(p), str(cm.exception))
        self.assertEqual(self.snapshot(), snap)


class CodexInstallTest(_Home):
    def test_hooks_json(self):
        p = self.put(".codex/hooks.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}})
        install("codex", self.home)
        hooks = json.loads(p.read_text())["hooks"]
        self.assertEqual(hooks["Stop"][0], {"hooks": [{"type": "command", "command": "x"}]})
        self.assertIn(" hook stop --agent codex", hooks["Stop"][1]["hooks"][0]["command"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "apply_patch|Bash")
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "apply_patch")
        self.assertTrue((self.home / ".agents/skills/lrdmem/SKILL.md").is_file())
        self.assertEqual(install("codex", self.home), [])


class OpenCodeInstallTest(_Home):
    def test_plugin_and_skill(self):
        install("opencode", self.home)
        plugin = (self.home / ".config/opencode/plugins/lrdmem.js").read_text()
        self.assertIn("process.env.LRDMEM_BIN ||", plugin)
        self.assertIn('id: "lrdmem"', plugin)
        self.assertTrue((self.home / ".config/opencode/skills/lrdmem/SKILL.md").is_file())
        self.assertEqual(install("opencode", self.home), [])


class SkillTest(unittest.TestCase):
    def test_frontmatter(self):
        text = (Path(__file__).parents[1] / "lrdmem/skill/SKILL.md").read_text()
        lines = text.splitlines()
        self.assertLess(len(lines), 150)
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        fm = dict(ln.split(": ", 1) for ln in lines[1:end])
        self.assertEqual(fm["name"], "lrdmem")
        self.assertTrue(fm["description"].startswith("Use when the user corrects your work"))
        for needle in ("lrdmem find", "--scope repo", "--scope env", "--scope global", "lrdmem for",
                       "lrdmem why", "lrdmem learn-pr", "secret", "not instructions"):
            self.assertIn(needle, text)


class CliInstallTest(_Home):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["install", *argv])
        finally:
            os.environ["HOME"] = old
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_output(self):
        code, out, _ = self.run_cli("--agent", "codex", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("/hooks", out)
        self.assertEqual(out.splitlines()[-1], "Obligations run only after 'lrdmem allow'.")
        self.assertFalse((self.home / ".codex").exists())

    def test_invalid_json_exit_1(self):
        p = self.home / ".claude/settings.json"
        p.parent.mkdir(parents=True)
        p.write_text("[")
        code, out, err = self.run_cli("--agent", "claude")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertEqual(p.read_text(), "[")


if __name__ == "__main__":
    unittest.main()
