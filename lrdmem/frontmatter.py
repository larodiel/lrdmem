"""Parser and writer for the small YAML subset used in memory frontmatter.

Grammar: top-level `key: value`; a `key:` with an empty value opens a section
of indented `key: value` lines; values may be quoted or inline lists; list items may be flat inline maps `{k: v}`
(nested maps/lists inside a map stay raw strings, quoted on dump).
"""

import json

_SPECIAL = (",", "]", ":", " ", "'", '"', "{")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return json.loads(value)
            except ValueError:
                pass
        return value[1:-1]
    return value


def _split_top(inner: str, sep: str) -> list[str]:
    """Split on sep outside quotes and outside nested {} / []."""
    items, cur, quote, depth = [], "", "", 0
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == sep and depth <= 0:
            items.append(cur)
            cur = ""
            continue
        cur += ch
    items.append(cur)
    return items


def _item(raw: str):
    """A list item: an inline map `{k: v, ...}` (flat; nested values stay raw strings) or a scalar."""
    if raw.startswith("{") and raw.endswith("}"):
        pairs = [p.partition(":") for p in _split_top(raw[1:-1], ",") if p.strip()]
        if all(sep for _, sep, _ in pairs):
            return {k.strip(): _unquote(v.strip()) for k, _, v in pairs}
    return _unquote(raw)


def _split_list(inner: str) -> list:
    return [_item(i.strip()) for i in _split_top(inner, ",") if i.strip()]


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return _split_list(raw[1:-1])
    return _unquote(raw)


def _parts(text: str) -> tuple[str, str] | None:
    """(header, body) of LF-normalised text, or None without frontmatter."""
    if not text.startswith("---\n"):
        return None
    rest = text[4:]
    if rest.startswith("---"):
        return ("", rest[3:].lstrip("\n")) if rest[3:4] in ("", "\n") else None
    end = rest.find("\n---")
    while end != -1 and rest[end + 4:end + 5] not in ("", "\n"):
        end = rest.find("\n---", end + 1)
    return None if end == -1 else (rest[:end], rest[end + 5:])


def unsupported(text: str) -> list[int]:
    """1-based file line numbers of frontmatter lines outside the grammar: block lists, comments, block scalars."""
    parts = _parts(text.replace("\r\n", "\n"))
    if not parts or not parts[0]:
        return []
    out = []
    for n, line in enumerate(parts[0].split("\n"), 2):
        s = line.strip()
        if s and (s.startswith(("-", "#")) or ":" not in s or s.partition(":")[2].strip()[:1] in ("|", ">")):
            out.append(n)
    return out


def split(text: str) -> tuple[dict | None, str]:
    """Split text into (frontmatter data, body); data is None without frontmatter."""
    text = text.replace("\r\n", "\n")
    parts = _parts(text)
    if parts is None:
        return None, text
    head, body = parts
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


def _scalar(i: str) -> str:
    return json.dumps(i) if not i or any(c in i for c in _SPECIAL) else i


def _fmt(value) -> str:
    if isinstance(value, list):
        items = ["{" + ", ".join(f"{k}: {_scalar(str(v))}" for k, v in i.items()) + "}" if isinstance(i, dict)
                 else _scalar(i) for i in value]
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
