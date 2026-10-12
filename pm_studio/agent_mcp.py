"""The MCP servers PM Studio hands to its agents - `[agents.mcp.<name>]` in config.toml
(see config.AgentMcpServer) - turned into a Claude Code `--mcp-config` file per launch.

One place, by design. Every agent launch passes `--strict-mcp-config`, so an agent sees
exactly the servers declared here and nothing from its own Claude Code config (an
isolated ~/.claude.json, a project .mcp.json): a deployment never has to wonder which of
several configs a dev agent picked a database connection up from.

Who gets what:

  - dev tasks: every server;
  - PM turns: only servers with `pm = true`, each also added to the turn's
    --allowedTools as `mcp__<name>` (the PM otherwise runs a fixed allowlist);
  - judges: none (`--strict-mcp-config` with no config at all).

Zero install. Nothing here installs anything: a stdio server is a command such as
`npx -y <package>`, and npx downloads the package on first use. What PM Studio does is
adapt the command to the OS (on Windows `npx` is a .cmd shim that CreateProcess cannot
start directly, so it is wrapped as `cmd /c npx ...`), resolve `${VAR}` /
`${VAR:-default}` references from the environment (the repo's .env is merged into it at
startup), and leave out - with one warning per server, naming only the variable - any
server whose variables are unset or whose command is not on PATH (no Node, no npx).

Hot reload. CONFIG is frozen at startup, but this part of config.toml is re-read
whenever the file's mtime changed - at every agent launch, and on demand (POST
/agents/mcp/reload) - with the same parser. A broken edit never breaks launches: the
last good server list stays in force and the error is reported in the status.

Secrets. Resolved values (a database URL with its password, an auth header) only ever
exist in memory and in the per-launch temp file, created by mkstemp (readable by this
user only), outside the repo, and deleted when the agent exits. Logs and the status
payload carry server names, variable NAMES, commands as written and URL hosts - never
args, values or headers.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

from .config import (
    CONFIG,
    CONFIG_FILE_NAME,
    LOCAL_DIR_NAME,
    AgentMcpServer,
    parse_agent_mcp,
)
from . import mcp_activity

# ${VAR} or ${VAR:-default}. The default may be empty (`${VAR:-}`).
VAR_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# Bare launcher names that are .cmd shims on Windows (see adapt_command).
NODE_LAUNCHERS = frozenset({"npx", "npm", "pnpm", "yarn", "bunx"})
SOURCE = f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME}"

READY = "ready"
SKIPPED = "skipped"
UNAVAILABLE = "unavailable"

# POST /agents/mcp/{name}/check: a stdio server that is still running this long after
# launch (stdin held open, as Claude Code does) counts as starting fine.
CHECK_ALIVE_SECONDS = 3.0
# POST /agents/mcp/{name}/test: the whole handshake (start, initialize, tools/list and
# the optional probe call) must finish within this; npx's first-use download counts.
TEST_TIMEOUT_SECONDS = 30.0
MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_TOOL_PAGES = 10


def _log(message: str) -> None:
    print(f"[pm_studio] {message}", file=sys.stderr)


def expand(text: str, environ) -> tuple[str, list[str]]:
    """`text` with every ${VAR} / ${VAR:-default} resolved from `environ`, plus the
    names of variables that were unset with no default (left in place)."""
    missing: list[str] = []

    def fill(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        value = environ.get(name)
        if value is not None and value != "":
            return value
        if default is not None:
            return default
        if value is not None:  # set but empty, no default: empty is what was set
            return value
        missing.append(name)
        return match.group(0)

    return VAR_REFERENCE.sub(fill, text), missing


def adapt_command(command: str, args: list[str], windows: bool) -> tuple[str, list[str]]:
    """On Windows, npx/npm/pnpm/yarn/bunx are .cmd shims: Claude Code spawns the
    command directly, and a .cmd only runs through cmd.exe. A bare name (no path, no
    extension) is wrapped as `cmd /c <name> ...`; anything else is left as written."""
    if windows and command in NODE_LAUNCHERS:
        return "cmd", ["/c", command, *args]
    return command, list(args)


class _TestFailure(Exception):
    """One stage of the connection test failed; the message is already safe to show."""


class _StdioSession:
    """A JSON-RPC conversation with a stdio MCP server: newline-delimited JSON on its
    stdin/stdout, started in its own process group so close() can end the whole tree
    (`cmd /c npx ...` is three processes deep)."""

    def __init__(self, argv: list[str], env: dict, cwd: str | None, deadline: float) -> None:
        self.argv, self.env, self.cwd, self.deadline = argv, env, cwd, deadline
        self.proc: subprocess.Popen | None = None
        self._messages: "queue.Queue[dict | None]" = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 0

    def start(self) -> None:
        from .local_env import _group_kwargs

        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.env, cwd=self.cwd, **_group_kwargs(),
            )
        except OSError as exc:
            raise _TestFailure(f"could not start: {exc.strerror or type(exc).__name__}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # a server logging to stdout; not protocol
                if isinstance(message, dict):
                    self._messages.put(message)
        self._messages.put(None)

    def _read_stderr(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
                    del self._stderr[:-20]

    def _exited(self) -> _TestFailure:
        code = None
        with contextlib.suppress(Exception):
            code = self.proc.wait(timeout=2)
        last = self._stderr[-1] if self._stderr else ""
        detail = f": {mcp_activity.error_summary(last)}" if last else ""
        return _TestFailure(f"the server exited (code {code}){detail}")

    def _send(self, message: dict) -> None:
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise self._exited() from exc

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _TestFailure(f"no answer to {method} in time")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise _TestFailure(f"no answer to {method} in time") from None
            if message is None:
                raise self._exited()
            if "method" in message and "id" in message:
                # A request from the server (ping, roots/list...): answer, keep waiting.
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "not supported by the PM Studio test"}
                self._send(reply)
                continue
            if message.get("id") != request_id:
                continue  # a notification (logging, progress)
            if "error" in message:
                raise _TestFailure(mcp_activity.jsonrpc_error_summary(message["error"]))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self.proc is None:
            return
        from .local_env import _kill_tree

        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        with contextlib.suppress(Exception):
            _kill_tree(self.proc)


class _HttpSession:
    """The same conversation over MCP's streamable HTTP transport: one POST per message,
    the answer as JSON or as an SSE stream, the session id carried in Mcp-Session-Id."""

    def __init__(self, url: str, headers: dict, deadline: float) -> None:
        self.url, self.headers, self.deadline = url, dict(headers), deadline
        self.session_id: str | None = None
        self._next_id = 0

    def start(self) -> None:
        pass

    def _post(self, message: dict, method: str = "POST"):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _TestFailure("no answer in time")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        data = json.dumps(message).encode("utf-8") if method == "POST" else None
        request = urllib.request.Request(self.url, data=data, method=method, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            raise _TestFailure(f"HTTP {exc.code} {mcp_activity.error_summary(str(exc.reason or ''))}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _TestFailure(mcp_activity.error_summary(str(reason))) from exc

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        with self._post(message) as response:
            response.read(65536)

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        with self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) as response:
            self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
            if "text/event-stream" in (response.headers.get("Content-Type") or ""):
                message = self._from_stream(response, request_id)
            else:
                try:
                    message = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", "replace"))
                except ValueError as exc:
                    raise _TestFailure(f"{method}: the answer is not JSON") from exc
        if not isinstance(message, dict):
            raise _TestFailure(f"{method}: no answer")
        if "error" in message:
            raise _TestFailure(mcp_activity.jsonrpc_error_summary(message["error"]))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _from_stream(response, request_id: int) -> dict | None:
        data: list[str] = []
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line or not data:
                continue
            with contextlib.suppress(ValueError):
                message = json.loads("\n".join(data))
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            data = []
        return None

    def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception):
                self._post({}, method="DELETE").close()


def _expand_json(value, environ, missing: list[str]):
    """A probe's args with ${VAR} resolved in every string, at any depth."""
    if isinstance(value, str):
        text, gaps = expand(value, environ)
        missing.extend(gaps)
        return text
    if isinstance(value, list):
        return [_expand_json(item, environ, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand_json(item, environ, missing) for key, item in value.items()}
    return value


class AgentMcp:
    """The live `[agents.mcp]` server list, with hot reload, resolution and the
    per-launch config file. One instance per process (AGENT_MCP below); tests build
    their own with an injected environment, platform and `which`."""

    def __init__(
        self,
        config_path: Path,
        servers: tuple[AgentMcpServer, ...] = (),
        *,
        environ=None,
        windows: bool | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._path = Path(config_path)
        self._servers = tuple(servers)
        self._environ = environ
        self._windows = os.name == "nt" if windows is None else windows
        self._which = which
        self._lock = threading.Lock()
        self._error: str | None = None
        self._loaded_at = time.time()
        # The file the startup CONFIG was read from: unchanged since, nothing to reload.
        self._mtime = self._stat()
        self._warned: set[str] = set()

    # ---- loading ----

    def _stat(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def refresh(self, force: bool = False) -> None:
        """Re-reads [agents] when config.toml changed since the last read (always, with
        `force`). On any error the last good list stays and the error is recorded."""
        mtime = self._stat()
        with self._lock:
            if not force and mtime == self._mtime:
                return
            self._mtime = mtime
            try:
                raw = tomllib.loads(self._path.read_text(encoding="utf-8")) if mtime is not None else {}
                servers = parse_agent_mcp(raw)
            except (OSError, ValueError) as exc:  # TOMLDecodeError and AgentConfigError are ValueErrors
                self._error = str(exc)
                _log(f"agent MCP: {SOURCE} has an error, keeping the last good servers: {exc}")
                return
            self._servers = servers
            self._error = None
            self._loaded_at = time.time()
            # A changed list is a new chance to warn about what is skipped.
            self._warned.clear()
        self.log_summary()

    def servers(self) -> tuple[AgentMcpServer, ...]:
        self.refresh()
        return self._servers

    # ---- resolution ----

    def _env(self, extra: dict[str, str] | None = None):
        base = os.environ if self._environ is None else self._environ
        return {**base, **extra} if extra else base

    def _resolve(
        self, spec: AgentMcpServer, extra_env: dict[str, str] | None = None
    ) -> tuple[dict, list[str]]:
        """The server's --mcp-config entry with every variable resolved, and the names
        of variables that could not be. `extra_env` (per launch, e.g. a dev task's
        PM_STUDIO_ARTIFACTS_DIR) wins over the process environment."""
        environ = self._env(extra_env)
        missing: list[str] = []

        def ex(text: str) -> str:
            value, gaps = expand(text, environ)
            missing.extend(gaps)
            return value

        env = {key: ex(value) for key, value in spec.env}
        if spec.transport == "stdio":
            command, args = adapt_command(ex(spec.command), [ex(a) for a in spec.args], self._windows)
            entry: dict = {"type": "stdio", "command": command, "args": args}
            if env:
                entry["env"] = env
        else:
            entry = {"type": spec.transport, "url": ex(spec.url)}
            headers = {key: ex(value) for key, value in spec.headers}
            if headers:
                entry["headers"] = headers
        return entry, list(dict.fromkeys(missing))

    def _command_path(self, spec: AgentMcpServer) -> str | None:
        command, _ = expand(spec.command, self._env())
        return self._which(command)

    def _state(
        self, spec: AgentMcpServer, extra_env: dict[str, str] | None = None
    ) -> tuple[str, list[str], dict]:
        entry, missing = self._resolve(spec, extra_env)
        if missing:
            return SKIPPED, missing, entry
        if spec.transport == "stdio" and self._command_path(spec) is None:
            return UNAVAILABLE, [], entry
        return READY, [], entry

    def _warn_once(self, spec: AgentMcpServer, status: str, missing: list[str]) -> None:
        if spec.name in self._warned:
            return
        self._warned.add(spec.name)
        if status == SKIPPED:
            _log(f"agent MCP: skipping {spec.name!r}: {', '.join(missing)} not set (add it to .env)")
        else:
            _log(f"agent MCP: skipping {spec.name!r}: {spec.command!r} not found on PATH")

    def launch_config(
        self, pm: bool, extra_env: dict[str, str] | None = None
    ) -> tuple[dict, list[str]]:
        """{"mcpServers": {...}} for one launch, and the names it holds: every ready
        server for a dev agent, only `pm = true` ones for the PM."""
        servers: dict[str, dict] = {}
        for spec in self.servers():
            if pm and not spec.pm:
                continue
            status, missing, entry = self._state(spec, extra_env)
            if status != READY:
                self._warn_once(spec, status, missing)
                continue
            servers[spec.name] = entry
        return {"mcpServers": servers}, list(servers)

    @contextlib.contextmanager
    def launch_args(
        self, pm: bool, extra_env: dict[str, str] | None = None
    ) -> Iterator[tuple[list[str], list[str]]]:
        """The `claude` arguments for one agent launch, and the server names included.
        Always `--strict-mcp-config` (config.toml is the only source); plus
        `--mcp-config <file>` when there is anything to give. The file is created by
        mkstemp - readable by this user only, in the system temp dir, never the repo -
        and deleted when the block exits, i.e. once the agent's process has ended.
        `extra_env` holds variables that exist only for this launch (see _resolve)."""
        config, names = self.launch_config(pm, extra_env)
        if not names:
            yield ["--strict-mcp-config"], []
            return
        fd, path = tempfile.mkstemp(prefix="pm_studio_mcp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            # --mcp-config takes several values; the flag right after it ends the list.
            yield ["--mcp-config", path, "--strict-mcp-config"], names
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ---- reporting ----

    def status(self) -> dict:
        """GET /agents/mcp. Names, transports, commands AS WRITTEN (no args), URL hosts
        and variable NAMES only - never resolved values, args or headers."""
        self.refresh()
        servers = []
        for spec in self._servers:
            status, missing, entry = self._state(spec)
            url_host = None
            if spec.transport != "stdio" and status != SKIPPED:
                url_host = urlsplit(entry.get("url", "")).hostname
            if status == SKIPPED:
                message = f"set {', '.join(missing)} in .env"
            elif status == UNAVAILABLE:
                message = f"{spec.command!r} not found on PATH"
                if spec.command in NODE_LAUNCHERS:
                    message += " - install Node.js (npx then downloads the package on first use)"
            else:
                message = "given to dev agents and the PM" if spec.pm else "given to dev agents"
            servers.append({
                "name": spec.name,
                "transport": spec.transport,
                "command": spec.command if spec.transport == "stdio" else None,
                "url_host": url_host,
                "pm": spec.pm,
                # The probe's tool name only (POST .../test calls it); never its args.
                "probe": spec.probe.tool if spec.probe else None,
                "status": status,
                "missing_env": missing,
                "message": message,
            })
        npx = self._which("npx")
        return {
            "source": SOURCE,
            "loaded_at": self._loaded_at,
            "error": self._error,
            "servers": servers,
            "node": {"npx": npx is not None, "path": npx},
        }

    def summary(self) -> str:
        """One line: "agent MCP: N servers (names), skipped: name (VAR), ..."."""
        ready, skipped = [], []
        for spec in self._servers:
            status, missing, _ = self._state(spec)
            if status == READY:
                ready.append(spec.name)
            else:
                why = ", ".join(missing) if status == SKIPPED else f"{spec.command} not on PATH"
                skipped.append(f"{spec.name} ({why})")
        line = f"agent MCP: {len(ready)} servers ({', '.join(ready) or 'none'})"
        if skipped:
            line += f", skipped: {'; '.join(skipped)}"
        return line

    def log_summary(self) -> None:
        _log(self.summary())

    def check(self, name: str, alive_seconds: float = CHECK_ALIVE_SECONDS) -> dict:
        """POST /agents/mcp/{name}/check: launch a stdio server once, OS-adapted and
        with its resolved env, holding its stdin open as Claude Code would, and report
        whether it is still running after `alive_seconds` (then stop it). A minimal
        smoke test - it does not speak MCP. Raises KeyError for an unknown name."""
        spec = next((s for s in self.servers() if s.name == name), None)
        if spec is None:
            raise KeyError(name)
        status, missing, entry = self._state(spec)
        result = {"name": name, "status": status, "ok": False}
        if spec.transport != "stdio":
            return {**result, "ok": None, "message": "only stdio servers can be checked here"}
        if status == SKIPPED:
            return {**result, "message": f"set {', '.join(missing)} in .env"}
        if status == UNAVAILABLE:
            return {**result, "message": f"{spec.command!r} not found on PATH"}
        from .local_env import _group_kwargs, _kill_tree  # the same process-tree handling

        env = dict(os.environ)
        env.update(entry.get("env", {}))
        try:
            proc = subprocess.Popen(
                [entry["command"], *entry["args"]],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, **_group_kwargs(),
            )
        except OSError as exc:
            return {**result, "message": f"could not start: {exc.strerror or type(exc).__name__}"}
        try:
            code = proc.wait(timeout=alive_seconds)
        except subprocess.TimeoutExpired:
            return {**result, "ok": True, "message": f"started and still running after {alive_seconds:g}s"}
        else:
            return {**result, "message": f"exited with code {code} within {alive_seconds:g}s"}
        finally:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()
                _kill_tree(proc)

    def test(
        self, name: str, timeout: float = TEST_TIMEOUT_SECONDS,
        activity: "mcp_activity.McpActivityLog | None" = None,
    ) -> dict:
        """POST /agents/mcp/{name}/test: a real connection test. Starts the server
        exactly as an agent gets it (the resolved launch entry, OS-adapted, with the
        agents' environment), speaks MCP - `initialize`, `notifications/initialized`,
        `tools/list` - and, when the server declares a `probe`, calls that tool. All
        within `timeout` seconds; the process tree is killed afterwards.

        Returns {name, transport, ok, stage, tools, probe_outcome, error, duration_ms}:
        `stage` is where it stopped ("start", "initialize", "tools", "probe"); on success
        the last stage run. `probe_outcome` is the result's shape only ("1 row", "ok"),
        `error` one safe line. A probe call is also recorded in `activity` (source
        "test"), so a server that works again stops showing as failing.
        http servers get the same handshake over streamable HTTP; sse ones are not
        supported yet (ok None). Raises KeyError for an unknown name."""
        spec = next((s for s in self.servers() if s.name == name), None)
        if spec is None:
            raise KeyError(name)
        began = time.monotonic()
        deadline = began + timeout
        result: dict = {
            "name": name, "transport": spec.transport, "ok": False, "stage": "start",
            "tools": [], "probe": spec.probe.tool if spec.probe else None,
            "probe_outcome": None, "error": None, "duration_ms": 0,
        }

        def finish(**fields) -> dict:
            result.update(fields)
            result["duration_ms"] = int(round((time.monotonic() - began) * 1000))
            return result

        status, missing, entry = self._state(spec)
        if status == SKIPPED:
            return finish(error=f"set {', '.join(missing)} in .env")
        if status == UNAVAILABLE:
            return finish(error=f"{spec.command!r} not found on PATH")
        if spec.transport == "sse":
            return finish(ok=None, error="the connection test is not supported for sse servers yet")
        if spec.transport == "stdio":
            from .agent_env import subprocess_env  # the environment agents run with

            env = subprocess_env()
            env.update(entry.get("env", {}))
            session = _StdioSession(
                [entry["command"], *entry["args"]], env, str(CONFIG.repo_root), deadline,
            )
        else:
            session = _HttpSession(entry["url"], entry.get("headers", {}), deadline)
        try:
            session.start()
            result["stage"] = "initialize"
            session.request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pm-studio-test", "version": "1"},
            })
            session.notify("notifications/initialized")
            result["stage"] = "tools"
            tools: list[str] = []
            cursor = None
            for _ in range(MAX_TOOL_PAGES):
                listing = session.request("tools/list", {"cursor": cursor} if cursor else {})
                tools += [str(t.get("name")) for t in listing.get("tools") or [] if isinstance(t, dict)]
                cursor = listing.get("nextCursor")
                if not cursor:
                    break
            result["tools"] = tools
            if spec.probe is None:
                return finish(ok=True)
            result["stage"] = "probe"
            gaps: list[str] = []
            args = _expand_json(spec.probe.args, self._env(), gaps)
            if gaps:
                return finish(error=f"the probe needs {', '.join(dict.fromkeys(gaps))} set in .env")
            if spec.probe.tool not in tools:
                return finish(error=f"the server has no tool {spec.probe.tool!r}")
            probe_began = time.monotonic()
            called = session.request("tools/call", {"name": spec.probe.tool, "arguments": args})
            ok = not called.get("isError")
            outcome = (mcp_activity.result_summary if ok else mcp_activity.error_summary)(called.get("content"))
            if activity is not None:
                with contextlib.suppress(Exception):
                    activity.record(mcp_activity.make_entry(
                        session="", source="test", server=name, tool=spec.probe.tool,
                        args=mcp_activity.args_summary(spec.probe.tool, args), ok=ok, outcome=outcome,
                        duration_ms=int(round((time.monotonic() - probe_began) * 1000)),
                    ))
            if ok:
                return finish(ok=True, probe_outcome=outcome)
            return finish(error=outcome)
        except _TestFailure as exc:
            return finish(error=str(exc))
        finally:
            session.close()


# The process-wide instance, seeded with the startup CONFIG (constructing it only
# stats config.toml). Agent launches and the /agents/mcp routes go through it.
AGENT_MCP = AgentMcp(CONFIG.local_dir / CONFIG_FILE_NAME, CONFIG.agent_mcp)
