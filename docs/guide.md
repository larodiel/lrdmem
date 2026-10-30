# lrdmem guide

The full reference. For the pitch and quick start, see the [README](../README.md).

## Commands

```
lrdmem find TERM | show SLUG | why SLUG | hot | stale     # read the store
lrdmem recall [QUERY] [--file F] [--command C] [--json]    # most relevant memories, with signals and relations
lrdmem for FILE [--command CMD]                           # what a hook injects for a file or command
lrdmem learn ... | learn-pr URL --agent NAME              # record corrections (active right away)
lrdmem experience add --kind K --agent A TEXT             # record what happened (immutable)
lrdmem associate [QUERY] [--file F]                       # memories related to a candidate, with signals
lrdmem memory propose --agent me < payload.json          # validated candidates from an experience
lrdmem feedback REF useful|incorrect|irrelevant           # signal on a recalled memory
lrdmem consolidate [--queue | --resolve ITEM DECISION]    # automatic consolidation and the judgment queue
lrdmem revert REVISION                                    # undo one recorded change
lrdmem lint | graph --format html|json|dot -o OUT         # check and visualise the store
lrdmem allow [SLUG] | check --on stop|post-edit --files F...  # approve and run obligation checks
lrdmem sync [ENV]                                         # commit, pull and push the store
lrdmem config set store PATH | set device NAME | autopush on|off [--env ENV] | show
lrdmem install --agent claude|codex|opencode [--dry-run]  # wire hooks and the skill
```

## Memory model

- **Experience** (`<scope>/experiences/`): an immutable observation — error, decision, discovery, outcome, execution,
  correction, review, feedback or check. Legacy `contexts/` files read as corrections.
- **Memory** (`<scope>/<slug>.md`): the reusable representation. `metadata.memory_type` is episodic, semantic,
  procedural, hypothesis, prospective or metacognitive; `metadata.state` is candidate, active, contested, superseded
  or obsolete; `evidence: [{ref, kind, weight}]` lists the experiences behind it (supports, refutes, observed_in).
  Confidence and priority are computed on read, never stored. Memories without the new fields read as semantic and
  active, and their legacy contexts count as supporting evidence.
- **Relation** (`<scope>/edges/`): a typed edge (related_to, derived_from, occurred_in, supports, contradicts,
  caused_by, resolved_by, generalizes, supersedes) with a state (proposed, validated, invalidated, superseded) and
  evidence. `[[links]]` read as validated related_to.
- **Revision** (`<scope>/revisions/`): every relevant change, with before, after and reason; `lrdmem revert` undoes
  one.

## Lifecycle

1. `lrdmem experience add` records what happened before anything else (a `--request-id` makes retries no-ops).
2. The agent reads `lrdmem associate` and proposes candidates with `lrdmem memory propose` (JSON on stdin:
   `{"experience": ref, "candidates": [...]}`); the code validates types, states, scope, sizes and secrets before
   writing. Agent-extracted candidates start as `candidate`; a hypothesis is never stored as a fact; a user
   correction (`learn`, `learn-pr`) starts `active`.
3. Classification: `new`, `equivalent` (adds evidence, no new memory), `complementary` (proposed related_to or
   generalizes edge), `contradicts` (proposed contradicts edge; only a correction can contest an active memory),
   `insufficient`.
4. `lrdmem consolidate` runs at the end of each task (20 s, resumable): expired memories become obsolete, exact
   duplicates merge into the oldest, candidates with 2 independent supports become active, refuted memories become
   contested (and come back when supports dominate), edges with 2 independent evidences are validated. Hypotheses
   with outcome evidence, repeated experiences without a procedure and paraphrase suspects go to the judgment queue
   for an agent to resolve (`--queue`, `--resolve`). Independence means a different agent, device or day; access
   counts and repetition never promote.
5. `lrdmem feedback` adds a signal (useful supports, incorrect refutes, irrelevant lowers priority); one negative
   never changes a state. Hooks record each obligation check as a `check` experience.

Recall (`lrdmem recall`, and hooks before edits and commands) combines lexical search (accent-aware BM25), file and
command entities, triggers, up to 2 hops in the graph, state, confidence, validity and priority. Hooks inject only
active and contested memories; explicit recall also shows candidates and hypotheses, labelled as not confirmed.

