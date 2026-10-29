# lrdmem

A stdlib-only Python CLI that gives AI coding agents a shared, git-backed memory store: markdown memories with frontmatter, injected by hooks when their triggers match. See `docs/specs/2026-10-28-lrdmem-design.md`.

```
pipx install lrdmem
```

## Usage

```
lrdmem find TERM | show SLUG | why SLUG | hot | stale     # read the store
lrdmem for FILE [--command CMD]                           # memories that apply to a file or command
lrdmem learn --help | sync | lint | graph --format html|json|dot -o OUT
```

## Store layout

```
<store>/_global/MEMORY.md             # facts about you, every repo
<store>/<env>/_global/MEMORY.md       # company-wide (only trigger-injected with triggers.repos)
<store>/<env>/<repo>/MEMORY.md        # one repo; contexts in <folder>/contexts/<YYYY-MM-DD>-<slug>.md
```

An environment is any top-level directory except `_global`, `bin` and names starting with `.` or `_`; symlinked
directories count, and an environment may be its own git repository. A repo takes part only if `<env>/<repo>/` exists.

## Migrating from bin/memory

An existing `bin/memory` store works as is: same layout, no `kind` or contexts needed, `[[links]]` kept, and
`bin/` and the root `README.md` are ignored. Point lrdmem at it with `lrdmem config set store <path>` (or
`LRDMEM_STORE`), run `lrdmem lint`, then `lrdmem install --agent claude|codex|opencode` to replace the old hooks.

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
