"""Command line entry point."""

import argparse
import sys

from lrdmem import __version__
from lrdmem.lint import lint
from lrdmem.store import Store, store_root


def main(argv: list[str] | None = None) -> int:
    """Run the lrdmem CLI and return the process exit code."""
    parser = argparse.ArgumentParser(prog="lrdmem", description="Shared memory store for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"lrdmem {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("lint", help="check the store for problems").add_argument("env", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "lint":
        problems = lint(Store(store_root()), args.env)
        for line in problems:
            print(line)
        print(f"{len(problems)} problem(s)" if problems else "ok", file=sys.stderr)
        return 1 if problems else 0
    return 0
