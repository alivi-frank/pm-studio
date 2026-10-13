"""What agents do with their MCP servers, made visible - safely.

Two things live here, both shared by PM turns and dev tasks (see agent.ProgressTracker)
and by the connection test (agent_mcp.AgentMcp.test):

1. The sanitizer. A step label or a log line may say WHICH tool was called and a short,
   safe summary of its arguments - the SQL it ran (trimmed, one line), the URL it opened,
   the element it clicked, the screenshot's file name. It is a whitelist: any other
   argument is never shown, so the text typed into a field (which can be a password), a
   request body, a token or a script never leaves the agent. A result is never shown
   either - query rows can hold patient data - only its shape ("12 rows", "ok"), or for
   a failure the first line of the error ("read ECONNRESET"), with credentials in URLs,
   `password=...` pairs and the agent auth token blanked out.

2. The activity log: `<workspace>/mcp-activity.jsonl`, one line per MCP tool call when
   its result arrives (time, session, pm/task, server, tool, argument summary, ok,
   outcome, duration). Append-only and bounded: once it holds MAX_ENTRIES + TRIM_SLACK
   lines it is rewritten to the newest MAX_ENTRIES, so an append stays a single write.
   Machine-local operational data - gitsnapshot.SENSITIVE_WORKSPACE_FILES keeps it out
   of every commit. GET /agents/mcp/activity reads it (report()).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import CONFIG

FILE_NAME = "mcp-activity.jsonl"
MAX_ENTRIES = 2000
TRIM_SLACK = 400
MAX_OUTCOME_CHARS = 160
MAX_SQL_CHARS = 140
MAX_URL_CHARS = 140
MAX_NAME_CHARS = 60

# The only argument keys whose values ever reach a label or the log (see args_summary).
SQL_KEYS = ("sql", "query", "statement")
URL_KEYS = ("url", "href")
IDENTIFIER_KEYS = ("table", "schema", "database")
# Keys whose values are never shown, whatever the tool: what someone types, sends or
# runs. The whitelist above already leaves them out; listed so that a tool carrying one
# next to an `element` reads as "typing into <element>" rather than as a click.
SECRET_KEYS = frozenset({
    "text", "value", "values", "password", "passwd", "pass", "token", "secret", "secrets",
    "credentials", "credential", "body", "content", "data", "code", "function", "script",
    "cookie", "cookies", "headers", "authorization", "api_key", "apikey",
})

_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_SECRET_PAIR = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)(\s*[=:]\s*)(\S+)"
)
_TOOL_ERROR_TAG = re.compile(r"</?tool_use_error>")


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def redact(text: str) -> str:
    """Blanks what must never reach a browser or the log: the user:password part of a
    URL, `password=...`-style pairs and PM Studio's own agent auth token."""
    text = _URL_USERINFO.sub(r"\1***@", str(text))
    text = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    try:
        from .accounts import AGENT_TOKEN
    except Exception:  # pragma: no cover - accounts always imports
        AGENT_TOKEN = ""
    if AGENT_TOKEN:
        text = text.replace(AGENT_TOKEN, "***")
    return text


def _safe_url(url: str) -> str:
    """scheme://host[:port]/path - no credentials, no query values, no fragment."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return _one_line(redact(str(url).split("?")[0]), MAX_URL_CHARS)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    text = f"{parts.scheme}://{host}{parts.path}" + ("?…" if parts.query else "")
    return _one_line(text, MAX_URL_CHARS)


def args_summary(tool: str, args) -> str:
    """A short, safe summary of one MCP tool call's arguments (see the module doc): SQL
    trimmed to one line, URLs without credentials or query values, the element a browser
    acts on, a file's base name, a key press, a table name - and nothing else."""
    if not isinstance(args, dict):
        return ""
    tool = str(tool or "").lower()
    keys = {str(k).lower(): v for k, v in args.items()}
    parts: list[str] = []
    element = keys.get("element")
    if isinstance(keys.get("fields"), list):
        count = len(keys["fields"])
        parts.append(f"filling {count} field{'' if count == 1 else 's'}")
    elif isinstance(element, str) and element.strip():
        target = _one_line(element, MAX_NAME_CHARS)
        if "select" in tool:
            parts.append(f"choosing in {target}")
        elif "type" in tool or "fill" in tool or SECRET_KEYS & set(keys):
            parts.append(f"typing into {target}")
        else:
            parts.append(target)
    for key in SQL_KEYS:
        value = keys.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_one_line(redact(value), MAX_SQL_CHARS))
            break
    for key in URL_KEYS:
        value = keys.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_safe_url(value))
            break
    filename = keys.get("filename")
    if isinstance(filename, str) and filename.strip():
        parts.append(_one_line(Path(filename.replace("\\", "/")).name, MAX_NAME_CHARS))
    key = keys.get("key")
    if isinstance(key, str) and key.strip() and "press" in tool:
        parts.append(f"key {_one_line(key, 20)}")
    for name in IDENTIFIER_KEYS:
        value = keys.get(name)
        if isinstance(value, str) and value.strip():
            parts.append(f"{name} {_one_line(value, MAX_NAME_CHARS)}")
    return ", ".join(p for p in parts if p)


