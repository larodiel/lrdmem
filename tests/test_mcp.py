import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import mcp
from lrdmem.cli import main
from lrdmem.triggers import CAVEAT
from tests.fixtures import make_store

MEM = ("---\nname: spaces\ndescription: Indent with spaces\nmetadata:\n  type: feedback\n  author: me\n"
       "  updated: 2026-01-01\ntriggers:\n  paths: ['**/*.py']\n---\nUse spaces.\n\n**Why:** style\n**How to apply:** indent\n")


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, **({"params": params} if params is not None else {})}


class McpTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "",
                                          "acme/web/MEMORY.md": "- [spaces](spaces.md) — Indent with spaces\n",
                                          "acme/web/spaces.md": MEM})
        self.repo = self.tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        p = mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.root), "LRDMEM_CONFIG": str(self.tmp / "c.json")})
        p.start()
        self.addCleanup(p.stop)

    def call(self, name, **args):
        r = mcp.handle(rpc("tools/call", {"name": name, "arguments": args}))
        return r["result"]["content"][0]["text"], r["result"].get("isError", False)

    def test_initialize_and_notifications(self):
        r = mcp.handle(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}}))
        res = r["result"]
        self.assertEqual((r["id"], res["protocolVersion"], res["serverInfo"]["name"]), (1, "2025-06-18", "lrdmem"))
        self.assertEqual(res["capabilities"], {"tools": {}})
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(mcp.handle(rpc("ping"))["result"], {})

    def test_an_unknown_client_version_gets_ours(self):
        r = mcp.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(r["result"]["protocolVersion"], mcp.PROTOCOL)

    def test_tools_list_is_read_only_with_schemas(self):
        tools = mcp.handle(rpc("tools/list"))["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["memory_find", "memory_list", "memory_recall", "memory_show", "memory_why"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertEqual(t["annotations"], {"readOnlyHint": True})

    def test_find_show_why_list_return_labelled_text(self):
        for name, args, needle in (("memory_find", {"term": "spaces"}, "acme/web/spaces"),
                                   ("memory_show", {"slug": "spaces"}, "Use spaces."),
                                   ("memory_why", {"slug": "spaces"}, "No experiences or revisions"),
                                   ("memory_list", {}, "spaces")):
            text, err = self.call(name, **args)
            self.assertFalse(err, (name, text))
            self.assertIn(needle, text)
            self.assertTrue(text.startswith(CAVEAT), name)

    def test_recall_needs_a_repository(self):
        text, err = self.call("memory_recall", query="indent")
        self.assertTrue(err)
        self.assertIn("not opted in", text)
        with mock.patch("pathlib.Path.cwd", return_value=self.repo):
            text, err = self.call("memory_recall", query="indent", files=["a.py"])
        self.assertFalse(err, text)
        self.assertIn("spaces", text)

    def test_arguments_never_become_flags_or_paths(self):
        text, err = self.call("memory_find", term="--help")
        self.assertFalse(err)
        self.assertNotIn("usage:", text)
        for name, args in (("memory_show", {"slug": "../x"}), ("memory_show", {"slug": "spaces", "env": "-x"}),
                           ("memory_find", {"term": ""}), ("memory_find", {"term": "x" * 300}),
                           ("memory_list", {"env": "a/b"}), ("memory_recall", {"query": "q", "files": ["--x"]}),
                           ("memory_recall", {"query": "q", "limit": 0})):
            r = mcp.handle(rpc("tools/call", {"name": name, "arguments": args}))
            self.assertEqual(r["error"]["code"], -32602, (name, args))

    def test_json_rpc_errors(self):
        self.assertEqual(mcp.handle(rpc("nope"))["error"]["code"], -32601)
        self.assertEqual(mcp.handle(rpc("tools/call", {"name": "memory_write", "arguments": {}}))["error"]["code"], -32602)
        self.assertEqual(mcp.handle(rpc("tools/call", {"arguments": {}}))["error"]["code"], -32602)
        self.assertEqual(mcp.handle([rpc("ping")])["error"]["code"], -32600)  # batches are not part of MCP
        self.assertEqual(mcp.handle({"jsonrpc": "1.0", "id": 3, "method": "ping"})["error"]["code"], -32600)

    def test_output_is_cleaned_and_capped(self):
        with mock.patch.object(mcp, "_run", return_value=(0, "ok\x1b[31m red\n" + "x" * 30000, "")):
            text, err = self.call("memory_list")
        self.assertNotIn("\x1b", text)
        self.assertLess(len(text), mcp.CAP + len(CAVEAT) + 200)
        self.assertIn("truncated", text)

    def test_serve_answers_one_line_per_request_and_nothing_else(self):
        lines = [json.dumps(rpc("ping")), "not json", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "", json.dumps(rpc("tools/list", id=2))]
        out = io.StringIO()
        self.assertEqual(mcp.serve(io.StringIO("\n".join(lines) + "\n"), out), 0)
        got = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([g.get("id") for g in got], [1, None, 2])
        self.assertEqual(got[1]["error"]["code"], -32700)

    def test_cli_runs_the_server_on_stdio(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(rpc("ping")) + "\n")), redirect_stdout(out):
            self.assertEqual(main(["mcp"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_cli_cwd_flag_points_recall_at_a_repository(self):
        out = io.StringIO()
        req = rpc("tools/call", {"name": "memory_recall", "arguments": {"query": "indent", "files": ["a.py"]}})
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(req) + "\n")), redirect_stdout(out):
            self.assertEqual(main(["mcp", "--cwd", str(self.repo)]), 0)
        self.assertIn("spaces", json.loads(out.getvalue())["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
