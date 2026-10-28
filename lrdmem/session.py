"""Per-session state kept in the temp dir so hooks can share it across processes."""

import json
import re
import tempfile
from pathlib import Path


class Session:
    """Injected slugs, edited files and Stop-block count for one agent session."""

    def __init__(self, id: str = ""):
        self.id = id
        self.injected: set[str] = set()
        self.edited: list[str] = []
        self.stop_blocks = 0
        path = self._path()
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.injected = {s for s in data["injected"] if isinstance(s, str)}
                self.edited = [s for s in data["edited"] if isinstance(s, str)]
                self.stop_blocks = int(data["stop_blocks"])
            except (OSError, ValueError, KeyError, TypeError):
                self.injected, self.edited, self.stop_blocks = set(), [], 0  # corrupt: start empty

    def _path(self) -> Path | None:
        if not self.id:
            return None
        # sanitised so a hostile id cannot escape the state dir
        return Path(tempfile.gettempdir()) / "lrdmem" / (re.sub(r"[^A-Za-z0-9_-]", "_", self.id) + ".json")

    def save(self) -> None:
        """Write the state to disk; a no-op for an empty id."""
        path = self._path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "injected": sorted(self.injected), "edited": self.edited, "stop_blocks": self.stop_blocks,
        }), encoding="utf-8")
