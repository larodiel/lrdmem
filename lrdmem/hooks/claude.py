"""Claude Code adapter: map hook payloads to handler events and handler results to Claude's hook JSON."""

import json
import os
import re
from datetime import date
from pathlib import Path

from lrdmem.hooks import HookInput, HookOutput, repo_path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_EVENT_NAMES = {"session-start": "SessionStart", "pre-edit": "PreToolUse", "pre-command": "PreToolUse",
                "post-edit": "PostToolUse", "prompt": "UserPromptSubmit"}


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def parse(event: str, payload: dict) -> HookInput:
    """HookInput for a CLI phase (session-start, pre-tool, post-tool, prompt, stop); event "" when it is a no-op."""
    cwd = Path(_str(payload.get("cwd")) or os.getcwd())
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    inp = HookInput("", _str(payload.get("session_id")) or "", cwd)
    if event in ("session-start", "prompt"):
        inp.event = event
        inp.prompt = _str(payload.get("prompt"))
    elif event == "stop":
        inp.event, inp.stop_active = "stop", payload.get("stop_hook_active") is True
    elif event in ("pre-tool", "post-tool") and tool in EDIT_TOOLS:
        inp.event = "pre-edit" if event == "pre-tool" else "post-edit"
        inp.path = repo_path(cwd, _str(ti.get("file_path")) or _str(ti.get("notebook_path")))
    elif event == "pre-tool" and tool == "Bash":
        inp.event, inp.command = "pre-command", _str(ti.get("command"))
    return inp


def emit(inp: HookInput, out: HookOutput) -> str:
    """Claude hook JSON for a handler result; "" when there is nothing to say."""
    if out.block_reason is not None and inp.event == "stop":
        return json.dumps({"decision": "block", "reason": out.block_reason})
    if not out.context:
        return ""
    if inp.event == "stop":
        return json.dumps({"systemMessage": out.context})
    name = _EVENT_NAMES.get(inp.event)
    return json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": out.context}}) if name else ""


def link_claude_memory(cwd: Path, repo_dir: Path) -> None:
    """Point Claude's per-path auto-memory dir at repo_dir; an existing symlink is kept, a non-empty dir is backed up."""
    mem = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / "memory"
    if mem.is_symlink():
        return
    mem.parent.mkdir(parents=True, exist_ok=True)
    if mem.is_dir():
        if any(mem.iterdir()):
            base = f"memory.bak-{date.today().isoformat()}"
            backup, n = mem.with_name(base), 1
            while backup.exists() or backup.is_symlink():
                n += 1
                backup = mem.with_name(f"{base}-{n}")
            mem.rename(backup)
        else:
            mem.rmdir()
    mem.symlink_to(repo_dir)
