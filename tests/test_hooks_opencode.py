import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from lrdmem import config
from lrdmem.session import Session
from tests import test_hooks as th

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
PLUGIN = Path(__file__).parent.parent / "lrdmem" / "hooks" / "opencode-plugin.js"
FAIL = "lrdmem check failed: lint-php\nbad src/a.php\n"


class OpenCodeHookTest(th._Base):
    def payload(self, name, **over):
        data = json.loads((FIXTURES / f"{name}.json").read_text().replace("{cwd}", str(self.repo)))
        data.update(over)
        return data

    def hook(self, phase, payload, agent="opencode", argv=None):
        return super().hook(phase, payload, agent, argv)

    def test_session_start_is_plain_text(self):
        self.store()
        text = (self.header() + "\n\n## _global/MEMORY.md\n(empty)\n\n## work/_global/MEMORY.md\n"
                "(empty)\n\n## work/app/MEMORY.md\n- [PHP style](php-style.md) — tabs\n")
        self.assertEqual(self.hook("session-start", self.payload("session_start")), text)
        self.assertEqual(self.hook(None, self.payload("session_start"), argv=["context", "--agent", "opencode"]), text)
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_pre_edit_injects_once_per_session(self):
        self.store()
        text = th.recalled("src/a.php", th.PHP_STYLE) + "\n"
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_edit")), text)
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_edit")), "")
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_edit", session_id="ses_2")), text)

    def test_patch_skips_deletes_and_follows_moves(self):
        self.store()
        self.assertTrue(self.hook("pre-tool", self.payload("pre_tool_patch")).startswith(
            "## lrdmem: memories for src/a.php, c.php\n"))

    def test_multi_file_patch_covers_every_file(self):
        self.multi_store()
        p = self.payload("pre_tool_patch", input={"patchText": th.MULTI_PATCH})
        self.assertEqual(self.hook("pre-tool", p), self.multi_rules() + "\n")
        self.assertEqual(self.hook("post-tool", p), "lrdmem check failed: lint-all\ngot src/a.php src/b.php lib/c.js\n"
                         "lrdmem check failed: stop-all\nlate src/a.php src/b.php lib/c.js\n")
        self.assertEqual(Session("ses_1").edited, th.MULTI)

    def test_pre_command(self):
        self.store()
        text = th.recalled("composer install", th.PHP_STYLE[:3] + ("command trigger composer",)) + "\n"
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_shell")), text)

    def test_other_tool_and_bad_input_are_noops(self):
        self.store()
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_read")), "")
        self.assertEqual(self.hook("post-tool", self.payload("pre_tool_shell")), "")
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_edit", input="x")), "")
        self.assertEqual(self.hook("pre-tool", self.payload("pre_tool_edit", tool=None)), "")

    def test_post_edit_runs_post_edit_and_stop_checks(self):
        store = self.store({"work/app/lint-php.md": th._ob("echo bad {files} && exit 1", "post-edit"),
                            "work/app/stop-php.md": th._ob("echo late {files} && exit 1", "stop", "stop-php")})
        self.approve(store)
        self.approve(store, "stop-php")
        self.assertEqual(self.hook("post-tool", self.payload("post_tool_write")),
                         FAIL + "lrdmem check failed: stop-php\nlate src/a.php\n")

    def test_post_edit_checks_share_a_four_second_budget(self):
        store = self.store({"work/app/lint-php.md": th._ob("sleep 1", "post-edit"),
                            "work/app/stop-php.md": th._ob("exit 1", "stop", "stop-php")})
        self.approve(store)
        self.approve(store, "stop-php")
        with th.mock.patch("lrdmem.hooks.run_checks", wraps=th.checks.run_checks) as rc:
            self.hook("post-tool", self.payload("post_tool_write"))
        first, second = (c.kwargs["budget"] for c in rc.call_args_list)
        self.assertAlmostEqual(first, 4, delta=0.2)
        self.assertLess(second, 3.1)

    def test_post_edit_passing_prints_nothing(self):
        self.store()
        self.assertEqual(self.hook("post-tool", self.payload("post_tool_write")), "")

    def test_prompt(self):
        self.store()
        self.assertEqual(self.hook("prompt", self.payload("prompt")), th.PROMPT_LINE + "\n")

    def test_stop_runs_no_checks_and_never_blocks(self):
        marker = self.tmp / "ran"
        store = self.store({"work/app/lint-php.md": th._ob(f"touch {marker} && exit 1", "stop")})
        self.approve(store)
        self.hook("post-tool", self.payload("post_tool_write"))
        marker.unlink()
        for _ in range(3):
            self.assertEqual(self.hook("stop", self.payload("stop")), "")
        self.assertFalse(marker.exists())

    def test_stop_autopush_syncs(self):
        self.store()
        th.git(self.root, "init", "-q")
        cfg = config.load()
        cfg["autopush"] = {"*": True}
        config.save(cfg)
        self.assertEqual(self.hook("stop", self.payload("stop")), "")
        self.assertIn("work/app/php-style.md", th.git(self.root, "ls-files"))


class PluginFileTest(th.unittest.TestCase):
    def test_small_esm_without_dependencies(self):
        src = PLUGIN.read_text()
        self.assertLess(len(src.splitlines()), 60)
        self.assertNotIn("require(", src)
        self.assertEqual(re.findall(r'from "([^"]+)"', src), ["node:child_process"])
        self.assertIn("export default", src)


