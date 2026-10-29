"""Store model: environments, memories, experiences, edges, revisions and queue items laid out as Markdown files."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lrdmem import config
from lrdmem.frontmatter import split
from lrdmem.model import EDGE_STATES, MEMORY_TYPES, STATES, Evidence, v01_memory_type

IGNORED = {"MEMORY.md", "README.md"}
_KIND = {"user": "fact", "reference": "fact", "feedback": "pattern", "project": "pattern"}
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
SUPPORTED_FORMAT = 2
FORMAT_FILE = ".lrdmem-format"
SUBFOLDERS = ("contexts", "experiences", "edges", "revisions", "queue")


class StoreNotConfigured(RuntimeError):
    """Raised by store_root() when neither $LRDMEM_STORE nor the config names a store."""


class StoreFormatError(RuntimeError):
    """Raised when the store's format marker is newer than this version supports, or is unusable."""


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


def _ref(env: str | None, repo: str | None, *tail: str) -> str:
    """Store-relative reference of a node, without the .md suffix."""
    return "/".join(["_global"] if env is None else [env, repo or "_global"]) + "".join(f"/{t}" for t in tail)


def _evidence(data: dict) -> list[Evidence]:
    raw = data.get("evidence")
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(Evidence.from_dict(item))
        except (ValueError, AttributeError):
            pass  # lint reports the invalid items
    return out


def _float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


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

    @property
    def memory_type(self) -> str:
        """Explicit metadata.memory_type if valid, else mapped from the v0.1 metadata.kind."""
        explicit = _str(_meta(self.data).get("memory_type"))
        return explicit if explicit in MEMORY_TYPES else v01_memory_type(_str(_meta(self.data).get("kind")))

    @property
    def state(self) -> str:
        """Explicit metadata.state if valid, else active."""
        explicit = _str(_meta(self.data).get("state"))
        return explicit if explicit in STATES else "active"

    @property
    def valid_from(self) -> str:
        """First day the memory holds, or an empty string."""
        return _str(_meta(self.data).get("valid_from"))

    @property
    def valid_until(self) -> str:
        """Last day the memory holds, or an empty string."""
        return _str(_meta(self.data).get("valid_until"))

    @property
    def created(self) -> str:
        """Creation date, or an empty string."""
        return _str(_meta(self.data).get("created"))

    @property
    def evidence(self) -> list[Evidence]:
        """The valid evidence items; invalid ones are skipped."""
        return _evidence(self.data)

    @property
    def ref(self) -> str:
        """Store-relative path without .md."""
        return _ref(self.env, self.repo, self.slug)


@dataclass
class _Entry:
    """A non-memory store file: id, location, parsed frontmatter and body."""

    id: str
    path: Path
    env: str | None
    repo: str | None
    data: dict
    body: str = field(default="")

    @property
    def ref(self) -> str:
        """Store-relative path without .md."""
        return _ref(self.env, self.repo, self.path.parent.name, self.path.stem)


class Experience(_Entry):
    """An immutable recorded event; a v0.1 context is an experience of kind correction."""

    @property
    def kind(self) -> str:
        """The experience kind; only a legacy context (contexts/) may omit it and is then a correction."""
        return _str(self.data.get("kind")) or ("correction" if self.path.parent.name == "contexts" else "")

    @property
    def memories(self) -> list[str]:
        """Slugs of the memories this (legacy) context reinforces."""
        return _list(self.data.get("memories"))

    @property
    def date(self) -> str:
        """The experience date, or an empty string."""
        return _str(self.data.get("date"))

    @property
    def agent(self) -> str:
        """The agent that recorded it."""
        return _str(self.data.get("agent"))

    @property
    def device(self) -> str:
        """The device it was recorded on."""
        return _str(self.data.get("device"))

    @property
    def files(self) -> list[str]:
        """Files involved."""
        return _list(self.data.get("files"))

    @property
    def source(self) -> str:
        """Where it came from: chat, pr, run or check."""
        return _str(self.data.get("source"))

    @property
    def request_id(self) -> str:
        """Idempotency key."""
        return _str(self.data.get("request_id"))


Context = Experience  # v0.1 name


class Edge(_Entry):
    """A typed relation between two nodes."""

    @property
    def source(self) -> str:
        """Ref of the origin node."""
        return _str(self.data.get("source"))

    @property
    def target(self) -> str:
        """Ref of the destination node."""
        return _str(self.data.get("target"))

    @property
    def type(self) -> str:
        """The edge type."""
        return _str(self.data.get("type"))

    @property
    def state(self) -> str:
        """The edge state; proposed when absent or invalid."""
        explicit = _str(self.data.get("state"))
        return explicit if explicit in EDGE_STATES else "proposed"

    @property
    def weight(self) -> float:
        """Recall relevance, 1.0 when absent or invalid."""
        return _float(self.data.get("weight"), 1.0)

    @property
    def evidence(self) -> list[Evidence]:
        """The valid evidence items; invalid ones are skipped."""
        return _evidence(self.data)

    @property
    def superseded_by(self) -> str:
        """Id of the edge replacing this one."""
        return _str(self.data.get("superseded_by"))


