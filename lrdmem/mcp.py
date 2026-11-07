"""A read-only Model Context Protocol server over stdio, so apps without hooks (a chat window) can use the store.

JSON-RPC 2.0, one message per line. The tools are fixed, read-only calls of the CLI itself (`find`, `show`, `why`,
`list`, `recall`): every argument is validated, never becomes a flag or a path, and every answer is labelled as
reference data. There is no write tool: recording memories stays with the hooks and `lrdmem learn`.
"""

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from typing import Callable, TextIO

from lrdmem.lint import printable
from lrdmem.triggers import CAVEAT

PROTOCOL = "2025-06-18"
SUPPORTED = {PROTOCOL, "2025-03-26", "2024-11-05"}
CAP = 20_000  # characters of one tool answer

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ENV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class _Invalid(Exception):
    """Arguments that fail validation: answered as JSON-RPC invalid params."""


def _string(args: dict, key: str, required: bool = True, limit: int = 200) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise _Invalid(f"{key} must be a non-empty string of at most {limit} characters")
    return value


def _matching(args: dict, key: str, pattern: re.Pattern, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise _Invalid(f"{key} is not a valid {key}")
    return value


def _env(args: dict, flag: str = "--env") -> list[str]:
    env = _matching(args, "env", _ENV, required=False)
    return [flag, env] if env else []


def _find(a: dict) -> list[str]:
    return ["find", *_env(a), "--", _string(a, "term")]


def _show(name: str) -> Callable[[dict], list[str]]:
    return lambda a: [name, *_env(a), "--", _matching(a, "slug", _SLUG)]


def _list(a: dict) -> list[str]:
    return ["list", *(_env(a) or ["--all"])]


def _recall(a: dict) -> list[str]:
    files = a.get("files") or []
    if not isinstance(files, list) or len(files) > 10:
        raise _Invalid("files must be a list of at most 10 paths")
    argv = ["recall", *_env(a)]
    for f in files:
        if not isinstance(f, str) or not f.strip() or f.startswith("-") or len(f) > 200 or "\x00" in f:
            raise _Invalid("files must hold relative paths")
        argv += ["--file", f]
    limit = a.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise _Invalid("limit must be an integer from 1 to 50")
    return [*argv, "--limit", str(limit), "--", _string(a, "query")]


def _schema(**props: dict) -> dict:
    required = [k for k, v in props.items() if v.pop("required", False)]
    return {"type": "object", "properties": props, **({"required": required} if required else {}),
            "additionalProperties": False}


_ENV_PROP = {"type": "string", "description": "environment (a company or area of the store); optional"}
TOOLS = {
    "memory_find": ("Search memories and experiences by keyword.", _find,
                    _schema(term={"type": "string", "required": True}, env=_ENV_PROP)),
    "memory_show": ("Show one memory by slug with its evidence, relations and revisions.", _show("show"),
                    _schema(slug={"type": "string", "required": True}, env=_ENV_PROP)),
    "memory_why": ("The experiences and revisions behind a memory, oldest first.", _show("why"),
                   _schema(slug={"type": "string", "required": True}, env=_ENV_PROP)),
    "memory_list": ("List memories: of one environment, or of the whole store.", _list, _schema(env=_ENV_PROP)),
    "memory_recall": ("The memories most relevant to a task in the repository the server was started in.", _recall,
                      _schema(query={"type": "string", "required": True},
                              files={"type": "array", "items": {"type": "string"}, "maxItems": 10},
                              limit={"type": "integer", "minimum": 1, "maximum": 50}, env=_ENV_PROP)),
}


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process with stdout and stderr captured; a crash is an error, never an exit."""
    from lrdmem.cli import main  # cli registers this module's command, so import late

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 2
    except Exception:
        return 1, "", "lrdmem: internal error"
    return code, out.getvalue(), err.getvalue()


def _text(code: int, out: str, err: str) -> str:
    body = (out if code == 0 else (err or out)).strip() or "(no output)"
    body = "\n".join(printable(line) for line in body.split("\n"))
    if len(body) > CAP:
        body = body[:CAP] + "\n[truncated]"
    return f"{CAVEAT}\n\n{body}"


def _error(id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _call(params) -> dict:
    if not isinstance(params, dict) or params.get("name") not in TOOLS:
        raise _Invalid("unknown tool")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise _Invalid("arguments must be an object")
    argv = TOOLS[params["name"]][1](args)
    code, out, err = _run(argv)
    return {"content": [{"type": "text", "text": _text(code, out, err)}], **({"isError": True} if code else {})}


def handle(msg) -> dict | None:
    """The response to one JSON-RPC message, or None for a notification."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "a single JSON object per message")
    id, method = msg.get("id"), msg.get("method")
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(id, -32600, "not a JSON-RPC 2.0 request")
    if "id" not in msg:
        return None  # a notification (initialized, cancelled, ...): nothing to answer
    params = msg.get("params")
    try:
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {"protocolVersion": asked if asked in SUPPORTED else PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "lrdmem", "version": _version()}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s, "annotations": {"readOnlyHint": True}}
                                for n, (d, _, s) in TOOLS.items()]}
        elif method == "tools/call":
            result = _call(params)
        else:
            return _error(id, -32601, f"method not found: {method}")
    except _Invalid as e:
        return _error(id, -32602, str(e))
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _version() -> str:
    from lrdmem import __version__
    return __version__


def serve(stdin: TextIO, stdout: TextIO) -> int:
    """Answer one message per line until stdin closes; nothing but responses is ever written to stdout."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            response = handle(json.loads(line))
        except ValueError:
            response = _error(None, -32700, "parse error")
        if response is not None:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0
