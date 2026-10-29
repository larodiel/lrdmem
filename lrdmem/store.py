"""Store model: environments, memories and contexts laid out as Markdown files on disk."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem import config
from lrdmem.frontmatter import split

IGNORED = {"MEMORY.md", "README.md"}
_KIND = {"user": "fact", "reference": "fact", "feedback": "pattern", "project": "pattern"}
_LINK = re.compile(r"\[\[([^\]]+)\]\]")


class StoreNotConfigured(RuntimeError):
    """Raised by store_root() when neither $LRDMEM_STORE nor the config names a store."""


class AmbiguousRepo(Exception):
    """Raised when a repo exists in more than one environment and no env was given."""


def _list(value) -> list:
    if value is None or value == {}:  # an empty `key:` parses as {}
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else [value]


def _str(value) -> str:
    return value if isinstance(value, str) else ""


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
        return _str(_meta(self.data).get("type"))

    @property
    def kind(self) -> str:
        """Explicit metadata kind, else inferred from the type."""
        return _meta(self.data).get("kind") or _KIND.get(self.type, "fact")

    @property
    def description(self) -> str:
        """The one-line description."""
        return _str(self.data.get("description"))

    @property
    def triggers(self) -> dict:
        """Trigger section, empty when absent."""
        t = self.data.get("triggers")
        return {k: _list(v) if isinstance(v, list) else v for k, v in t.items()} if isinstance(t, dict) else {}

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
        return _list(self.data.get("supersedes"))


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
        return _list(self.data.get("memories"))

    @property
    def date(self) -> str:
        """The context date, or an empty string."""
        return _str(self.data.get("date"))


def _read(path: Path) -> tuple[dict, str] | None:
    try:
        data, body = split(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
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
                out += sorted(
                    (p for p in base.iterdir() if p.is_dir() and p.name != "contexts"
                     and (p.name == "_global" or not p.name.startswith((".", "_")))),
                    key=lambda p: (p.name != "_global", p.name),
                )
        return out

    def scope_of(self, folder: Path) -> tuple[str | None, str | None]:
        """(env, repo) a folder belongs to; None for the scopes that do not name them."""
        rel = folder.relative_to(self.root).parts
        if rel[0] == "_global":
            return None, None
        return rel[0], None if rel[1] == "_global" else rel[1]

    def memories(self) -> list[Memory]:
        """All memories across every folder."""
        out = []
        for folder in self.folders():
            env, repo = self.scope_of(folder)
            for p in sorted(folder.glob("*.md")):
                if p.name not in IGNORED:
                    read = _read(p)
                    if read:
                        out.append(Memory(p.stem, p, env, repo, *read))
        return out

    def contexts(self) -> list[Context]:
        """All contexts, from any folder's contexts/ subfolder."""
        out = []
        for folder in self.folders():
            env, repo = self.scope_of(folder)
            for p in sorted((folder / "contexts").glob("*.md")):
                read = _read(p)
                if read:
                    out.append(Context(_str(read[0].get("id")) or p.stem, p, env, repo, *read))
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
    """Store root: $LRDMEM_STORE, else the configured store; RuntimeError when neither is set."""
    value = os.environ.get("LRDMEM_STORE") or config.load()["store"]
    if not value:
        raise StoreNotConfigured("no store configured — run 'lrdmem config set store <path>' or set LRDMEM_STORE")
    return Path(value)
