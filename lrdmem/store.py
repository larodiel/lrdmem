"""Store model: environments, memories and contexts laid out as Markdown files on disk."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem.frontmatter import split

_IGNORED = {"MEMORY.md", "README.md"}
_KIND = {"user": "fact", "reference": "fact", "feedback": "pattern", "project": "pattern"}
_LINK = re.compile(r"\[\[([^\]]+)\]\]")


class AmbiguousRepo(Exception):
    """Raised when a repo exists in more than one environment and no env was given."""


def _meta(data: dict) -> dict:
    meta = data.get("metadata")
    return meta if isinstance(meta, dict) else {}


@dataclass
class Memory:
    """One memory file; env/repo are None for the scopes that do not name them."""

    slug: str
    path: Path
    env: str | None
    repo: str | None
    data: dict
    body: str

    @property
    def type(self) -> str:
        """The metadata type, or an empty string."""
        return _meta(self.data).get("type", "")

    @property
    def kind(self) -> str:
        """Explicit metadata kind, else inferred from the type."""
        return _meta(self.data).get("kind") or _KIND.get(self.type, "fact")

    @property
    def description(self) -> str:
        """The one-line description."""
        return self.data.get("description", "")

    @property
    def triggers(self) -> dict:
        """Trigger section, empty when absent."""
        t = self.data.get("triggers")
        return t if isinstance(t, dict) else {}

    @property
    def check(self) -> dict | None:
        """Check section, None when absent."""
        c = self.data.get("check")
        return c if isinstance(c, dict) else None

    @property
    def links(self) -> list[str]:
        """Slugs referenced as [[slug]] in the body."""
        return _LINK.findall(self.body)

    @property
    def supersedes(self) -> list[str]:
        """Slugs this memory replaces."""
        s = self.data.get("supersedes", [])
        return s if isinstance(s, list) else [s]


@dataclass
class Context:
    """One context file (a recorded event that reinforces memories)."""

    id: str
    path: Path
    env: str | None
    repo: str | None
    data: dict
    body: str = field(default="")

    @property
    def memories(self) -> list[str]:
        """Slugs of the memories this context reinforces."""
        m = self.data.get("memories", [])
        return m if isinstance(m, list) else [m]

    @property
    def date(self) -> str:
        """The context date, or an empty string."""
        return self.data.get("date", "")


def _read(path: Path) -> tuple[dict, str]:
    data, body = split(path.read_text())
    return data or {}, body


class Store:
    """A memory store rooted at a directory."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def environments(self) -> list[str]:
        """Sorted environment names: top-level dirs (symlinks included) minus _global, bin, dot and _ names."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and p.name != "bin" and not p.name.startswith((".", "_"))
        )

    def folders(self, env: str | None = None) -> list[Path]:
        """Memory folders, _global first; limited to one environment when env is given."""
        out = [self.root / "_global"] if (self.root / "_global").is_dir() else []
        for e in [env] if env else self.environments():
            base = self.root / e
            if base.is_dir():
                out += sorted(p for p in base.iterdir() if p.is_dir() and p.name != "contexts")
        return out

    def _scoped(self, folder: Path) -> tuple[str | None, str | None]:
        rel = folder.relative_to(self.root).parts
        if rel[0] == "_global":
            return None, None
        return rel[0], None if rel[1] == "_global" else rel[1]

    def memories(self) -> list[Memory]:
        """All memories across every folder."""
        out = []
        for folder in self.folders():
            env, repo = self._scoped(folder)
            for p in sorted(folder.glob("*.md")):
                if p.name not in _IGNORED:
                    data, body = _read(p)
                    out.append(Memory(p.stem, p, env, repo, data, body))
        return out

    def contexts(self) -> list[Context]:
        """All contexts, from any folder's contexts/ subfolder."""
        out = []
        for folder in self.folders():
            env, repo = self._scoped(folder)
            for p in sorted((folder / "contexts").glob("*.md")):
                data, body = _read(p)
                out.append(Context(data.get("id", p.stem), p, env, repo, data, body))
        return out

    def memory(self, slug: str, env: str | None) -> Memory | None:
        """The memory with this slug visible in env (its own env or _global), else None."""
        for m in self.memories():
            if m.slug == slug and m.env in (None, env):
                return m
        return None

    def scope_for(self, env: str, repo: str) -> list[Path]:
        """Folders that apply to a repo, broadest first."""
        return [self.root / "_global", self.root / env / "_global", self.root / env / repo]


def repo_key(cwd: Path) -> str | None:
    """Main-checkout directory name for cwd (same from every worktree), None outside git."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(common).parent.name


def resolve(store: Store, cwd: Path, env: str | None = None) -> tuple[str, str] | None:
    """(env, repo) for cwd, or None if the repo has no folder; AmbiguousRepo if several envs hold it."""
    repo = repo_key(cwd)
    if not repo:
        return None
    envs = [e for e in ([env] if env else store.environments()) if (store.root / e / repo).is_dir()]
    if len(envs) > 1:
        raise AmbiguousRepo(f"repo '{repo}' exists in {', '.join(envs)}; pass --env")
    return (envs[0], repo) if envs else None


def store_root() -> Path:
    """Store root from $LRDMEM_STORE; ponytail: Task 9 adds the config fallback."""
    value = os.environ.get("LRDMEM_STORE")
    if not value:
        raise RuntimeError("store not configured: set LRDMEM_STORE")
    return Path(value)
