# AGENTS.md

Instructions for AI coding agents working on the lrdmem repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

> **Asked to install lrdmem for a user, not to work on its code?** Follow
> [Installing lrdmem for a user](#installing-lrdmem-for-a-user) at the end of this file. The rest of the file is
> about developing lrdmem, and its rule against touching the real environment applies only to that work.

## What this is

A stdlib-only Python 3.11+ CLI (`lrdmem`), a skill (`lrdmem/skill/SKILL.md`) and harness hooks that give Claude Code,
Codex and OpenCode one git-backed memory store, shipped as a PyPI package plus Claude Code and Codex plugins.
Memories are Markdown files with a small YAML-subset frontmatter; experiences (`<folder>/experiences/*.md`, legacy
`contexts/`) record what happened, immutably; edges, revisions and the judgment queue live beside them. Design:
`docs/specs/2026-10-28-lrdmem-design.md` and `docs/specs/2026-10-29-lrdmem-v2-memory-model-design.md`. Reference:
`README.md#commands` (every command and option) and `docs/guide.md`.

## Commands

```bash
python3.11 -m unittest discover -s tests                 # full suite (must stay green)
python3.11 -m unittest tests.test_learn                  # one module
python3.11 -W error::ResourceWarning -m unittest discover -s tests   # before you report done
python3.11 -m lrdmem <command> --help                    # run the CLI from the checkout
bash tests/smoke.sh "python3.11 -m lrdmem"               # packaging smoke test
actionlint .github/workflows/*.yml                       # after editing workflows
```

## Map

| Module | Owns |
|---|---|
| `frontmatter.py` | parse/dump the YAML subset; `unsupported()` lines |
| `store.py` | environments, folders, `Memory`/`Context`, `resolve(cwd)`, `store_root()` |
| `graph.py` | memory/context graph, slug resolution, `find`/`hot`/`edges` |
| `triggers.py` | `in_scope`, `matches` (trigger globs/prefixes), `CAVEAT` |
| `recall.py` | `recall` (scored, budgeted; hooks use `for_hook=True`), `render`, `to_json` |
| `lint.py` | store checks, `SECRET`, `printable()` |
| `model.py`, `write.py`, `revision.py` | v2 constants and `today_utc`; the only write path (secret guard); revisions |
| `experience.py`, `propose.py`, `edges.py` | experiences and feedback; validated candidates; typed relations |
| `evidence.py`, `search.py`, `consolidate.py` | confidence/priority on read; BM25+graph search; consolidation and queue |
| `commitmsg.py` | Conventional Commits messages for every lrdmem commit |
| `learn.py`, `pr.py` | writing contexts/memories; PR review ingestion |
| `checks.py`, `config.py` | obligation approval (hash allowlist) and execution; device config |
| `sync.py` | git pull/commit/push per environment repo |
| `hooks/` | harness-neutral `handle()` + `claude`, `codex`, `opencode` adapters and the OpenCode JS plugin |
| `install.py` | merging hooks into harness configs, installing the skill |
| `cli.py` | argparse wiring only |
| `plugins/claude-code`, `plugins/codex` | harness plugins: `hooks/hooks.json` (from `install._EVENTS`), a launcher calling `lrdmem hook`, a copy of the skill |
| `.claude-plugin/`, `.agents/plugins/` | plugin marketplaces for `claude plugin` and `codex plugin` |
| `site/` | the GitHub Pages site, one static `index.html`, deployed by `.github/workflows/pages.yml` |

## Rules

- **Standard library only.** Never add a dependency.
- **Test first**, with tests that fail if the behaviour breaks. Keep tests hermetic: temp dirs, fake `HOME`,
  `LRDMEM_STORE`; git with `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`.
- **Never touch the developer's real environment** while working here: no writes to `~/.claude`, `~/.codex`,
  `~/.agents`, `~/.config/opencode`, `~/.config/lrdmem` or any real memory store; never run `lrdmem install`,
  `sync`, `allow` or hooks against them.
- **Reuse before you write.** Each concern has one home (see the map); call it instead of duplicating logic.
- **Private by default.** `_helpers`; public names get a docstring. CLI flags, hook JSON and the file format are
  public API.
- **Keep the copies in step.** A CLI command or flag change updates `README.md#commands` (`test_cli` fails on any
  undocumented flag). A hook event change in `install._EVENTS` or a skill edit regenerates both plugins and their
  version follows `lrdmem.__version__` (`test_plugins` fails otherwise). Commands or features shown on `site/`
  must match the README.
- **Dot-directories are tracked on purpose.** `.claude-plugin/`, `.codex-plugin/` and `.agents/` are unignored in
  `.gitignore`; check `git status` shows new files in them before you report done.
- **Don't commit, push, tag or open PRs unless the human asks.**

## Reserved names

Repo folders named `contexts`, `experiences`, `edges`, `revisions` or `queue` are reserved: inside a scope folder
they hold v2 entities, and the store never treats them as repos. All git calls on store content go through
`sync._git`/`toplevel` (hardened, run in the trusted env toplevel), never with a cwd inside store content.

## Security invariants

Changes must keep all of these true. A change that weakens one needs an explicit decision from the maintainer.

1. A `check.run` command runs only if its exact text's sha256 is approved for that memory path on this device;
   unapproved, non-printable-ASCII, or `{files}` used quoted or alongside `$(`, `` ` `` or `${` → never runs.
2. File names reach commands only as `sh -c script sh <files>` positional arguments (`{files}` → `"$@"`), names
   starting with `-` prefixed with `./`.
3. `learn` refuses secrets in every field, unknown environments, paths escaping the environment, symlinked targets,
   reserved or invalid slugs, and same-slug shadowing; writes use `O_EXCL|O_NOFOLLOW`.
4. `learn-pr` stores only comments from OWNER/MEMBER/COLLABORATOR on PRs of the checkout's own GitHub remotes.
5. Hooks never raise or exit non-zero (except a Stop block), pass all file-derived text through `printable()`, and
   label injected memory as reference data, not instructions.
6. Hooks commit/push only where autopush is on; sync never autostashes, never force-pushes, never aborts a rebase
   it didn't start, and never commits unmerged paths.
7. Every store write goes through `write.py`, which refuses secrets (also JSON-escaped ones) and symlinked targets;
   experiences are never edited; consolidation changes are evidence-based, idempotent and recorded as revisions.
8. `install` validates every target before writing, backs up edited files, edits symlinked configs through the link,
   and removes only hooks it owns (plus the legacy `bin/memory context` hook).

## Installing lrdmem for a user

Follow these steps when the user asks you to install or set up lrdmem on their machine. Ask before every step that
changes something outside the store, and show the user what you will run. Never skip a question by guessing an
answer that changes their setup.

### 1. Check the machine

```bash
python3 --version          # needs 3.11 or newer; also try python3.11, python3.12, python3.13
git --version
command -v uv pipx         # one of them installs the CLI
command -v claude codex opencode
lrdmem --version           # already installed? then skip step 2
```

No Python 3.11+: stop and tell the user how to get one (Homebrew `brew install python@3.13`, their package
manager, or `uv python install 3.13`). Don't change the system Python.

### 2. Install the CLI

```bash
uv tool install lrdmem --python 3.11                              # preferred
pipx install lrdmem                                               # without uv
uv tool install git+https://github.com/larodiel/lrdmem --python 3.11  # if the PyPI package is not found
lrdmem --version
```

If `lrdmem` is not found afterwards, run `uv tool update-shell` (or `pipx ensurepath`) and tell the user to open
a new terminal.

### 3. The store

Ask the user: **do you already have a memory store (a folder or git repo of memories)?**

- **Yes:** use their path.
- **No:** propose `~/agent-memory` and, once they agree, create it:

```bash
git init ~/agent-memory
mkdir -p ~/agent-memory/_global && touch ~/agent-memory/_global/MEMORY.md
```

Then configure this machine. For the device name, propose the short hostname and let the user change it; it must
differ between their computers.

```bash
lrdmem config set store ~/agent-memory
lrdmem config set device <name>
lrdmem lint
```

Don't add a git remote, push, or turn on autopush unless the user asks; point them to the README's
"Sync between computers" section instead.

### 4. Opt in the user's projects

A repository takes part when the store has `<env>/<repo>/` for it. Ask which environment names they want (for example
`work` and `personal`), and which repositories go in each. The repo name is the main checkout's directory name:

```bash
basename "$(dirname "$(git -C <path-to-repo> rev-parse --path-format=absolute --git-common-dir)")"
```

Create each folder with an empty index:

```bash
mkdir -p ~/agent-memory/<env>/_global ~/agent-memory/<env>/<repo>
touch ~/agent-memory/<env>/_global/MEMORY.md ~/agent-memory/<env>/<repo>/MEMORY.md
```

A repo name may live in only one environment. Never create `contexts`, `experiences`, `edges`, `revisions` or
`queue` as a repo name.

### 5. Connect the agents

For each agent found in step 1, ask whether to connect it. Use **either** the plugin **or** `lrdmem install` for an
agent, never both: both would run every hook twice.

| Agent | Plugin | Without the plugin |
|---|---|---|
| Claude Code | `claude plugin marketplace add larodiel/lrdmem` then `claude plugin install lrdmem@lrdmem` | `lrdmem install --agent claude` |
| Codex | `codex plugin marketplace add larodiel/lrdmem` then `codex plugin add lrdmem@lrdmem`; the user trusts the hooks in `/hooks` | `lrdmem install --agent codex` |
| OpenCode | none | `lrdmem install --agent opencode` |

Before `lrdmem install`, run it with `--dry-run`, show the user the planned changes and wait for their yes. It backs
up every file it edits (`<file>.bak-<date>`). If the dry run says it will remove an existing hook, say which one.

### 6. Verify

From inside an opted-in repository:

```bash
lrdmem lint
lrdmem for <a file in the repo>
echo '{"session_id":"check","cwd":"'"$PWD"'","hook_event_name":"SessionStart","source":"startup"}' \
  | lrdmem hook session-start --agent claude
```

The last command must print JSON naming the repository and environment. Then tell the user to start a new agent
session so the hooks load.

### Never, during an install

- Approve check commands (`lrdmem allow <slug>`) for the user: list them with `lrdmem allow` and let the user read
  and approve each one.
- Put secrets, tokens or personal data in the store or the config.
- Delete or overwrite an existing store, config or harness file. `install` keeps backups; you keep everything else.
- Push, add remotes or turn on autopush without being asked.

### Report

End with: where the store is, the device name, the opted-in `<env>/<repo>` folders, which agents are connected and
how (plugin or `lrdmem install`), any backup files created, and what is left for the user (approvals, remotes,
restarting sessions).
