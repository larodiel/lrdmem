"""Per-session state kept in the temp dir so hooks can share it across processes."""

import json
import os
import re
import stat
import tempfile
from pathlib import Path


def state_dir() -> Path | None:
    """Private per-user state dir `$TMPDIR/lrdmem-<uid>/`, or None when it cannot be trusted (symlink, wrong owner)."""
    d = Path(tempfile.gettempdir()) / f"lrdmem-{os.getuid()}"
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = d.lstat()
    except OSError:
        return None
    return d if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() else None


class Session:
    """Injected memory refs, edited files and Stop-block count for one agent session."""

    def __init__(self, id: str = ""):
        self.id = id
        self.injected: set[str] = set()
        self.edited: list[str] = []
        self.stop_blocks = 0
        path = self._path()
        if path:
            try:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    raise ValueError("untrusted state file")
                data = json.loads(path.read_text(encoding="utf-8"))
                self.injected = {s for s in data["injected"] if isinstance(s, str)}
                self.edited = [s for s in data["edited"] if isinstance(s, str)]
                self.stop_blocks = int(data["stop_blocks"])
            except (OSError, ValueError, KeyError, TypeError):
                self.injected, self.edited, self.stop_blocks = set(), [], 0  # corrupt: start empty

    def _path(self) -> Path | None:
        d = state_dir() if self.id else None
        # sanitised so a hostile id cannot escape the state dir
        return d / (re.sub(r"[^A-Za-z0-9_-]", "_", self.id) + ".json") if d else None

    def save(self) -> None:
        """Atomically write the state to disk; a no-op for an empty id, never raises."""
        path = self._path()
        if not path:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # mode 0o600
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"injected": sorted(self.injected), "edited": self.edited,
                           "stop_blocks": self.stop_blocks}, f)
            os.replace(tmp, path)
        except OSError:
            pass  # state just isn't persisted