Every commit lrdmem makes follows Conventional Commits: `feat(memory): learn …`, `refactor(memory): consolidate —
merge N, promote N, contest N`, `revert(memory): <revision>`, `chore(memory): sync <env> from <device>`.

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

Before a new memory is created, `learn` refuses near-duplicates (it lists them; `--new` overrides) and any slug that
already exists in a visible scope. It refuses secrets in every field, home paths and links that leave the
environment, and never writes through symlinks.

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
most twice per session). `{files}` becomes the matching files, passed as arguments, never spliced into the command,
and must appear as a bare unquoted word. A check never runs until you approve its exact text on this device:
`lrdmem allow` lists the pending ones, `lrdmem allow SLUG` approves one, and any edit of `check.run` needs approving
again. Only printable-ASCII commands can be approved. An obligation in `<env>/_global/` runs in every repo of that
environment (narrowed by `triggers.repos` when set). `lrdmem check --on stop|post-edit --files F...` runs the
approved checks by hand. Hooks give all post-edit checks 8 s together (4 s in OpenCode) and stop checks 300 s.

## Syncing

`lrdmem sync [ENV]` commits and pushes each environment repository and the store root. Hooks never commit unless
you turn on `lrdmem config autopush on [--env ENV]` for this device: then session start pulls (3 s at most) and
the end of a task commits and pushes, skipping the network when there is nothing to push. Sync never autostashes,
never force-pushes, never touches a repo with a rebase in progress, and stops on conflicts without resolving them.

## Store layout

```
<store>/_global/MEMORY.md             # facts about you, every repo
<store>/<env>/_global/MEMORY.md       # company-wide (only trigger-injected with triggers.repos)
<store>/<env>/<repo>/MEMORY.md        # one repo; contexts in <folder>/contexts/<YYYY-MM-DD>-<slug>.md
```

An environment is any top-level directory except `_global`, `bin` and names starting with `.` or `_`; symlinked
directories count, and an environment may be its own git repository. A repo takes part only if `<env>/<repo>/` exists.
`<repo>` is the directory name of the repository's main checkout, so every worktree and clone maps to the same folder.

Frontmatter is a small YAML subset: `key: value`, one level of nested keys, and inline lists `[a, b]`. `lrdmem lint`
flags anything else (block lists, comments, `|`/`>` scalars).

## Harness support

All three run the same handlers through `lrdmem hook <phase> --agent claude|codex|opencode`.

- **Claude Code**: hooks in `~/.claude/settings.json`; `stop` checks block the Stop (at most twice per session).
- **Codex CLI** (verified on 0.159.0): hooks in `~/.codex/hooks.json`, same payload and output JSON as Claude Code.
  Edits arrive as `apply_patch`; lrdmem uses every file the patch adds or updates. `Stop` can block, so `stop`
  checks behave as in Claude Code. Trust the hooks once with `/hooks` in Codex.
- **OpenCode** (verified on v2.1.4): `lrdmem install --agent opencode` copies the plugin to
  `~/.config/opencode/plugins/lrdmem.js`. Context goes into the system prompt (session start, prompts) or the tool
  result (pre/post tool). OpenCode has no blocking stop, so `check.on: stop` obligations run after each edit, like
  `post-edit`; the end of a session execution only syncs. The plugin stops waiting for lrdmem after 5 s per hook,
  so a check slower than that reports nothing in OpenCode.

`lrdmem install --agent claude|codex|opencode [--dry-run]` wires this up and installs the skill (Claude
`~/.claude/skills/lrdmem/`, Codex `~/.agents/skills/lrdmem/`, OpenCode `~/.config/opencode/skills/lrdmem/`). It backs
up every file it edits, keeps every hook it didn't write, and edits symlinked configs through the link. OpenCode also
reads the Claude and `~/.agents` skill folders, so after installing for several agents it may list the lrdmem skill
twice; that is harmless.

## Migrating from bin/memory

An existing `bin/memory` store works as is: same layout, no `kind` or contexts needed, `[[links]]` kept, and
`bin/` and the root `README.md` are ignored. Point lrdmem at it with `lrdmem config set store <path>` (or
`LRDMEM_STORE`), run `lrdmem lint`, then `lrdmem install --agent claude|codex|opencode` to replace the old hooks.