DRIVER = r"""
import { existsSync, readFileSync } from "node:fs";
const mod = await import(process.argv[1]);
const hooks = {};
const reg = (domain) => ({ hook: async (name, cb) => { hooks[domain + "." + name] = cb; return { dispose: async () => {} }; } });
const queue = [];
let wake = () => {};
const emit = (e) => { queue.push(e); wake(); };
const ctx = { location: { directory: "/w" }, session: reg("session"), tool: reg("tool"),
  event: { subscribe: () => (async function* () {
    for (;;) { while (queue.length) yield queue.shift(); await new Promise((r) => { wake = r; }); } })() } };
const stops = () => existsSync(process.env.LOG) ? readFileSync(process.env.LOG, "utf8").split("\n").filter((l) => l === "stop").length : 0;
const waitStops = async (n) => {
  const live = existsSync(process.env.LRDMEM_BIN);  // a missing binary never logs: do not wait for it
  for (const end = Date.now() + 10000; live && stops() < n && Date.now() < end;) await new Promise((r) => setTimeout(r, 20));
  return stops();
};
const out = { cleanup: typeof await mod.default.setup(ctx), id: mod.default.id };
const sys = async () => { const ev = { sessionID: "s", system: [] }; await hooks["session.context"](ev); return ev.system; };
out.first = await sys();
await hooks["session.prompt"]({ sessionID: "s" });
out.afterPrompt = await sys();
out.later = await sys();
const before = (id) => hooks["tool.execute.before"]({ tool: "edit", sessionID: "s", id, input: { path: "a" } });
const after = async (id, result, status = "completed") => {
  const ev = { tool: "edit", sessionID: "s", id, input: { path: "a" }, status, ...(result ? { result } : {}) };
  await hooks["tool.execute.after"](ev);
  return ev.result;
};
const call = async (id, result, status) => { await before(id); return after(id, result, status); };
out.str = await call("c1", { content: "ok" });
out.arr = await call("c2", { content: [{ type: "text", text: "ok" }] });
out.err = await call("c3", undefined, "error");
out.afterErr = await sys();
const end = (location) => ({ type: "session.execution.succeeded", data: { sessionID: "s" }, ...(location ? { location } : {}) });
emit(end({ directory: "/elsewhere" }));
emit({ type: "session.updated", data: { sessionID: "s" } });
await before("c9");
emit(end({ directory: "/w" }));
out.stops = await waitStops(1);
out.stale = await after("c9", { content: "ok" });
emit(end());
out.stops2 = await waitStops(2);
out.broken = typeof await mod.default.setup({});
console.log(JSON.stringify(out));
process.exit(0);
"""


class PluginNodeTest(th._Base):
    def setUp(self):
        super().setUp()
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node not installed")
        self.log = self.tmp / "log"
        self.bin = self.tmp / "fake-lrdmem"
        self.bin.write_text(f'#!/bin/sh\necho "$2" >> {self.log}\n'
                            'if [ "$2" = post-tool ] && [ -n "$SLOW" ] && [ ! -e "$SLOW" ]; then : > "$SLOW"; '
                            'exec sleep 30; fi\nprintf "%s:%s" "$2" "$(cat)"\n')
        self.bin.chmod(0o755)

    def run_driver(self, **env):
        r = subprocess.run([self.node, "--input-type=module", "-e", DRIVER, str(PLUGIN)], capture_output=True,
                           text=True, timeout=60, env={**os.environ, "LRDMEM_BIN": str(self.bin), "LOG": str(self.log), **env})
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def test_injection_points(self):
        out = self.run_driver()
        start = {"type": "text", "text": 'session-start:{"session_id":"s","cwd":"/w"}'}
        prompt = {"type": "text", "text": 'prompt:{"session_id":"s","cwd":"/w"}'}
        tool = '{"session_id":"s","cwd":"/w","tool":"edit","input":{"path":"a"}}'
        both = lambda: f"pre-tool:{tool}\n\npost-tool:{tool}"
        self.assertEqual(out["id"], "lrdmem")
        self.assertEqual(out["cleanup"], "function")
        self.assertEqual(out["first"], [start])
        self.assertEqual(out["afterPrompt"], [start, prompt])
        self.assertEqual(out["later"], [start])
        self.assertEqual(out["str"], {"content": "ok\n\n" + both()})
        self.assertEqual(out["arr"], {"content": [{"type": "text", "text": "ok"}, {"type": "text", "text": both()}]})
        self.assertIsNone(out.get("err"))
        self.assertEqual(out["afterErr"], [start, {"type": "text", "text": both()}])
        self.assertEqual(out["broken"], "function")
        self.assertEqual((out["stops"], out["stops2"]), (1, 2))  # the other-directory end event is skipped
        self.assertEqual(out["stale"], {"content": f"ok\n\npost-tool:{tool}"})  # pre-tool text dropped at the end
        self.assertEqual(self.log.read_text().split().count("session-start"), 1)

    def test_missing_binary_injects_nothing(self):
        out = self.run_driver(LRDMEM_BIN=str(self.tmp / "nope"))
        self.assertEqual(out["first"], [])
        self.assertEqual(out["str"], {"content": "ok"})

    def test_spawn_is_time_boxed(self):
        out = self.run_driver(SLOW=str(self.tmp / "slow"))
        self.assertEqual(out["str"], {"content": 'ok\n\npre-tool:{"session_id":"s","cwd":"/w","tool":"edit",'
                                                 '"input":{"path":"a"}}'})


if __name__ == "__main__":
    th.unittest.main()
