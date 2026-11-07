"""`lrdmem update`: upgrade the installed CLI through the tool that installed it, or just check PyPI for a newer one.

The only network call is the explicit one to PyPI's JSON endpoint; no hook ever calls it. The installer runs with fixed
arguments (no shell), and the version PyPI answers must be a plain `N.N.N` before it is shown or compared.
"""

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from lrdmem import __version__
from lrdmem.lint import printable

_URL = "https://pypi.org/pypi/lrdmem/json"
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_COMMANDS = {"uv": ["uv", "tool", "upgrade", "lrdmem"], "pipx": ["pipx", "upgrade", "lrdmem"]}
_PLUGINS = "Plugins update separately: `claude plugin update lrdmem@lrdmem` / `codex plugin add lrdmem@lrdmem`."


class UpdateError(Exception):
    """PyPI could not be reached or gave an answer that is not a version."""


def installer(prefix: str | None = None) -> str | None:
    """"uv" or "pipx" when this interpreter lives in a virtualenv one of them manages, else None."""
    parts = Path(prefix or sys.prefix).parts
    for i in range(len(parts) - 1):
        if parts[i:i + 2] == ("uv", "tools"):
            return "uv"
        if parts[i:i + 2] == ("pipx", "venvs"):
            return "pipx"
    return None


def newer(a: str, b: str) -> bool:
    """True when version `a` is higher than `b` (both plain `N.N.N`)."""
    return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))


def latest() -> str:
    """The newest released version on PyPI; raises UpdateError when it cannot be told."""
    try:
        with urllib.request.urlopen(_URL, timeout=5) as r:
            version = json.loads(r.read(1_000_000))["info"]["version"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise UpdateError(f"cannot read the latest version from PyPI: {type(e).__name__}") from e
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise UpdateError("PyPI answered with something that is not a version")
    return version


def run(check: bool) -> int:
    """Check for (exit 1 when there is one, 2 when PyPI cannot be read) or install the newer version; return the exit code."""
    if check:
        try:
            version = latest()
        except UpdateError as e:
            print(printable(f"lrdmem: {e}"), file=sys.stderr)
            return 2
        if newer(version, __version__):
            print(f"lrdmem {version} is available (installed: {__version__}): run `lrdmem update`")
            return 1
        print(f"lrdmem {__version__} is up to date")
        return 0
    kind = installer()
    if kind is None:
        print("lrdmem: not installed by uv or pipx, so it cannot update itself; run `uv tool upgrade lrdmem`, "
              "`pipx upgrade lrdmem` or `pip install -U lrdmem`", file=sys.stderr)
        return 1
    if not shutil.which(kind):
        print(f"lrdmem: `{kind}` is not on PATH", file=sys.stderr)
        return 1
    code = subprocess.run(_COMMANDS[kind], check=False).returncode
    if code == 0:
        print(_PLUGINS)
    return code
