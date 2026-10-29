"""OpenCode adapter: parse the JSON opencode-plugin.js sends and return plain text for it to inject.

The plugin writes one JSON object to stdin per call:

    {"session_id": str, "cwd": str, "tool": str, "input": object}

`tool` and `input` are only sent for pre-tool and post-tool: OpenCode v2's tool name and arguments
(edit/write: {"path"}, patch: {"patchText"}, shell: {"command"}). stdout is the context text, "" for none.
OpenCode has no blocking Stop, so stop checks run at post-edit and stop only syncs (README "Harness support").
"""

import os
from pathlib import Path

from lrdmem.hooks import HookInput, HookOutput, patch_paths, repo_paths
from lrdmem.hooks.claude import _str

EDIT_TOOLS = {"edit", "write", "patch"}


def parse(event: str, payload: dict) -> HookInput:
    """HookInput for a CLI phase from the plugin's JSON; event "" when it is a no-op."""
    cwd = Path(_str(payload.get("cwd")) or os.getcwd())
    tool = payload.get("tool")
    args = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    inp = HookInput("", _str(payload.get("session_id")) or "", cwd, edit_time_stop=True)
    if event in ("session-start", "prompt", "stop"):
        inp.event = event
    elif event in ("pre-tool", "post-tool") and tool in EDIT_TOOLS:
        inp.event = "pre-edit" if event == "pre-tool" else "post-edit"
        raw = patch_paths(_str(args.get("patchText")) or "") if tool == "patch" else [_str(args.get("path"))]
        inp.paths = repo_paths(cwd, raw)
    elif event == "pre-tool" and tool == "shell":
        inp.event, inp.command = "pre-command", _str(args.get("command"))
    return inp


def emit(inp: HookInput, out: HookOutput) -> str:
    """The context as plain text; a Stop never blocks here (edit_time_stop), so block_reason is never set."""
    return out.context