def _texts(content) -> tuple[list[str], int]:
    """The text blocks of a tool result's content, and how many images it holds."""
    if isinstance(content, str):
        return [content], 0
    texts: list[str] = []
    images = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                elif block.get("type") == "image":
                    images += 1
    return texts, images


def _row_count(data) -> int | None:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("rows", "items", "results", "data", "records"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def result_summary(content) -> str:
    """The SHAPE of a successful MCP result - never its contents: "12 rows" when it is
    JSON rows (an array, or an object holding one), "image" for a screenshot, else "ok"."""
    texts, images = _texts(content)
    joined = "\n".join(texts).strip()
    if joined:
        try:
            count = _row_count(json.loads(joined))
        except ValueError:
            count = None
        if count is not None:
            return f"{count} row{'' if count == 1 else 's'}"
    if images and not joined:
        return "image" if images == 1 else f"{images} images"
    return "ok"


def error_summary(content) -> str:
    """The first non-empty line of a failed result's text, redacted and capped."""
    texts, _ = _texts(content)
    for line in _TOOL_ERROR_TAG.sub("", "\n".join(texts)).splitlines():
        if line.strip():
            return _one_line(redact(line), MAX_OUTCOME_CHARS)
    return "failed"


def jsonrpc_error_summary(error) -> str:
    """A JSON-RPC error object (the connection test) as one safe line."""
    if isinstance(error, dict):
        text = str(error.get("message") or error.get("code") or "error")
    else:
        text = str(error)
    return error_summary(text)


class McpActivityLog:
    """The bounded JSONL log (see the module doc). Thread-safe within the process; a
    failure to write is reported once on stderr and never reaches an agent's turn."""

    def __init__(self, path: Path, max_entries: int = MAX_ENTRIES, slack: int = TRIM_SLACK) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self.slack = slack
        self._lock = threading.Lock()
        self._count: int | None = None
        self._warned = False

    def _read_lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self._count is None:
                    self._count = len(self._read_lines())
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                self._count += 1
                if self._count > self.max_entries + self.slack:
                    keep = self._read_lines()[-self.max_entries:]
                    tmp = self.path.with_name(self.path.name + ".tmp")
                    tmp.write_text("".join(f"{item}\n" for item in keep), encoding="utf-8")
                    os.replace(tmp, self.path)
                    self._count = len(keep)
            except OSError as exc:
                self._count = None
                if not self._warned:
                    self._warned = True
                    print(f"[pm_studio] MCP activity log not written ({exc})", file=sys.stderr)

    def entries(self) -> list[dict]:
        """Every retained entry, oldest first."""
        with self._lock:
            lines = self._read_lines()
        out: list[dict] = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def report(self, server: str | None = None, limit: int = 50) -> dict:
        """GET /agents/mcp/activity: the newest entries (newest first, optionally one
        server's) and per-server counters over the retained window."""
        entries = self.entries()
        servers: dict[str, dict] = {}
        for item in entries:
            name = str(item.get("server") or "")
            stats = servers.setdefault(name, {
                "calls": 0, "errors": 0, "consecutive_errors": 0, "last_error": None,
                "last_ok_at": None, "last_call_at": None, "last_ok": None,
            })
            stats["calls"] += 1
            stats["last_call_at"] = item.get("ts")
            stats["last_ok"] = bool(item.get("ok"))
            if item.get("ok"):
                stats["last_ok_at"] = item.get("ts")
                stats["consecutive_errors"] = 0
            else:
                stats["errors"] += 1
                stats["consecutive_errors"] += 1
                stats["last_error"] = {
                    "ts": item.get("ts"), "tool": item.get("tool"), "outcome": item.get("outcome"),
                }
        chosen = [e for e in entries if not server or e.get("server") == server]
        limit = max(1, min(int(limit), self.max_entries))
        return {
            "entries": chosen[-limit:][::-1],
            "servers": servers,
            "max_entries": self.max_entries,
        }


def make_entry(
    *, session: str, source: str, task_id: str = "", server: str, tool: str, args: str,
    ok: bool, outcome: str, duration_ms: int | None, ts: float | None = None,
) -> dict:
    entry = {
        "ts": time.time() if ts is None else ts,
        "session": session,
        "source": source,
        "server": server,
        "tool": tool,
        "args": args,
        "ok": ok,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    if task_id:
        entry["task_id"] = task_id
    return entry


# The process-wide log, in the primary checkout's workspace (shared by every session).
ACTIVITY_LOG = McpActivityLog(CONFIG.workspace_dir / FILE_NAME)
