# lrdmem

A stdlib-only Python CLI that gives AI coding agents a shared, git-backed memory store: markdown memories with frontmatter, injected by hooks when their triggers match. See `docs/specs/2026-10-28-lrdmem-design.md`.

```
pipx install lrdmem
```

Author: Victor Larodiel. MIT licensed.

## Harness support

All three run the same handlers through `lrdmem hook <phase> --agent claude|codex|opencode`.

- **Claude Code**: hooks in `~/.claude/settings.json`; `stop` checks block the Stop (at most twice per session).
- **Codex CLI** (verified on 0.159.0): hooks in `~/.codex/hooks.json`, same payload and output JSON as Claude Code.
  Edits arrive as `apply_patch`; lrdmem uses every file the patch adds or updates. `Stop` can block, so `stop`
  checks behave as in Claude Code.
- **OpenCode** (verified on v2.1.4): copy `lrdmem/hooks/opencode-plugin.js` to `~/.config/opencode/plugins/`.
  Context goes into the system prompt (session start, prompts) or the tool result (pre/post tool). OpenCode has no
  blocking stop, so `check.on: stop` obligations run after each edit, like `post-edit`; the end of a session
  execution only syncs. The plugin stops waiting for lrdmem after 5 s per hook, so a check slower than that
  reports nothing in OpenCode.

`lrdmem install --agent claude|codex|opencode [--dry-run]` wires this up and installs the skill (Claude
`~/.claude/skills/lrdmem/`, Codex `~/.agents/skills/lrdmem/`, OpenCode `~/.config/opencode/skills/lrdmem/`).
OpenCode also reads the Claude and `~/.agents` skill folders, so after installing for several agents it may list
the lrdmem skill twice; that is harmless.
