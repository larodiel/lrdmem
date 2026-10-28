"""Shared test helpers."""

from pathlib import Path


def make_store(tmp: Path, files: dict[str, str]) -> Path:
    """Create a store under tmp/store from a {relative path: text} map and return its root."""
    root = tmp / "store"
    root.mkdir()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root
