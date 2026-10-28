"""Command line entry point."""

import argparse
import sys

from lrdmem import __version__
from lrdmem.graph import AmbiguousSlug, Graph
from lrdmem.lint import lint, printable
from lrdmem.store import Context, Memory, Store, store_root


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


def main(argv: list[str] | None = None) -> int:
    """Run the lrdmem CLI and return the process exit code."""
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
    args = parser.parse_args(argv)
    if hasattr(args, "fn"):
        store = Store(store_root())
        return args.fn(store, Graph(store), args)
    if args.command == "lint":
        problems = lint(Store(store_root()), args.env)
        for line in problems:
            print(printable(line))
        print(f"{len(problems)} problem(s)" if problems else "ok", file=sys.stderr)
        return 1 if problems else 0
    return 0
