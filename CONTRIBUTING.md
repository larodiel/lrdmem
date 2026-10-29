# Contributing to lrdmem

Thanks for helping. lrdmem is small on purpose: one package, the Python standard library, and tests for everything.

## Setup

```bash
git clone https://github.com/larodiel/lrdmem && cd lrdmem
python3.11 -m unittest discover -s tests   # Python 3.11+, nothing to install
python3.11 -m lrdmem --help                # run the CLI from the checkout
```

Optional: `pipx install -e .` to get an `lrdmem` command that tracks your checkout. Node is used only by the
OpenCode plugin tests, which skip when `node` is missing.

## Ground rules

- **Standard library only.** `dependencies = []` stays empty. If you need a library, open an issue first.
- **Test first.** Write the failing test, watch it fail, then make it pass. Every behaviour change ships with a test
  that would fail without it.
- **Hermetic tests.** Tests never touch your real home, harness configs or memory store: use temp directories, a fake
  `HOME`, `LRDMEM_STORE`, and the suite-wide `LRDMEM_CONFIG` set in `tests/__init__.py`. Tests that run git set
  `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`. Run the suite twice; it must pass both times with no
  warnings (`python3.11 -W error::ResourceWarning -m unittest discover -s tests`).
- **One module per feature.** `store`, `graph`, `triggers`, `learn`, `pr`, `checks`, `sync`, `hooks`, `install`, …
  Reuse what a module already exposes instead of re-deriving it.
- **Private by default.** Helpers are `_prefixed`; make something public only when another module needs it, with a
  docstring. CLI flags, hook output and file formats are public API: changing them needs a migration note.
- **Hooks never fail the harness.** Any error inside a hook prints nothing and exits 0 (a Stop block is the only
  non-empty failure output). Hooks must stay fast: injection under ~300 ms, all network time-boxed.
- **Match the code around you.** Short functions, plain names, comments only where the why isn't obvious.

## Pull requests

1. Branch from `main`, keep the change focused, and describe what and why.
2. Run the full suite and `actionlint` if you touched `.github/workflows/`.
3. Update `README.md` / `docs/guide.md` when behaviour or flags change.
4. CI must be green on Python 3.11–3.13, Linux and macOS.

Changes to the harness adapters (`lrdmem/hooks/`) should say which harness version you verified against and how
(docs, source, or a live run), and add or update the fixtures in `tests/fixtures/<harness>/`.

## Security

lrdmem runs approved commands and writes files that other agents read, so security bugs matter. **Please don't open
a public issue or PR for a vulnerability.** Report it privately through
[GitHub Security Advisories](https://github.com/larodiel/lrdmem/security/advisories/new). The invariants to preserve
are listed in [AGENTS.md](AGENTS.md#security-invariants).

## Releasing (maintainers)

Pushing a tag `vX.Y.Z` (must equal `lrdmem.__version__`) runs the tests, builds and smoke-tests the wheel and the
`.deb`, then publishes to PyPI, attaches `lrdmem_X.Y.Z_all.deb` to the GitHub Release and updates the Homebrew tap.
Nothing publishes unless every smoke test passed. One-time setup:

1. Create the GitHub repo `larodiel/homebrew-tap` (with a `main` branch).
2. On PyPI, add a trusted publisher for project `lrdmem`: owner `larodiel`, repo `lrdmem`, workflow `release.yml`,
   environment `pypi`. Create the `pypi` environment in the repo settings.
3. Create a token with write access to `larodiel/homebrew-tap` and add it to this repo as the secret
   `HOMEBREW_TAP_TOKEN`.
4. Release: bump `lrdmem/__init__.py`, commit, then `git tag vX.Y.Z && git push --tags`.

Workflow actions are pinned to commit SHAs; Dependabot proposes updates.
