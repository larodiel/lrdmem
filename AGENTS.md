# AGENTS.md

Instructions for AI coding agents working on the lrdmem repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

A stdlib-only Python 3.11+ CLI (`lrdmem`), a skill (`lrdmem/skill/SKILL.md`) and harness hooks that give Claude Code,
Codex and OpenCode one git-backed memory store. Memories are Markdown files with a small YAML-subset frontmatter;
contexts (`<folder>/contexts/*.md`) record the incidents behind them. Design: `docs/specs/2026-10-28-lrdmem-design.md`.
Reference: `docs/guide.md`.

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
| `triggers.py` | `matches`, `select` (budgeted injection), `render` |
| `lint.py` | store checks, `SECRET`, `printable()` |
| `learn.py`, `pr.py` | writing contexts/memories; PR review ingestion |
| `checks.py`, `config.py` | obligation approval (hash allowlist) and execution; device config |
| `sync.py` | git pull/commit/push per environment repo |
| `hooks/` | harness-neutral `handle()` + `claude`, `codex`, `opencode` adapters and the OpenCode JS plugin |
| `install.py` | merging hooks into harness configs, installing the skill |
| `cli.py` | argparse wiring only |

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
7. `install` validates every target before writing, backs up edited files, edits symlinked configs through the link,
   and removes only hooks it owns (plus the legacy `bin/memory context` hook).
