"""Parser and writer for the small YAML subset used in memory frontmatter.

Grammar: top-level `key: value`; a `key:` with an empty value opens a section
of indented `key: value` lines; values may be quoted or inline lists.
"""

import json

_SPECIAL = (",", "]", ":", " ", "'", '"')


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return json.loads(value)
            except ValueError:
                pass
        return value[1:-1]
    return value


def _split_list(inner: str) -> list[str]:
    items, cur, quote = [], "", ""
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [_unquote(i.strip()) for i in items if i.strip()]


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return _split_list(raw[1:-1])
    return _unquote(raw)


def split(text: str) -> tuple[dict | None, str]:
    """Split text into (frontmatter data, body); data is None without frontmatter."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, text
    rest = text[4:]
    if rest.startswith("---"):
        head, body = "", rest[3:].lstrip("\n") if rest[3:4] in ("", "\n") else None
        if body is None:
            return None, text
    else:
        end = rest.find("\n---")
        while end != -1 and rest[end + 4:end + 5] not in ("", "\n"):
            end = rest.find("\n---", end + 1)
        if end == -1:
            return None, text
        head, body = rest[:end], rest[end + 5:]
    data: dict = {}
    section = None
    for line in head.split("\n"):
        if not line.strip():
            continue
        key, _, raw = line.strip().partition(":")
        if line[0] in " \t" and section is not None:
            section[key.strip()] = _value(raw)
        elif raw.strip() == "":
            section = data[key.strip()] = {}
        else:
            section = None
            data[key.strip()] = _value(raw)
    return data, body


def parse(text: str) -> dict | None:
    """Return the frontmatter of text as a dict, or None when absent."""
    return split(text)[0]


def _fmt(value) -> str:
    if isinstance(value, list):
        items = [json.dumps(i) if not i or any(c in i for c in _SPECIAL) else i for i in value]
        return "[" + ", ".join(items) + "]"
    text = str(value)
    if isinstance(value, str) and (not text or "\n" in text or text[0] in "\"'[" or text != text.strip()):
        return json.dumps(text)
    return text


def dump(data: dict) -> str:
    """Serialise data to a frontmatter block (no trailing newline)."""
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            lines += [f"  {k}: {_fmt(v)}" for k, v in value.items()]
        else:
            lines.append(f"{key}: {_fmt(value)}")
    lines.append("---")
    return "\n".join(lines)
