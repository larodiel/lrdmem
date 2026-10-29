# lrdmem

A stdlib-only Python CLI that gives AI coding agents a shared, git-backed memory store: markdown memories with frontmatter, injected by hooks when their triggers match. See `docs/specs/2026-10-28-lrdmem-design.md`.

```
pipx install lrdmem
```

## Usage

```
lrdmem find TERM | show SLUG | why SLUG | hot | stale     # read the store
lrdmem for FILE [--command CMD]                           # memories that apply to a file or command
lrdmem learn --help | learn-pr URL | sync | lint | graph --format html|json|dot -o OUT
lrdmem allow [SLUG] | check --on stop|post-edit --files F...  # approve and run obligation checks
lrdmem config set store PATH | config autopush on|off [--env ENV] | config show
```

## Recording lessons

`lrdmem learn` writes a context (the incident) and links it to a memory (the rule), creating the memory when the slug
is new. A new memory needs `--description`, `--body`, `--why` and `--how` (written as the **Why:** and
**How to apply:** lines lint expects), and takes its triggers from `--paths` (globs relative to the repo root),
`--commands` (command prefixes) and `--repos`; each can be repeated or comma-separated. A memory in `<env>/_global/`
is only injected in the repos its `--repos` names.

```
lrdmem learn --scope repo --agent claude --memory php-tabs --mistake "indented with spaces" \
  --correction "user asked for tabs" --description "PHP is indented with tabs" --body "Indent PHP with tabs." \
  --why "house style" --how "configure the editor for tabs in *.php" --paths '**/*.php'
```

`lrdmem learn-pr https://github.com/OWNER/REPO/pull/N --agent claude` records each review comment as a context of the
memory it matches best and lists the ones that match none. Only comments by the repository's owners, members and
collaborators are stored (bots are skipped), the PR must belong to a GitHub remote of the current checkout and to its
opted-in repo, and a comment already recorded by an earlier run is reported as `already linked` instead of twice.

## Obligations

A memory with `metadata.kind: obligation` can carry a check that hooks run for you:

```
---
name: phpcs-clean
description: PHP passes phpcs
metadata:
  type: feedback
  kind: obligation
  author: me
  updated: 2026-10-28
triggers:
  paths: ['**/*.php']
check:
  on: stop
  run: vendor/bin/phpcs {files}
---
```

`check.on` is `post-edit` (after each matching edit) or `stop` (at the end of the task; a failure blocks the Stop at
most twice per session). `{files}` becomes the matching files, passed as arguments, never spliced into the command.
A check never runs until you approve its exact text on this device: `lrdmem allow` lists the pending ones,
`lrdmem allow SLUG` approves one, and any edit of `check.run` needs approving again. An obligation in
`<env>/_global/` runs in every repo of that environment (narrowed by `triggers.repos` when set).
`lrdmem check --on stop|post-edit --files F...` runs the approved checks by hand. Hooks give all post-edit checks
8 s together (4 s in OpenCode) and stop checks 300 s.

## Syncing

`lrdmem sync [ENV]` commits and pushes each environment repository and the store root. Hooks never commit unless
you turn on `lrdmem config autopush on [--env ENV]` for this device: then session start pulls (3 s at most) and
the end of a task commits and pushes, skipping the network when there is nothing to push.

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
- **OpenCode** (verified on v2.1.4): `lrdmem install --agent opencode` copies the plugin to
  `~/.config/opencode/plugins/lrdmem.js`. Context goes into the system prompt (session start, prompts) or the tool result (pre/post tool). OpenCode has no
  blocking stop, so `check.on: stop` obligations run after each edit, like `post-edit`; the end of a session
  execution only syncs. The plugin stops waiting for lrdmem after 5 s per hook, so a check slower than that
  reports nothing in OpenCode.

`lrdmem install --agent claude|codex|opencode [--dry-run]` wires this up and installs the skill (Claude
`~/.claude/skills/lrdmem/`, Codex `~/.agents/skills/lrdmem/`, OpenCode `~/.config/opencode/skills/lrdmem/`).
OpenCode also reads the Claude and `~/.agents` skill folders, so after installing for several agents it may list
the lrdmem skill twice; that is harmless.

## Releasing

Pushing a tag `vX.Y.Z` (must equal `lrdmem.__version__`) builds and smoke-tests the sdist and wheel, publishes to PyPI,
attaches `lrdmem_X.Y.Z_all.deb` to the GitHub Release and updates the Homebrew tap. One-time setup:

1. Create the GitHub repo `larodiel/homebrew-tap` (with a `main` branch).
2. On PyPI, add a trusted publisher for project `lrdmem`: owner `larodiel`, repo `lrdmem`, workflow `release.yml`,
   environment `pypi`. Create the `pypi` environment in the repo settings.
3. Create a token with write access to `larodiel/homebrew-tap` and add it to this repo as the secret `HOMEBREW_TAP_TOKEN`.
4. Release: `git tag vX.Y.Z && git push --tags`.
