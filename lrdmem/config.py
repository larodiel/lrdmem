"""Per-device configuration: store path, device name, autopush flags and the check allowlist."""

import json
import os
import socket
import tempfile
from pathlib import Path

_corrupt: set[Path] = set()  # config files load() found unreadable as JSON objects; save() never overwrites them


class ConfigError(Exception):
    """Raised by save() when the config file on disk is corrupt and would be lost."""


def _path() -> Path:
    return Path(os.environ.get("LRDMEM_CONFIG") or Path.home() / ".config" / "lrdmem" / "config.json")


def load() -> dict:
    """The config with defaults filled in; a missing or corrupt file yields the defaults (and save() then refuses)."""
    cfg = {"store": None, "device": socket.gethostname(), "autopush": {}, "allow": {}}
    path = _path()
    _corrupt.discard(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        _corrupt.add(path)
        return cfg
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
    """Atomically write the config (file 0600, parent dir 0700); unknown keys are kept as given.

    Raises ConfigError instead of overwriting a file that load() found corrupt.
    """
    path = _path()
    if path in _corrupt:
        raise ConfigError(f"{path} is not a valid JSON object; fix or remove it, then retry (nothing was written)")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.name == "lrdmem" and path.parent.stat().st_uid == os.getuid():  # never tighten e.g. $HOME
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # mode 0o600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
