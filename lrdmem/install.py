"""Wire lrdmem's hooks and skill into a harness's user config (`lrdmem install`)."""

import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

_HERE = Path(__file__).parent
_SKILL = _HERE / "skill" / "SKILL.md"
_PLUGIN = _HERE / "hooks" / "opencode-plugin.js"
_BIN_MARKER = 'process.env.LRDMEM_BIN || "lrdmem"'
_OLD_TOOL = "bin/memory context"
_EDITS = "Edit|Write|MultiEdit|NotebookEdit"
# (event, matcher, phase); Codex reports file edits as apply_patch (Edit/Write are only matcher aliases there).
_EVENTS = {
    "claude": [("SessionStart", "startup|resume|clear|compact", "session-start"),
               ("PreToolUse", f"{_EDITS}|Bash", "pre-tool"), ("PostToolUse", _EDITS, "post-tool"),
               ("UserPromptSubmit", None, "prompt"), ("Stop", None, "stop")],
    "codex": [("SessionStart", "startup|resume|clear|compact", "session-start"),
              ("PreToolUse", "apply_patch|Bash", "pre-tool"), ("PostToolUse", "apply_patch", "post-tool"),
              ("UserPromptSubmit", None, "prompt"), ("Stop", None, "stop")],
}
_HOOKS_FILE = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}
# ~/.codex/skills is deprecated in codex-rs (host_roots.rs); ~/.agents/skills is the user root.
_SKILL_DIR = {"claude": ".claude/skills/lrdmem", "codex": ".agents/skills/lrdmem",
              "opencode": ".config/opencode/skills/lrdmem"}
AGENTS = tuple(_SKILL_DIR)


class InstallError(Exception):
    """Raised when install cannot proceed safely; nothing has been written."""


def _bin() -> str:
    """The lrdmem executable running now, as an absolute path, or plain `lrdmem`."""
    argv0 = sys.argv[0] if Path(sys.argv[0]).name == "lrdmem" else None
    found = argv0 or shutil.which("lrdmem")
    return os.path.abspath(found) if found else "lrdmem"


def _ours(cmd: str, agent: str) -> bool:
    try:
        first = shlex.split(cmd)[0]
    except (ValueError, IndexError):
        return False
    return " hook " in cmd and f"--agent {agent}" in cmd and Path(first).name == "lrdmem"


def _drop(hook, agent: str) -> bool:
    cmd = hook.get("command") if isinstance(hook, dict) else None
    return isinstance(cmd, str) and (_OLD_TOOL in cmd or _ours(cmd, agent))


def _merge(groups: list, want: dict, agent: str) -> list:
    """Drop our hooks and the old tool's; put `want` where the first emptied group was, else at the end."""
    out, placed = [], False
    for g in groups:
        hooks = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(hooks, list) or not any(_drop(h, agent) for h in hooks):
            out.append(g)
            continue
        kept = [h for h in hooks if not _drop(h, agent)]
        if kept:
            out.append({**g, "hooks": kept})
        elif not placed:
            out.append(want)
            placed = True
    return out if placed else out + [want]


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise InstallError(f"{path} is not valid JSON ({e}); fix it and run install again") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise InstallError(f"{path} does not hold a JSON object with a 'hooks' object; nothing changed")
    return data


def _hooks_plan(agent: str, path: Path, command: str) -> tuple[bytes | None, list[str]]:
    data = _load(path)
    hooks = data.get("hooks", {})
    new, notes = dict(hooks), []
    for event, matcher, phase in _EVENTS[agent]:
        cmd = f"{command} hook {phase} --agent {agent}"
        want = ({"matcher": matcher} if matcher else {}) | {"hooks": [{"type": "command", "command": cmd}]}
        old = hooks.get(event, [])
        if not isinstance(old, list):
            raise InstallError(f"{path}: hooks.{event} is not a list; nothing changed")
        new[event] = _merge(old, want, agent)
        notes += [f"{path}: {event} remove `{h['command']}`" for g in old if isinstance(g, dict)
                  for h in g.get("hooks") or [] if _drop(h, agent) and h != want["hooks"][0]]
        if want not in old:
            notes.append(f"{path}: {event} add `{cmd}`" + (f" (matcher {matcher})" if matcher else ""))
    if new == hooks:
        return None, []
    text = json.dumps({**data, "hooks": new}, indent=2, ensure_ascii=False) + "\n"
    return text.encode(), notes


def _write(path: Path, content: bytes, backup: bool) -> None:
    """Atomically replace path (keeping its mode), backing it up once per day when asked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    if backup and path.exists():
        bak = path.with_name(f"{path.name}.bak-{date.today().isoformat()}")
        if not bak.exists():
            shutil.copy2(path, bak)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install(agent: str, home: Path, dry_run: bool = False) -> list[str]:
    """Install lrdmem's hooks (or plugin) and skill for agent under home; return the changes made or planned.

    Replaces earlier lrdmem hooks and any `bin/memory context` hook, leaves every other entry alone, and
    returns [] when everything is already in place. Raises InstallError, before writing anything, when an
    existing config file cannot be parsed.
    """
    if agent not in AGENTS:
        raise InstallError(f"unknown agent '{agent}' (use {', '.join(AGENTS)})")
    command = shlex.quote(_bin())
    plan: list[tuple[Path, bytes, bool, list[str]]] = []  # (target, content, back up first, change notes)
    if agent in _HOOKS_FILE:
        path = (home / _HOOKS_FILE[agent]).resolve()  # a symlinked config is edited through its target
        content, notes = _hooks_plan(agent, path, command)
        if content is not None:
            plan.append((path, content, True, notes or [f"{path}: rewrite"]))
    else:
        plugin = _PLUGIN.read_text(encoding="utf-8").replace(
            _BIN_MARKER, f"process.env.LRDMEM_BIN || {json.dumps(_bin())}")
        plan.append((home / ".config/opencode/plugins/lrdmem.js", plugin.encode(), False, []))
    plan.append((home / _SKILL_DIR[agent] / "SKILL.md", _SKILL.read_bytes(), False, []))
    plan = [p for p in plan if not (p[0].is_file() and p[0].read_bytes() == p[1])]
    if not dry_run:
        for path, content, backup, _ in plan:
            _write(path, content, backup)
    return [note for path, _, _, notes in plan for note in (notes or [f"write {path}"])]
