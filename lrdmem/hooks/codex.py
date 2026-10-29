"""Codex CLI adapter (verified against codex-cli 0.159.0 hook schemas).

Codex hooks take Claude Code's stdin payload and output JSON (hookSpecificOutput.additionalContext,
Stop {"decision": "block"}), so this reuses the Claude adapter. The one difference: file edits arrive as
tool_name "apply_patch" with the patch text in tool_input.command; they map to an edit of every file the
patch adds or updates.
"""

from lrdmem.hooks import HookInput, patch_paths, repo_paths
from lrdmem.hooks.claude import emit  # noqa: F401  (same output format)
from lrdmem.hooks.claude import parse as _claude_parse


def parse(event: str, payload: dict) -> HookInput:
    """HookInput for a CLI phase from a Codex hook payload; event "" when it is a no-op."""
    if payload.get("tool_name") != "apply_patch":
        return _claude_parse(event, payload)
    ti = payload.get("tool_input")
    patch = ti.get("command") if isinstance(ti, dict) else None
    inp = _claude_parse(event, {**payload, "tool_name": "Edit", "tool_input": {}})
    inp.paths = repo_paths(inp.cwd, patch_paths(patch)) if inp.event and isinstance(patch, str) else []
    return inp
