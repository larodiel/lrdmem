"""Command line entry point."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from lrdmem import __version__, checks, config
from lrdmem.export import to_dot, to_html, to_json
from lrdmem.graph import AmbiguousSlug, Graph
from lrdmem.hooks import HookInput, dispatch, handle
from lrdmem.install import AGENTS, InstallError, install
from lrdmem.learn import LearnRefused, learn
from lrdmem.lint import lint, printable
from lrdmem.pr import learn_pr
from lrdmem.session import Session
from lrdmem.stale import stale
from lrdmem.store import AmbiguousRepo, Context, Memory, Store, StoreNotConfigured, resolve, store_root
from lrdmem.sync import sync
from lrdmem.triggers import render, select


def _out(text: str = "") -> None:
    for line in text.splitlines() or [""]:
        print(printable(line))


def _rel(store: Store, item: Memory | Context) -> str:
    return item.path.relative_to(store.root).with_suffix("").as_posix()


def _first(c: Context) -> str:
    return next((ln for ln in c.body.splitlines() if ln.strip()), "")


def _context_line(c: Context) -> str:
    return f"{c.date} {c.data.get('agent', '')} {c.data.get('source', '')} — {_first(c)}"


def _find(store: Store, g: Graph, args) -> int:
    for item in g.find(args.term, args.env):
        if isinstance(item, Memory):
            _out(f"{item.kind} {_rel(store, item)} ({g.strength(item)}) — {item.description}")
            rel = g.related(item)
            if rel:
                _out(f"  related: {', '.join(rel)}")
        else:
            _out(f"context {_rel(store, item)} — {_first(item)}")
    return 0


def _pick(store: Store, g: Graph, args) -> Memory | int:
    try:
        m = g.resolve(args.slug, args.env)
    except AmbiguousSlug as e:
        print(printable(f"{e}: " + ", ".join(_rel(store, c) for c in e.candidates)), file=sys.stderr)
        return 2
    if not m:
        print(printable(f"unknown memory '{args.slug}'"), file=sys.stderr)
        return 1
    return m


def _show(store: Store, g: Graph, args) -> int:
    m = _pick(store, g, args)
    if isinstance(m, int):
        return m
    _out(f"{m.slug} [{m.type}/{m.kind}] {_rel(store, m)}")
    _out(m.description)
    _out(m.body.strip())
    _out("Contexts:")
    for c in g.contexts_of(m):
        _out(f"  {_context_line(c)}")
    _out(f"Related: {', '.join(g.related(m))}")
    return 0


def _why(store: Store, g: Graph, args) -> int:
    m = _pick(store, g, args)
    if isinstance(m, int):
        return m
    for c in g.contexts_of(m):
        _out(_context_line(c))
        _out(c.body.strip())
        _out()
    return 0


def _hot(store: Store, g: Graph, args) -> int:
    for m, n in g.hot(args.min):
        _out(f"{_rel(store, m)}  {n}" + ("  [candidate: obligation]" if m.kind == "pattern" else ""))
    return 0


def _repo_relative(file: str | None) -> str | None:
    if not file:
        return file
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd(), capture_output=True,
                             text=True, check=True).stdout.strip()
        return (Path.cwd() / file).resolve().relative_to(Path(top).resolve()).as_posix()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return file


def _git_root() -> Path | None:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd(), capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(top) if top else None


def _stale(store: Store, g: Graph, args) -> int:
    found = stale(store, _git_root())
    for m, path in found:
        _out(f"{_rel(store, m)}: {path}")
    return 1 if found else 0


def _graph(store: Store, g: Graph, args) -> int:
    text = {"html": to_html, "dot": to_dot, "json": lambda g: json.dumps(to_json(g), indent=2)}[args.format](g)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        _out(text)
    return 0


def _for(store: Store, g: Graph, args) -> int:
    try:
        scope = resolve(store, Path.cwd(), args.env)
    except AmbiguousRepo as e:
        print(printable(str(e)), file=sys.stderr)
        return 2
    if not scope:
        return 0
    path = _repo_relative(args.file)
    session = Session(args.session or "")
    picked = select(store, *scope, paths=[path] if path else [], command=args.command, exclude=session.injected)
    if picked:
        _out(render(picked, path or args.command or ""))
        session.injected |= {m.slug for m in picked}
        session.save()
    return 0


def _csv(values: list[str] | None) -> list[str]:
    return [v.strip() for value in values or [] for v in value.split(",") if v.strip()]


def _learn(store: Store, g: Graph, args) -> int:
    try:
        r = learn(store, Path.cwd(), memory=args.memory, mistake=args.mistake, correction=args.correction,
                  scope=args.scope, agent=args.agent, source=args.source, files=args.files, pr=args.pr,
                  kind=args.kind, description=args.description, body=args.body, why=args.why, how=args.how,
                  paths=_csv(args.paths), commands=_csv(args.commands), repos=_csv(args.repos), env=args.env,
                  new=args.new,
                  device=config.load()["device"])
    except LearnRefused as e:
        print(printable(str(e)), file=sys.stderr)
        for slug in e.duplicates:
            print(printable(slug), file=sys.stderr)
        return 1
    _out(r.context.relative_to(store.root).as_posix())
    if r.created:
        _out(r.memory.relative_to(store.root).as_posix())
    return 0


def _learn_pr(store: Store, g: Graph, args) -> int:
    try:
        r = learn_pr(store, Path.cwd(), args.url, args.agent, env=args.env, device=config.load()["device"])
    except subprocess.CalledProcessError as e:
        print(printable(f"gh failed: {(e.stderr or '').strip() or e}"), file=sys.stderr)
        return 1
    except (ValueError, LookupError, AmbiguousRepo, RuntimeError, OSError) as e:
        print(printable(str(e)), file=sys.stderr)
        return 2 if isinstance(e, ValueError) else 1
    for url in r.already:
        _out(f"already linked {url}")
    for url, rel in r.linked:
        _out(f"linked {url} -> {rel}")
    for c in r.unmatched:
        _out(c["url"])
        note = f" (not stored: {c['note']})" if c.get("note") else ""
        _out(f"  {c['path'] + ': ' if c['path'] else ''}{next(iter(c['body'].splitlines()), '')}{note}")
    return 0


def _config(args) -> int:
    cfg = config.load()
    if args.action == "set":
        cfg[args.key] = str(Path(args.value).resolve()) if args.key == "store" else args.value
    elif args.action == "autopush":
        cfg["autopush"][args.env or "*"] = args.state == "on"
    if args.action == "show":
        _out(json.dumps(cfg, indent=2))
    else:
        config.save(cfg)
    return 0


def _allow(store: Store, g: Graph, args) -> int:
    cfg = config.load()
    if not args.slug:
        for m in checks.pending(store, cfg):
            why = checks.problem(m)
            _out(f"{checks.key(store, m)}: {m.check['run']}" + (f"  [{why}]" if why else ""))
        _out("Only approve commands you have read.")
        return 0
    by_path = [m for m in store.memories() if checks.key(store, m) == args.slug]
    m = by_path[0] if by_path else _pick(store, g, args)
    if isinstance(m, int):
        return m
    if not checks.command_text(m):
        print(printable(f"'{args.slug}' has no check.run to approve"), file=sys.stderr)
        return 1
    why = checks.problem(m)
    if why:
        print(printable(f"'{args.slug}': {why}"), file=sys.stderr)
        return 1
    _out(f"{checks.key(store, m)}: {m.check['run']}")
    _out(f"sha256 {checks.command_hash(m)}")
    _out(m.path.as_posix())
    cfg["allow"][checks.key(store, m)] = checks.command_hash(m)
    config.save(cfg)
    _out("Approved. Only approve commands you have read.")
    return 0


def _check(store: Store, g: Graph, args) -> int:
    try:
        scope = resolve(store, Path.cwd(), args.env)
    except AmbiguousRepo as e:
        print(printable(str(e)), file=sys.stderr)
        return 2
    root = _git_root()
    if not scope or not root:
        return 0
    files = [_repo_relative(f) for f in args.files]
    r = checks.run_checks(store, *scope, root, args.on, files, config.load())
    for slug, tail in r.failures:
        _out(f"lrdmem check failed: {slug}")
        _out(tail)
    for slug, why in r.unrunnable:
        _out(f"lrdmem check skipped: {slug} ({why})")
    for slug in r.skipped_pending:
        _out(f"lrdmem check skipped: {slug} is not approved. Run `lrdmem allow {slug}` after reading its command.")
    return 1 if r.failures else 0


def _sync(store: Store, g: Graph, args) -> int:
    results = sync(store, args.env)
    for r in results:
        if r.error:
            _out(f"{r.env}: {'committed, ' if r.committed else ''}error: {r.error}")
        else:
            _out(f"{r.env}: {'committed' if r.committed else 'nothing to commit'}, "
                 f"{'pushed' if r.pushed else 'not pushed (no remote)'}")
    return 1 if any(r.error for r in results) else 0


def _install(args) -> int:
    try:
        changes = install(args.agent, Path.home(), args.dry_run)
    except InstallError as e:
        print(printable(str(e)), file=sys.stderr)
        return 1
    if args.dry_run:
        _out("Dry run: nothing was written. Planned changes:")
    for line in changes:
        _out(line)
    if not changes:
        _out("Already installed; nothing to change.")
    elif args.agent == "codex":
        _out("In Codex, run /hooks once to review and trust the new lrdmem hooks.")
    _out("Obligations run only after 'lrdmem allow'.")
    return 0


def _context(agent: str) -> int:
    """`lrdmem context`: hook JSON when a harness pipes stdin, the plain session-start text for a human."""
    if not sys.stdin.isatty():
        return _hook("session-start", agent)
    try:
        text = handle(HookInput("session-start", "", Path.cwd())).context
    except Exception:
        return 0
    if text:
        _out(text)
    return 0


def _hook(phase: str, agent: str) -> int:
    try:
        text = dispatch(phase, agent, sys.stdin.read())
    except Exception:  # unreadable stdin: hooks stay silent
        return 0
    if text:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the lrdmem CLI and return the process exit code."""
    try:
        return _main(argv)
    except (config.ConfigError, StoreNotConfigured) as e:
        print(printable(f"lrdmem: {e}"), file=sys.stderr)
        return 1


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="lrdmem", description="Shared memory store for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"lrdmem {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("lint", help="check the store for problems").add_argument("env", nargs="?")
    for name, fn in (("find", _find), ("show", _show), ("why", _why), ("hot", _hot)):
        p = sub.add_parser(name, help=f"{name} memories")
        p.set_defaults(fn=fn)
        if name == "find":
            p.add_argument("term")
            p.add_argument("--env")
        elif name == "hot":
            p.add_argument("--min", type=int, default=3)
        else:
            p.add_argument("slug")
            p.add_argument("--env")
    p = sub.add_parser("stale", help="list memories citing paths that no longer exist")
    p.set_defaults(fn=_stale)
    p = sub.add_parser("graph", help="export the memory graph")
    p.set_defaults(fn=_graph)
    p.add_argument("--format", choices=["html", "json", "dot"], default="html")
    p.add_argument("-o", "--output")
    p = sub.add_parser("for", help="print memories triggered by a file or command")
    p.set_defaults(fn=_for)
    p.add_argument("file", nargs="?")
    p.add_argument("--command")
    p.add_argument("--session")
    p.add_argument("--env")
    p = sub.add_parser("learn", help="record a correction as a context linked to a memory")
    p.set_defaults(fn=_learn)
    p.add_argument("--memory", required=True)
    p.add_argument("--mistake", required=True)
    p.add_argument("--correction", required=True)
    p.add_argument("--scope", choices=["repo", "env", "global"], required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--source", choices=["chat", "pr"], default="chat")
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--pr")
    p.add_argument("--kind", choices=["obligation", "pattern", "fact"])
    p.add_argument("--description")
    p.add_argument("--body")
    p.add_argument("--why", help="new memory: why the rule exists")
    p.add_argument("--how", help="new memory: how to apply it")
    for name in ("paths", "commands", "repos"):
        p.add_argument(f"--{name}", action="append", help=f"new memory: triggers.{name} (repeat or comma-separate)")
    p.add_argument("--env")
    p.add_argument("--new", action="store_true")
    p = sub.add_parser("learn-pr", help="link a GitHub PR's review comments to the memories they reinforce")
    p.set_defaults(fn=_learn_pr)
    p.add_argument("url")
    p.add_argument("--agent", required=True)
    p.add_argument("--env")
    p = sub.add_parser("config", help="show or change this device's configuration")
    csub = p.add_subparsers(dest="action", required=True)
    q = csub.add_parser("set", help="set store (path) or device (name)")
    q.add_argument("key", choices=["store", "device"])
    q.add_argument("value")
    q = csub.add_parser("autopush", help="commit and push automatically for an environment (default: all)")
    q.add_argument("state", choices=["on", "off"])
    q.add_argument("--env")
    csub.add_parser("show", help="print the configuration as JSON")
    p = sub.add_parser("allow", help="approve a memory's check command to run on this device. "
                                     "Only approve commands you have read.")
    p.set_defaults(fn=_allow)
    p.add_argument("slug", nargs="?", help="memory slug or store-relative path; omit to list pending commands")
    p.add_argument("--env")
    p = sub.add_parser("check", help="run the approved checks of obligations matching the given files")
    p.set_defaults(fn=_check)
    p.add_argument("--on", choices=["stop", "post-edit"], required=True)
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--env")
    p = sub.add_parser("sync", help="commit and push the store (all environments, or one)")
    p.set_defaults(fn=_sync)
    p.add_argument("env", nargs="?")
    p = sub.add_parser("hook", help="run a harness hook (JSON on stdin, harness JSON on stdout)")
    p.add_argument("phase", help="session-start, pre-tool, post-tool, prompt or stop")
    p.add_argument("--agent", default="claude", help="claude, codex or opencode")
    sub.add_parser("context", help="same as `hook session-start`").add_argument(
        "--agent", default="claude", help="claude, codex or opencode")
    p = sub.add_parser("install", help="install lrdmem's hooks and skill into a harness's user config")
    p.add_argument("--agent", choices=AGENTS, required=True)
    p.add_argument("--dry-run", action="store_true", help="print the planned changes and write nothing")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return _hook(args.phase, args.agent)
    if args.command == "context":
        return _context(args.agent)
    if args.command == "config":
        return _config(args)
    if args.command == "install":
        return _install(args)
    if hasattr(args, "fn"):
        store = Store(store_root())
        return args.fn(store, Graph(store), args)
    if args.command == "lint":
        problems = lint(Store(store_root()), args.env, git_checks=True)
        for line in problems:
            print(printable(line))
        print(f"{len(problems)} problem(s)" if problems else "ok", file=sys.stderr)
        return 1 if problems else 0
    return 0
