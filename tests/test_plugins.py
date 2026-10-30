import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lrdmem import __version__, install

ROOT = Path(__file__).parent.parent
PLUGINS = {"claude": (ROOT / "plugins/claude-code", ".claude-plugin", "CLAUDE_PLUGIN_ROOT"),
           "codex": (ROOT / "plugins/codex", ".codex-plugin", "PLUGIN_ROOT")}


class PluginTest(unittest.TestCase):
    def test_hooks_match_install(self):
        for agent, (base, _, var) in PLUGINS.items():
            data = json.loads((base / "hooks/hooks.json").read_text())
            want = {}
            for event, matcher, phase in install._EVENTS[agent]:
                hook = {"type": "command", "command": f'sh "${{{var}}}/scripts/lrdmem-hook" {phase} {agent}',
                        "timeout": install._TIMEOUT.get(phase, 10)}
                want[event] = [({"matcher": matcher} if matcher else {}) | {"hooks": [hook]}]
            self.assertEqual(data, {"hooks": want}, f"{base}/hooks/hooks.json is out of date with install._EVENTS")

    def test_skill_and_version_match_the_package(self):
        skill = (ROOT / "lrdmem/skill/SKILL.md").read_bytes()
        for agent, (base, manifest, _) in PLUGINS.items():
            self.assertEqual((base / "skills/lrdmem/SKILL.md").read_bytes(), skill, f"{agent} plugin skill is stale")
            self.assertEqual(json.loads((base / manifest / "plugin.json").read_text())["version"], __version__)
            self.assertEqual((base / "scripts/lrdmem-hook").read_bytes(),
                             (PLUGINS["claude"][0] / "scripts/lrdmem-hook").read_bytes())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(market["plugins"][0]["version"], __version__)
        self.assertTrue((ROOT / market["plugins"][0]["source"] / ".claude-plugin/plugin.json").is_file())
        codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertTrue((ROOT / codex["plugins"][0]["source"]["path"] / ".codex-plugin/plugin.json").is_file())

    def _launch(self, env: dict, stdin: str = "{}") -> subprocess.CompletedProcess:
        script = PLUGINS["claude"][0] / "scripts/lrdmem-hook"
        return subprocess.run(["sh", str(script), "pre-tool", "claude"], input=stdin, capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", **env}, timeout=10)

    def test_launcher_without_lrdmem_never_blocks(self):
        r = self._launch({"LRDMEM_BIN": "/nonexistent/lrdmem"}, stdin='{"x": 1}')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_launcher_hands_phase_and_agent_to_lrdmem(self):
        with tempfile.TemporaryDirectory() as t:
            fake = Path(t) / "lrdmem"
            fake.write_text('#!/bin/sh\necho "$@"; cat\n')
            os.chmod(fake, 0o755)
            r = self._launch({"LRDMEM_BIN": str(fake)}, stdin="payload")
        self.assertEqual((r.returncode, r.stdout), (0, "hook pre-tool --agent claude\npayload"))


if __name__ == "__main__":
    unittest.main()