class Revision(_Entry):
    """A semantic change record of a memory or edge."""

    @property
    def target(self) -> str:
        """Ref of the changed node."""
        return _str(self.data.get("target"))

    @property
    def action(self) -> str:
        """The revision action."""
        return _str(self.data.get("action"))

    @property
    def before(self) -> dict:
        """Old values of the changed fields."""
        v = self.data.get("before")
        return v if isinstance(v, dict) else {}

    @property
    def after(self) -> dict:
        """New values of the changed fields."""
        v = self.data.get("after")
        return v if isinstance(v, dict) else {}

    @property
    def reason(self) -> str:
        """Why it changed."""
        return _str(self.data.get("reason"))

    @property
    def by(self) -> str:
        """Who changed it: consolidate or an agent."""
        return _str(self.data.get("by"))

    @property
    def date(self) -> str:
        """The revision date."""
        return _str(self.data.get("date"))


class QueueItem(_Entry):
    """A judgment-queue item awaiting (or holding) a resolution."""

    @property
    def kind(self) -> str:
        """The kind of judgment asked for."""
        return _str(self.data.get("kind"))

    @property
    def subjects(self) -> list[str]:
        """Refs the judgment is about."""
        return _list(self.data.get("subjects"))

    @property
    def status(self) -> str:
        """open or resolved."""
        return "resolved" if self.data.get("status") == "resolved" else "open"

    @property
    def resolution(self) -> str:
        """The recorded resolution."""
        return _str(self.data.get("resolution"))


_NODE_DIRS = {"experiences": Experience, "contexts": Experience, "edges": Edge, "revisions": Revision}


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
                    (p for p in base.iterdir() if p.is_dir() and p.name not in SUBFOLDERS
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

    def _entries(self, sub: str, cls: type[_Entry]) -> list:
        out = []
        for folder in self.folders():
            env, repo = self.scope_of(folder)
            for p in sorted((folder / sub).glob("*.md")):
                read = _read(p)
                if read:
                    out.append(cls(_str(read[0].get("id")) or p.stem, p, env, repo, *read))
        return out

    def contexts(self) -> list[Experience]:
        """Legacy v0.1 contexts, from any folder's contexts/ subfolder."""
        return self._entries("contexts", Experience)

    def experiences(self) -> list[Experience]:
        """All experiences: experiences/ files, then legacy contexts (kind correction)."""
        return self._entries("experiences", Experience) + self.contexts()

    def edges(self) -> list[Edge]:
        """All stored edges (the [[links]] of memory bodies are implicit and not listed)."""
        return self._entries("edges", Edge)

    def revisions(self) -> list[Revision]:
        """All revisions."""
        return self._entries("revisions", Revision)

    def queue(self) -> list[QueueItem]:
        """All judgment-queue items."""
        return self._entries("queue", QueueItem)

    def node(self, ref: str) -> "Memory | Experience | Edge | Revision | None":
        """The node at a store-relative ref without .md (work/app/phpcs, work/app/experiences/x), else None."""
        parts = ref.split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        if parts[0] == "_global":
            scope, rest = parts[:1], parts[1:]
        else:
            scope, rest = parts[:2], parts[2:]
            if (len(scope) < 2 or scope[0] not in self.environments() or scope[1] in SUBFOLDERS
                    or (scope[1] != "_global" and scope[1].startswith((".", "_")))):
                return None
        if len(rest) == 1:
            sub, name, cls = "", rest[0], None
        elif len(rest) == 2 and rest[0] in _NODE_DIRS:
            sub, name, cls = rest[0], rest[1], _NODE_DIRS[rest[0]]
        else:
            return None
        folder = self.root.joinpath(*scope)
        path = folder / sub / f"{name}.md"
        read = None if f"{name}.md" in IGNORED else _read(path)
        if not read:
            return None
        env, repo = self.scope_of(folder)
        if cls is None:
            return Memory(name, path, env, repo, *read)
        return cls(_str(read[0].get("id")) or name, path, env, repo, *read)

    def format(self) -> int:
        """The store format: the marker's number, 1 when there is no marker; StoreFormatError if unusable."""
        marker = self.root / FORMAT_FILE
        if marker.is_symlink():
            raise StoreFormatError(f"{marker} is a symlink")
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return 1
        except (OSError, ValueError) as err:
            raise StoreFormatError(f"{marker} is unreadable ({err.__class__.__name__})") from err
        if not re.fullmatch(r"[1-9][0-9]*", text):
            raise StoreFormatError(f"{marker} must hold a positive whole number")
        return int(text)

    def ensure_format(self) -> None:
        """Write the marker if absent; raise StoreFormatError when the store is newer than SUPPORTED_FORMAT."""
        found = self.format()
        if found > SUPPORTED_FORMAT:
            raise StoreFormatError(
                f"store format {found} is newer than this lrdmem supports ({SUPPORTED_FORMAT}); upgrade lrdmem")
        marker = self.root / FORMAT_FILE
        if os.path.lexists(marker):
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        tmp = self.root / f"{FORMAT_FILE}.{os.getpid()}.tmp"
        try:
            os.unlink(tmp)  # stale leftover of a crashed run; unlink never follows a symlink
        except FileNotFoundError:
            pass
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{SUPPORTED_FORMAT}\n")
            try:
                os.link(tmp, marker)  # atomic create-if-absent; never follows a symlink at marker
            except FileExistsError:
                pass
            except OSError:  # filesystem without hard links: exclusive create in place
                try:
                    fd = os.open(marker, flags, 0o644)
                except FileExistsError:
                    return
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{SUPPORTED_FORMAT}\n")
        finally:
            os.unlink(tmp)

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
