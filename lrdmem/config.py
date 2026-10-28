"""Per-device configuration: store path, device name, autopush flags and the check allowlist."""

import json
import os
import socket
import tempfile
from pathlib import Path


def _path() -> Path:
    return Path(os.environ.get("LRDMEM_CONFIG") or Path.home() / ".config" / "lrdmem" / "config.json")


def load() -> dict:
    """The config with defaults filled in; a missing or corrupt file yields the defaults."""
    cfg = {"store": None, "device": socket.gethostname(), "autopush": {}, "allow": {}}
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        cfg.update(data)
    for key in ("autopush", "allow"):
        if not isinstance(cfg[key], dict):
            cfg[key] = {}
    if not isinstance(cfg["store"], str):
        cfg["store"] = None
    if not isinstance(cfg["device"], str) or not cfg["device"]:
        cfg["device"] = socket.gethostname()
    return cfg


def save(cfg: dict) -> None:
    """Atomically write the config (file 0600, parent dir 0700); unknown keys are kept as given."""
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # mode 0o600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise
