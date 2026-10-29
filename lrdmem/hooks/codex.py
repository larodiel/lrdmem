"""Codex CLI adapter (verified against codex-cli 0.159.0 hook schemas).

Codex hooks take Claude Code's stdin payload and output JSON (hookSpecificOutput.additionalContext,
Stop {"decision": "block"}), so this reuses the Claude adapter. The one difference: file edits arrive as
tool_name "apply_patch" with the patch text in tool_input.command; they map to an edit of the patch's
first added or updated file.
"""

from lrdmem.hooks import HookInput, patch_path
from lrdmem.hooks.claude import emit  # noqa: F401  (same output format)
from lrdmem.hooks.claude import parse as _claude_parse


def parse(event: str, payload: dict) -> HookInput:
    """HookInput for a CLI phase from a Codex hook payload; event "" when it is a no-op."""
    ti = payload.get("tool_input")
    if payload.get("tool_name") == "apply_patch":
        patch = ti.get("command") if isinstance(ti, dict) else None
        path = patch_path(patch) if isinstance(patch, str) else None
        payload = {**payload, "tool_name": "Edit", "tool_input": {"file_path": path}}
    return _claude_parse(event, payload)
