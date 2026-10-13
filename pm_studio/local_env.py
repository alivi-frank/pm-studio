"""The local-environment supervisor: starts and stops the services a system declares
in `[systems.<id>.local]` (see config.LocalServiceSpec) on the developer's machine, and
provisions the resources they need (config.LocalResourceSpec).

Why PM Studio owns these processes at all: a dev agent verifying its change, a PM
checking a flow end to end, and the human watching the board all need the same thing -
"EpicRide is running locally, with its secrets emulator, against the dev profile" - and
before this, each of them improvised it (a dev agent backgrounding `npm start` inside
its own turn, which dies with the turn or, worse, outlives it as an orphan nobody can
see). So the processes belong to the SERVER, not to whichever task asked for them:

  - They outlive dev tasks and PM turns. A task can start the API and leave it up for
    the next task; nothing is torn down when an agent's `claude -p` exits.
  - They do NOT outlive the server. Every process is stopped on shutdown (the FastAPI
    lifespan hook, with atexit as the backstop), and each is launched in its own process
    group so the whole tree - the shell, `npm`, the node it spawns - goes with it.
  - One supervisor, one view. The UI, the agents (over HTTP, see server.py) and the
    human all see the same state, pid, logs and readiness.

One environment PER CHAT SESSION. Each session works on its own branch in its own
worktree, so "the API" means a different build in each of them; a dev agent in session
A verifying its change against session B's API would verify nothing. So every run is
keyed (scope, system, service), where scope is the session id for "session"-scoped
services - started from that session's worktree, typically on an auto-assigned port -
or "shared" for the few services with no per-session state (a secrets emulator), which
run once from the primary checkout and are reused by every session. Everything is on
demand: nothing starts, and no session checkout is prepared, until someone asks.

What a session environment costs is bounded twice: `[local_env] max_session_envs` caps
how many (session, system) environments may be up at once (a 409 names the running
ones), and release_session() - called by the server when a session ends - stops its
services and destroys the resources it provisioned.

Sessions rarely END, though; they are abandoned. So a slot is also given back when
nobody is using it: every call made on behalf of a session (start, stop, wait, logs,
status...) is that session's "activity", and a session counts as in use while it has a
dev task running (the injected `session_busy`) - and while its services keep writing
output, which is how a person clicking through the app they serve shows up. With `[local_env] idle_stop_minutes`
set, reap_idle() - called periodically by the reaper thread start_idle_reaper() runs -
stops the session-scoped services of every session idle that long; and whatever the
setting, a start that would hit the limit first stops the least recently active OTHER
session idle for at least MAKE_ROOM_IDLE_SECONDS, before refusing. Either way only
processes stop: resources and the prepared checkout are kept, and each stopped
service's status message says why, so the agent or human who comes back knows that
starting it again is all it takes.

Resources are the things a service needs PROVISIONED rather than run - a per-session
temporary database being the canonical one. A resource is created the first time a
dependent service starts in its scope, then reused (its outputs persist across server
restarts in a JSON file under the workspace, see `state_path`) until it is destroyed on
request or with its session. Shutdown deliberately does NOT destroy them: re-creating a
database on every server restart is exactly the cost reuse exists to avoid.

The supervisor is deliberately technology-agnostic. A service is a shell command plus a
declared readiness probe - a TCP port and/or an HTTP URL - and nothing here knows
whether it is running Node, .NET, Python or Flutter. What a service needs installed
first (its npm dependencies, say) is its optional `setup` command, run from the primary
checkout before every launch - so nobody has to remember to run it by hand.

Safety notes, because these commands run through the shell:

  - Callers never supply a command, only values for the `{name}` placeholders the
    operator declared, and every value is checked against PARAM_VALUE - a strict
    allowlist with no quotes, `;`, `&`, `|`, `$`, backticks, redirections or newlines.
    Values PM Studio generates itself (ports, session ids, worktree paths, resource
    outputs) pass the same allowlist before they are substituted: a resource's `create`
    is operator code, but what it PRINTS is data, and data does not reach a shell
    unchecked.
  - A port that is already taken by something that is not ours is refused up front
    (state `port_in_use`) instead of launching a second copy that will fail to bind, or
    worse, quietly talk to whatever already holds the port.
  - Logs go to the system temp directory, never into the repo: PM Studio snapshots the
    repo with `git add -A`, and a service's output has no business in that history.

Importing this module has no side effects: nothing starts, no thread runs, no file is
read and nothing is registered until a LocalEnvManager is actually used.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import importlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import (
    DEFAULT_MAX_SESSION_ENVS,
    LOCAL_OUTPUT_KEY,
    LOCAL_PLACEHOLDER,
    LocalEnvSpec,
    LocalResourceSpec,
    LocalServiceSpec,
    SystemSpec,
)

# What a caller-supplied param value may contain. Letters, digits, space and a handful
# of separators that real params need (comma-separated lists, hosts and ports, paths,
# emails, key=value) - and nothing a shell treats specially. Deliberately an allowlist:
# a denylist of shell metacharacters is exactly the kind of list that is one character
# short.
PARAM_VALUE = re.compile(r"^[A-Za-z0-9 ,._:/@=-]*$")
MAX_PARAM_LENGTH = 256
# Generated values get the same character allowlist with more room: a worktree path is
# routinely longer than any param a human types.
MAX_GENERATED_LENGTH = 1024

LOG_DIR = Path(tempfile.gettempdir()) / "pm-studio-local-env"
LOG_LINES = 1000
DEFAULT_LOG_TAIL = 200

# With no probe declared, a process still alive this long after launch counts as up.
NO_PROBE_GRACE_SECONDS = 1.0
# Readiness poll cadence. The wait is a blocking `proc.wait(timeout=...)`, so the loop
# also notices an early exit without ever spinning.
POLL_SECONDS = 0.5
PROBE_CONNECT_TIMEOUT = 0.5
PROBE_HTTP_TIMEOUT = 2.0
# POSIX: how long a process group gets between SIGTERM and SIGKILL.
STOP_GRACE_SECONDS = 5.0

# wait(): how long a caller may block on a service leaving "starting". The cap keeps a
# request thread from being parked indefinitely; a service slower than that can be
# waited on again. The wake interval is a safety net only - state changes notify.
DEFAULT_WAIT_SECONDS = 180.0
MAX_WAIT_SECONDS = 600.0
WAIT_WAKE_SECONDS = 2.0
# Idle stop: how often the reaper thread looks. Coarse on purpose - the threshold is in
# minutes, and each pass asks the session machinery whether every active session is busy.
REAP_INTERVAL_SECONDS = 30.0
# Making room at the limit only takes a slot from a session idle at least this long: a
# session that touched its environment a moment ago is plausibly about to use it (an
# agent between a start and its first request), and evicting it would just move the
# 409 to the other side.
MAKE_ROOM_IDLE_SECONDS = 60.0

# The session every route defaults to (sessions.DEFAULT_SESSION_ID; not imported, so
# this module stays importable without the session machinery behind it), and the scope
# key shared runs and resources live under.
DEFAULT_SESSION_ID = "default"
SHARED = "shared"

# Service states.
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
FAILED = "failed"
EXITED = "exited"
PORT_IN_USE = "port_in_use"
STATES = (STOPPED, STARTING, RUNNING, FAILED, EXITED, PORT_IN_USE)
_LIVE = (STARTING, RUNNING)

# Resource states ("failed" is shared with services).
ABSENT = "absent"
CREATING = "creating"
READY = "ready"
DESTROYING = "destroying"
# Not stored: a READY resource whose creation predates its most recent daily expiry
# boundary reads as this (see LocalEnvManager._effective_locked).
EXPIRED = "expired"
RESOURCE_STATES = (ABSENT, CREATING, READY, FAILED, DESTROYING, EXPIRED)

# Session-checkout preparation states, per (session, system).
PREP_NOT_NEEDED = "not_needed"  # the primary checkout: nothing to prepare
PREP_PENDING = "pending"  # never asked for yet
PREP_PREPARING = "preparing"
PREP_READY = "ready"

STATE_FILE_VERSION = 1


class LocalEnvError(Exception):
    """A refused local-env operation. `status_code` is the HTTP status the API answers
    with; the message is safe to show a user."""

    status_code = 400


class UnknownTarget(LocalEnvError):
    status_code = 404


class UnknownSession(UnknownTarget):
    status_code = 404


class InvalidParams(LocalEnvError):
    status_code = 400


class PortInUse(LocalEnvError):
    status_code = 409


class LimitReached(LocalEnvError):
    status_code = 409


class Busy(LocalEnvError):
    """The target is mid-transition (a resource being created or destroyed, or still in
    use by a running service) and the request would trample it."""

    status_code = 409


@dataclass(frozen=True)
class SessionContext:
    """What the supervisor needs to know about a chat session: where its checkout is.
    `primary` is true for the default session (whose worktree IS the primary checkout)
    and for the synthetic shared context - the two cases that never need a session
    checkout prepared."""

    id: str
    name: str
    worktree: Path
    primary: bool = False


# session id -> its context, or None for a session that does not exist (or no longer
# has a worktree). server.py supplies one over the SessionManager.
SessionLookup = Callable[[str], "SessionContext | None"]
# (repo_root, worktree, system_path, link_paths) -> human-readable notes. Raises on
# failure. The default is session_checkout.ensure_system_checkout.
PrepareCheckout = Callable[[Path, Path, str, tuple], list]
# session id -> whether something is using its environment right now that makes no
# local-env calls of its own (server.py: a dev task running in that session).
SessionBusy = Callable[[str], bool]


@dataclass
class _Run:
    """The mutable runtime state of one service in one scope. One per (scope, system,
    service), reused across launches; `generation` is bumped on every launch and every
    stop, and every background thread carries the generation it was started for, so a
    thread that belongs to a superseded launch can never overwrite the state of the
    current one."""

    state: str = STOPPED
    proc: subprocess.Popen | None = None
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    message: str = ""
    params: dict[str, str] = field(default_factory=dict)
    # The port this launch uses (fixed, or picked for "auto"), and the url/ready_url as
    # rendered for it. None/"" until known.
    port: int | None = None
    # The "auto" port the previous launch used. A relaunch takes it again when it is free,
    # because whatever depends on this service was rendered with it: an API that came
    # back on a new port left the web app proxying to a dead one (and a web app on a new
    # port is a new browser origin, which logs the user out).
    last_port: int | None = None
    url: str = ""
    ready_url: str = ""
    # What the resources this launch consumes looked like when it was substituted:
    # resource id -> its outputs as sorted pairs, or None when it was not ready. None
    # (the whole map) until launched. Compared against the present for restart_needed.
    resource_snapshot: dict | None = None
    lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LINES))
    lines_lock: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0
    # True while `proc` is the service's `setup` command rather than the service itself
    # (see LocalEnvManager._run_setup). Holding the setup in `proc` is what lets every
    # existing stop path - stop, stop_all, idle stop, release, shutdown - kill it too.
    in_setup: bool = False


@dataclass
class _Resource:
    """The mutable state of one resource in one scope. Same generation discipline as
    _Run: the create/destroy thread only writes back if nothing superseded it."""

    state: str = ABSENT
    outputs: dict[str, str] = field(default_factory=dict)
    message: str = ""
    created_at: float | None = None
    proc: subprocess.Popen | None = None
    lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LINES))
    lines_lock: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0


@dataclass
class _Prep:
    """Whether a session's worktree has been made runnable for one system (see
    _ensure_prepared). `lock` serialises the preparation itself, which can take a while
    (git worktrees for the system's submodules) and must not run twice at once."""

    state: str = PREP_PENDING
    notes: list[str] = field(default_factory=list)
    message: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


def _quote_note(value: str) -> str:
    return value if len(value) <= 40 else value[:37] + "..."


def _boundary_on(day: date, expires_at: str, timezone: str) -> datetime:
    """`expires_at` ("HH:MM") on `day`, as an aware datetime: in the IANA `timezone`,
    or in this machine's local time (with its own DST rules) when it is ""."""
    hours, minutes = (int(part) for part in expires_at.split(":"))
    naive = datetime.combine(day, clock_time(hours, minutes))
    if timezone:
        return naive.replace(tzinfo=ZoneInfo(timezone))
    return naive.astimezone()  # a naive datetime is read as local time


def _local_now(now: float, timezone: str) -> datetime:
    if timezone:
        return datetime.fromtimestamp(now, ZoneInfo(timezone))
    return datetime.fromtimestamp(now).astimezone()


def last_expiry_boundary(now: float, expires_at: str, timezone: str = "") -> datetime:
    """The most recent daily `expires_at` at or before `now` (epoch seconds). Computed
    on calendar dates in the zone, so a DST change between two boundaries moves the
    instant, never the wall-clock time - which is how a cron job at midnight behaves."""
    today = _local_now(now, timezone).date()
    boundary = _boundary_on(today, expires_at, timezone)
    if boundary.timestamp() > now:
        boundary = _boundary_on(today - timedelta(days=1), expires_at, timezone)
    return boundary


def next_expiry_boundary(now: float, expires_at: str, timezone: str = "") -> datetime:
    """The first daily `expires_at` strictly after `now`."""
    today = _local_now(now, timezone).date()
    boundary = _boundary_on(today, expires_at, timezone)
    if boundary.timestamp() <= now:
        boundary = _boundary_on(today + timedelta(days=1), expires_at, timezone)
    return boundary


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str) -> str:
    """Expands `${NAME}` and `${NAME:-default}` from the environment (which holds the
    deployment's .env, merged in at startup) in a descriptive setting such as
    `aws_profile`, so the UI shows what the system's own tooling will use when that is
    a per-developer .env value rather than one name for everybody."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), "") or (m.group(2) or ""), value)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "_"


def _port_open(port: int) -> bool:
    """Whether something accepts TCP connections on `port` on this machine. Both
    loopbacks are tried because a dev server may bind only one of them (Node's
    `localhost` resolves to ::1 first on recent versions; most others take 127.0.0.1)."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=PROBE_CONNECT_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def _free_port() -> int:
    """A port the OS considers free right now. There is a window between this and the
    service binding it; the supervisor narrows it by never handing the same port to two
    live runs, and a lost race surfaces as the service failing to start, not as two
    services silently sharing a port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _url_ready(url: str) -> bool:
    """Any HTTP answer below 500 means the server is up and routing - a 404 or a 401
    from an API root is still a live API. A 5xx usually means it is up but not ready
    yet (a proxy in front of a backend still booting), so it keeps waiting."""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_HTTP_TIMEOUT) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_owner(port: int) -> int | None:
    """The PID listening on `port`, when the OS will say so cheaply; None otherwise.
    Only ever used to make a `port_in_use` message more actionable, so every failure
    here is swallowed."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace", timeout=5
            ).stdout
            out += subprocess.run(
                ["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True, errors="replace", timeout=5
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                # Matched on the shape, not the state word: "LISTENING" is localized
                # ("ESCUCHANDO", ...), but a listener's foreign address is always 0.
                if (
                    len(parts) >= 5
                    and parts[1].endswith(f":{port}")
                    and parts[2] in ("0.0.0.0:0", "[::]:0")
                    and parts[-1].isdigit()
                ):
                    return int(parts[-1])
            return None
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, errors="replace", timeout=5,
        ).stdout
        first = out.split()
        return int(first[0]) if first and first[0].isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stops a launched service and everything it spawned.

    The shell is only the root of the tree - `npm run start` is a shell running npm
    running node - so killing just `proc` would orphan the process actually holding the
    port. Windows: taskkill /T walks the tree. POSIX: the service was started as its own
    session, so its pid is its process-group id and one killpg reaches the whole group;
    SIGTERM first so servers can shut down cleanly, SIGKILL for whatever is left after
    STOP_GRACE_SECONDS."""
    if os.name == "nt":
        if proc.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=30,
                )
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=STOP_GRACE_SECONDS)
        return

    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_GRACE_SECONDS)
        return
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=STOP_GRACE_SECONDS)
    # The leader is reaped; children of the group may still be shutting down. Give them
    # what is left of the grace period, then kill whatever remains.
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=STOP_GRACE_SECONDS)


def _group_kwargs() -> dict:
    """Popen kwargs that put the child in its own process group (see _kill_tree)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _dependency_order(local: LocalEnvSpec) -> list[LocalServiceSpec]:
    """Every service after every service it depends on (config guarantees no cycles),
    otherwise in declaration order. Resources are not services; they are provisioned by
    whichever start needs them first."""
    by_id = {s.id: s for s in local.services}
    ordered: list[LocalServiceSpec] = []
    seen: set[str] = set()

    def visit(service: LocalServiceSpec) -> None:
        if service.id in seen:
            return
        seen.add(service.id)
        for dep in service.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        ordered.append(service)

    for service in local.services:
        visit(service)
    return ordered


def _check_generated(name: str, value: str) -> str:
    """The allowlist, for a value PM Studio produced rather than a caller (see the
    module docstring). Raises LocalEnvError naming the placeholder."""
    if len(value) > MAX_GENERATED_LENGTH or not PARAM_VALUE.match(value):
        raise LocalEnvError(
            f"{{{name}}} = {_quote_note(value)!r} cannot be substituted: generated values "
            "must also contain only letters, digits, spaces and , . _ : / @ = - because "
            "the command runs through the shell."
        )
    return value


def render(
    template: str, values: dict[str, str], what: str, trusted: frozenset[str] | set[str] = frozenset()
) -> str:
    """Fills every placeholder in `template` from `values`. A placeholder with no value
    is an error rather than left as literal braces: config validation guarantees each
    one is resolvable in principle, so a gap here is a runtime fact worth reporting (a
    resource that did not print the key, a dependency with no port yet).

    Every substituted value not named in `trusted` (the params, which effective_params
    already vetted or the operator wrote) passes _check_generated first - and only the
    ones a template actually uses, so an unusual repo path cannot break a service that
    never mentions {worktree}."""
    missing: list[str] = []

    def fill(match: re.Match) -> str:
        token = match.group(1)
        if token in values:
            value = values[token]
            if token not in trusted:
                _check_generated(token, value)
            return value
        missing.append(token)
        return match.group(0)

    rendered = LOCAL_PLACEHOLDER.sub(fill, template)
    if missing:
        raise LocalEnvError(
            f"{what}: no value for {', '.join('{' + m + '}' for m in sorted(set(missing)))}."
        )
    return rendered


def _ensure_system_checkout(
    repo_root: Path, worktree: Path, system_path: str, link_paths: tuple[str, ...]
) -> list[str]:
    """The default checkout preparation: session_checkout.ensure_system_checkout.
    Imported here, lazily and by name, so this module (and every deployment that never
    runs a non-default session environment) does not depend on it at import time."""
    module = importlib.import_module(f"{__package__}.session_checkout")
    return module.ensure_system_checkout(repo_root, worktree, system_path, tuple(link_paths))


class LocalEnvManager:
    """Owns every local-environment process this server has started, and every resource
    it has provisioned.

    One instance per server (server.py), built from the systems that declare `local`.
    All state lives behind one re-entrant lock (re-entrant because starting a service
    starts its dependencies through the same path); the lock is never held while
    waiting on a probe, running a resource command or a service's `setup`, preparing a
    checkout or killing a process tree, so status reads stay instant.

    Starting never blocks on readiness: start() validates, refuses a taken port or a
    session over the limit, launches (or queues the launch behind dependencies,
    resources and checkout preparation that are still coming up) and returns the
    "starting" state at once. Background threads move it to running / failed / exited;
    callers poll status(), or block in wait() until the outcome is known.

    Every public method takes `session` (default "default"): which chat session's
    environment it acts on. Shared services and resources resolve to the one shared
    copy whichever session asks. Each such call is also that session's activity, which
    is what idle stops and making room at the limit go by (see the module docstring).
    """

    def __init__(
        self,
        systems: dict[str, SystemSpec],
        repo_root: Path,
        log_dir: Path | None = None,
        *,
        session_lookup: SessionLookup | None = None,
        state_path: Path | None = None,
        max_session_envs: int = DEFAULT_MAX_SESSION_ENVS,
        idle_stop_minutes: int = 0,
        session_busy: SessionBusy | None = None,
        prepare_checkout: PrepareCheckout | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._systems: dict[str, LocalEnvSpec] = {
            system_id: spec.local
            for system_id, spec in systems.items()
            if spec.local is not None
        }
        self._labels = {system_id: systems[system_id].label for system_id in self._systems}
        self._paths = {system_id: systems[system_id].path for system_id in self._systems}
        self._repo_root = Path(repo_root)
        self._log_dir = Path(log_dir) if log_dir is not None else LOG_DIR
        self._session_lookup = session_lookup or self._default_lookup
        self._state_path = Path(state_path) if state_path is not None else None
        self._max_session_envs = max_session_envs
        self._idle_stop_minutes = idle_stop_minutes
        self._session_busy = session_busy or (lambda session_id: False)
        self._prepare_checkout = prepare_checkout or _ensure_system_checkout
        # Wall clock for resource creation times, expiry and session activity;
        # injectable for tests.
        self._clock = clock or time.time
        self._shared_ctx = SessionContext(SHARED, SHARED, self._repo_root, primary=True)
        # {repo_root}: the primary checkout, where the deployment's own tooling lives.
        self._repo_root_value = self._repo_root.resolve().as_posix()
        self._lock = threading.RLock()
        # Signalled on every state change, so a launch queued behind a dependency
        # wakes the moment that dependency becomes running (or fails).
        self._changed = threading.Condition(self._lock)
        self._runs: dict[tuple[str, str, str], _Run] = {}
        self._resources: dict[tuple[str, str, str], _Resource] = {}
        self._preps: dict[tuple[str, str], _Prep] = {}
        # Rendered setup command -> the lock that keeps two launches (two sessions
        # starting the same service, say) from running it at the same time: it runs in
        # the primary checkout, and two concurrent `npm install`s in one directory
        # trample each other.
        self._setup_locks: dict[str, threading.Lock] = {}
        # Last known display name per session id, for the overview of runs whose
        # session the lookup no longer knows.
        self._session_names: dict[str, str] = {}
        # Persisted resource entries for systems/resources no longer declared: kept and
        # rewritten untouched, so editing the config never silently forgets a database
        # someone will want to drop by hand.
        self._orphans: list[dict] = []
        self._loaded = False
        self._atexit_registered = False
        # Resource create/destroy threads, so shutdown can let them finish writing.
        self._workers: set[threading.Thread] = set()
        # session id -> self._clock() of its most recent local-env call (or of the last
        # time it was seen busy). In memory only: after a restart nothing is running,
        # so there is nothing to be idle.
        self._last_activity: dict[str, float] = {}
        # (scope, system id, service id) -> its log's size at the last idle check, so
        # new output counts as activity (_note_output_locked).
        self._log_sizes: dict[tuple[str, str, str], int] = {}
        # The idle-stop thread (start_idle_reaper) and what tells it to stop.
        self._reaper: threading.Thread | None = None
        self._reaper_stop = threading.Event()

    # ---- sessions and scopes ----

    def _default_lookup(self, session_id: str) -> SessionContext | None:
        if session_id == DEFAULT_SESSION_ID:
            return SessionContext(DEFAULT_SESSION_ID, DEFAULT_SESSION_ID, self._repo_root, primary=True)
        return None

    def _session(self, session_id: str | None) -> SessionContext:
        """Resolves a caller's session id. Called OUTSIDE the manager lock: the lookup
        belongs to the session machinery, and its locks are not ours to nest.

        Every public call made on behalf of a session comes through here, which makes
        this the one place that records the session's activity - a read counts as much
        as a start: an agent polling its service's status is using it."""
        sid = (session_id or DEFAULT_SESSION_ID).strip() or DEFAULT_SESSION_ID
        ctx = self._session_lookup(sid) if sid != SHARED else None
        if ctx is None:
            raise UnknownSession(f"Unknown session {sid!r} (or it no longer has a worktree).")
        with self._lock:
            self._session_names[ctx.id] = ctx.name
            self._touch_locked(ctx.id)
        return ctx

    def _touch_locked(self, session_id: str, now: float | None = None) -> None:
        # max(): a reap pass running with an explicit `now` must never move a session's
        # activity backwards past a call it made in the meantime.
        now = self._clock() if now is None else now
        self._last_activity[session_id] = max(now, self._last_activity.get(session_id, now))

    def _note_output_locked(self, scope: str, now: float) -> None:
        """Counts new output from a session's live services as that session's activity.
        A person using what the environment serves - clicking through the app in a
        browser - makes no local-env call, but the services they reach log it (the
        EpicRide gateway writes a line per proxied request), while services nobody
        uses stay silent. Goes by log SIZE seen at the previous pass, not mtime, so the
        injected clock stays the only time source; the first sighting only records a
        baseline (starting the service was activity already)."""
        grew = False
        for (run_scope, system_id, service_id), run in self._runs.items():
            if run_scope != scope or run.state not in _LIVE:
                continue
            key = (run_scope, system_id, service_id)
            try:
                size = self.log_path(system_id, service_id, scope).stat().st_size
            except OSError:
                continue
            previous = self._log_sizes.get(key)
            self._log_sizes[key] = size
            if previous is not None and size > previous:
                grew = True
        if grew:
            self._touch_locked(scope, now)

    def _ctx_for(self, spec: LocalServiceSpec | LocalResourceSpec, ctx: SessionContext) -> SessionContext:
        """The context an entry actually runs in: the shared one for shared entries,
        the caller's session otherwise."""
        return self._shared_ctx if spec.shared else ctx

    def _key(self, system_id: str, spec, ctx: SessionContext) -> tuple[str, str, str]:
        return (self._ctx_for(spec, ctx).id, system_id, spec.id)

    # ---- lookup and validation ----

    @property
    def system_ids(self) -> list[str]:
        return list(self._systems)

    @property
    def max_session_envs(self) -> int:
        return self._max_session_envs

    def _local(self, system_id: str) -> LocalEnvSpec:
        local = self._systems.get(system_id)
        if local is None:
            raise UnknownTarget(f"System {system_id!r} declares no local environment.")
        return local

    def _service(self, system_id: str, service_id: str) -> tuple[LocalEnvSpec, LocalServiceSpec]:
        local = self._local(system_id)
        service = local.service(service_id)
        if service is None:
            known = ", ".join(s.id for s in local.services) or "none"
            raise UnknownTarget(
                f"System {system_id!r} has no local service {service_id!r} (declared: {known})."
            )
        return local, service

    def _resource_spec(self, system_id: str, resource_id: str) -> LocalResourceSpec:
        local = self._local(system_id)
        resource = local.resource(resource_id)
        if resource is None:
            known = ", ".join(r.id for r in local.resources) or "none"
            raise UnknownTarget(
                f"System {system_id!r} has no local resource {resource_id!r} (declared: {known})."
            )
        return resource

    def _run(self, key: tuple[str, str, str]) -> _Run:
        return self._runs.setdefault(key, _Run())

    @staticmethod
    def effective_params(service: LocalServiceSpec, given: dict | None) -> dict[str, str]:
        """Declared defaults with the caller's values laid over them, every value
        validated. An undeclared name is refused rather than ignored: a caller that
        thinks it set `apis` but misspelt it would otherwise get the default silently."""
        params = service.param_defaults
        if given is None:
            return params
        if not isinstance(given, dict):
            raise InvalidParams(f"params for {service.id!r} must be an object of name -> value")
        for name, value in given.items():
            if name not in params:
                declared = ", ".join(params) or "none"
                raise InvalidParams(
                    f"Service {service.id!r} has no param {name!r} (declared: {declared})."
                )
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise InvalidParams(f"Param {name!r} must be a string, got {type(value).__name__}.")
            text = str(value)
            if len(text) > MAX_PARAM_LENGTH or not PARAM_VALUE.match(text):
                raise InvalidParams(
                    f"Param {name!r} = {_quote_note(text)!r} is not allowed: values may only "
                    "contain letters, digits, spaces and , . _ : / @ = - "
                    f"(max {MAX_PARAM_LENGTH} characters), because the command runs "
                    "through the shell."
                )
            params[name] = text
        return params

    def log_path(self, system_id: str, item_id: str, scope: str = DEFAULT_SESSION_ID) -> Path:
        """Where a service's (or a resource's create/destroy) output is appended. One
        file per scope, so two sessions running the same service never interleave."""
        return self._log_dir / f"{_safe_name(scope)}-{system_id}-{item_id}.log"

    # ---- state transitions ----

    def _set(self, key: tuple[str, str, str], generation: int, **fields) -> bool:
        """Applies `fields` to the run only if it still belongs to `generation`."""
        with self._changed:
            run = self._runs.get(key)
            if run is None or run.generation != generation:
                return False
            for name, value in fields.items():
                setattr(run, name, value)
            self._changed.notify_all()
            return True

    def _set_resource(self, key: tuple[str, str, str], generation: int, save: bool = False, **fields) -> bool:
        with self._changed:
            rec = self._resources.get(key)
            if rec is None or rec.generation != generation:
                return False
            for name, value in fields.items():
                setattr(rec, name, value)
            if save:
                self._save_locked()
            self._changed.notify_all()
            return True

    def _register_atexit(self) -> None:
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.shutdown)

    # ---- the session-environment limit ----

    def _active_envs_locked(self) -> set[tuple[str, str]]:
        """(session id, system id) of every session environment with a service starting
        or running. Shared runs are not anyone's session environment."""
        return {
            (scope, system_id)
            for (scope, system_id, _), run in self._runs.items()
            if scope != SHARED and run.state in _LIVE
        }

    def _limit_reached_locked(self, ctx: SessionContext, system_id: str) -> bool:
        active = self._active_envs_locked()
        return (ctx.id, system_id) not in active and len(active) >= self._max_session_envs

    def _check_limit_locked(self, ctx: SessionContext, system_id: str) -> None:
        if not self._limit_reached_locked(ctx, system_id):
            return
        active = self._active_envs_locked()
        running = ", ".join(
            f"{self._session_names.get(scope, scope)} ({scope}) / {system}"
            for scope, system in sorted(active)
        )
        raise LimitReached(
            f"At most {self._max_session_envs} session environment(s) may run at once "
            f"([local_env] max_session_envs); running: {running}. Stop one of them first."
        )

    def _make_room(self, ctx: SessionContext, system_id: str) -> None:
        """Frees a slot for `ctx` when the limit is reached, by stopping the session-
        scoped services of the least recently active OTHER session that is not busy and
        has been idle at least MAKE_ROOM_IDLE_SECONDS. Does nothing when no session
        qualifies: the caller's own limit check then refuses as before, naming who
        holds the slots. Called WITHOUT the lock - it asks session_busy and kills
        process trees - and re-checks everything under it before acting, because the
        world may have moved while it was not held."""
        now = self._clock()
        with self._lock:
            if not self._limit_reached_locked(ctx, system_id):
                return
            others = {scope for scope, _ in self._active_envs_locked()} - {ctx.id}
            for scope in others:
                self._note_output_locked(scope, now)
            candidates = sorted((self._last_activity.get(scope, now), scope) for scope in others)
        for last, scope in candidates:
            if now - last < MAKE_ROOM_IDLE_SECONDS:
                break  # sorted oldest first: nobody after this one is idle long enough
            if self._busy(scope):
                with self._lock:
                    self._touch_locked(scope, now)
                continue
            with self._lock:
                if not self._limit_reached_locked(ctx, system_id):
                    return
                idle = now - self._last_activity.get(scope, now)
                if idle < MAKE_ROOM_IDLE_SECONDS:
                    continue  # it made a call while we were asking whether it is busy
                message = (
                    f"Stopped to make room for session {ctx.name} (limit of "
                    f"{self._max_session_envs} session environments reached) after "
                    f"{int(idle // 60)} min without use; start it again when needed."
                )
                stopped, procs = self._stop_session_runs_locked(scope, message)
            self._kill_all(procs)
            self._log_stop(scope, stopped, f"to make room for session {ctx.id}")
            return

    def _busy(self, session_id: str) -> bool:
        """session_busy, never under the lock (it belongs to the session machinery).
        A hook that raises counts as busy: stopping an environment out from under a
        running dev task is the expensive mistake; keeping an idle one a little longer
        is not."""
        try:
            return bool(self._session_busy(session_id))
        except Exception:
            return True

    def _needs_session_env(self, local: LocalEnvSpec, service_ids: list[str]) -> bool:
        """Whether starting these services (and what they depend on) would bring up any
        session-scoped service, i.e. count against the limit."""
        for service_id in service_ids:
            for item in [service_id, *local.closure(service_id)]:
                spec = local.service(item)
                if spec is not None and not spec.shared:
                    return True
        return False

    # ---- persistence (resources) ----

    def _ensure_loaded_locked(self) -> None:
        """Reads the persisted resource outputs once, on first need - never at
        construction, so importing server.py touches no file."""
        if self._loaded:
            return
        self._loaded = True
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[local_env] ignoring unreadable {self._state_path}: {exc}", file=sys.stderr)
            return
        for entry in data.get("resources", []) if isinstance(data, dict) else []:
            if not isinstance(entry, dict):
                continue
            scope, system_id, resource_id = (
                str(entry.get("scope", "")), str(entry.get("system", "")), str(entry.get("resource", ""))
            )
            outputs = entry.get("outputs")
            local = self._systems.get(system_id)
            if (
                not scope
                or local is None
                or local.resource(resource_id) is None
                or not isinstance(outputs, dict)
            ):
                self._orphans.append(entry)
                continue
            failed = entry.get("state") == FAILED
            self._resources[(scope, system_id, resource_id)] = _Resource(
                state=FAILED if failed else READY,
                outputs={str(k): str(v) for k, v in outputs.items()},
                message=str(entry.get("message", "")) if failed else "",
                created_at=entry.get("created_at"),
            )

    def _save_locked(self) -> None:
        """Writes every resource that holds outputs (ready, or failed to destroy) with an
        atomic replace. Best effort: a write failure costs reuse after a restart, never
        the running environment."""
        if self._state_path is None:
            return
        entries = [
            {
                "scope": scope,
                "system": system_id,
                "resource": resource_id,
                "state": rec.state if rec.state == FAILED else READY,
                "outputs": rec.outputs,
                "created_at": rec.created_at,
                "message": rec.message if rec.state == FAILED else "",
            }
            for (scope, system_id, resource_id), rec in self._resources.items()
            if rec.outputs and rec.state in (READY, FAILED, DESTROYING)
        ]
        payload = {"version": STATE_FILE_VERSION, "resources": entries + self._orphans}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            print(f"[local_env] could not save {self._state_path}: {exc}", file=sys.stderr)

    def _resource_record(self, key: tuple[str, str, str]) -> _Resource:
        self._ensure_loaded_locked()
        return self._resources.setdefault(key, _Resource())

    # ---- session checkout preparation ----

    def _prep_status_locked(self, ctx: SessionContext, system_id: str) -> dict:
        if ctx.primary:
            return {"state": PREP_NOT_NEEDED, "notes": [], "message": ""}
        prep = self._preps.get((ctx.id, system_id))
        if prep is None:
            return {"state": PREP_PENDING, "notes": [], "message": ""}
        return {"state": prep.state, "notes": list(prep.notes), "message": prep.message}

    def _prep_ready_locked(self, ctx: SessionContext, system_id: str) -> bool:
        prep = self._preps.get((ctx.id, system_id))
        return ctx.primary or (prep is not None and prep.state == PREP_READY)

    def _ensure_prepared(self, ctx: SessionContext, system_id: str) -> None:
        """Makes a NON-default session's worktree able to run this system, once per
        (session, system): a fresh `git worktree` of the main repo has empty submodule
        folders and no node_modules, so without this the first `npm start` there fails
        on a missing package.json. The work itself is session_checkout's (git worktrees
        for the submodules under the system's path, links for `link_from_primary`);
        this only sequences it, caches success and reports failure. A failure is not
        cached: the next start tries again. Blocking - called from background threads
        only, never under the manager lock.

        Once prepared it is still re-run (idempotent, quick) on every later start, to
        pick up `link_from_primary` dirs that appeared in the primary checkout since -
        dependencies someone installed there after this session began. A refresh that
        fails leaves the working checkout alone and says so in the notes."""
        if ctx.primary:
            return
        key = (ctx.id, system_id)
        with self._lock:
            prep = self._preps.setdefault(key, _Prep())
            local = self._local(system_id)
        with prep.lock:
            with self._changed:
                if prep.state == PREP_READY:
                    refresh = True
                else:
                    refresh = False
                    prep.state = PREP_PREPARING
                prep.message = ""
                self._changed.notify_all()
            try:
                notes = self._prepare_checkout(
                    self._repo_root, ctx.worktree, self._paths.get(system_id, ""),
                    tuple(local.link_from_primary),
                )
            except Exception as exc:  # CheckoutError, or anything the hook did not expect
                if refresh:
                    with self._changed:
                        prep.notes = [*prep.notes, f"refreshing links failed (kept as it was): {exc}"]
                        self._changed.notify_all()
                    return
                with self._changed:
                    prep.state = FAILED
                    prep.message = f"Could not prepare session {ctx.id}'s checkout for {system_id}: {exc}"
                    self._changed.notify_all()
                    message = prep.message
                raise LocalEnvError(message) from exc
            with self._changed:
                prep.state = PREP_READY
                prep.notes = [str(n) for n in (notes or [])]
                self._changed.notify_all()

    # ---- start ----

    def start(
        self, system_id: str, service_id: str, params: dict | None = None,
        session: str = DEFAULT_SESSION_ID,
    ) -> dict:
        """Starts one service, its dependencies (services and resources) first.
        Returns the service's status at once - normally "starting"; poll status() for
        the outcome.

        A service already starting or running is left alone (that is how a dependency
        shared by two services, or a shared service used by two sessions, is reused).
        Raises InvalidParams (400), UnknownTarget/UnknownSession (404), or PortInUse /
        LimitReached / Busy (409)."""
        ctx = self._session(session)
        with self._lock:
            local, service = self._service(system_id, service_id)
            effective = self.effective_params(service, params)
            needs_env = self._needs_session_env(local, [service.id])
        # Validated first, so a request that would be refused anyway evicts nobody.
        if needs_env:
            self._make_room(ctx, system_id)
        with self._lock:
            if needs_env:
                self._check_limit_locked(ctx, system_id)
            self._start_locked(system_id, local, service, effective, ctx)
            return self._service_status(system_id, service, ctx)

    def start_all(self, system_id: str, params: dict | None = None, session: str = DEFAULT_SESSION_ID) -> dict:
        """Starts every service of a system in dependency order. `params` maps service
        id -> that service's params; all of them are validated before anything starts,
        so a bad value never leaves half an environment running."""
        ctx = self._session(session)
        with self._lock:
            local = self._local(system_id)
            if params is not None and not isinstance(params, dict):
                raise InvalidParams("params must be an object of service id -> params")
            params = params or {}
            unknown = sorted(set(params) - {s.id for s in local.services})
            if unknown:
                raise InvalidParams(
                    f"System {system_id!r} has no local service(s) {', '.join(unknown)}."
                )
            effective = {
                s.id: self.effective_params(s, params.get(s.id)) for s in local.services
            }
            needs_env = self._needs_session_env(local, [s.id for s in local.services])
        if needs_env:
            self._make_room(ctx, system_id)
        with self._lock:
            if needs_env:
                self._check_limit_locked(ctx, system_id)
            for service in _dependency_order(local):
                self._start_locked(system_id, local, service, effective[service.id], ctx)
            return self._system_status(system_id, ctx)

    def _start_locked(
        self,
        system_id: str,
        local: LocalEnvSpec,
        service: LocalServiceSpec,
        effective: dict[str, str],
        ctx: SessionContext,
    ) -> None:
        sctx = self._ctx_for(service, ctx)
        key = (sctx.id, system_id, service.id)
        run = self._run(key)
        if run.state in _LIVE:
            return

        # Resources first: refusing on one being destroyed should leave nothing started.
        for dep_id in service.depends_on:
            resource = local.resource(dep_id)
            if resource is not None:
                self._ensure_resource_locked(system_id, resource, ctx)
        for dep_id in service.depends_on:
            dep = local.service(dep_id)
            if dep is None:
                continue
            dep_run = self._run(self._key(system_id, dep, ctx))
            # A dependency that is not up yet starts with the params it last ran with,
            # or its defaults - the caller's params are for the service they named.
            dep_params = {**dep.param_defaults, **dep_run.params}
            self._start_locked(system_id, local, dep, dep_params, ctx)

        port: int | None = None
        if service.fixed_port is not None:
            port = service.fixed_port
            holder = next(
                (
                    other for other, other_run in self._runs.items()
                    if other != key and other_run.port == port and other_run.state in _LIVE
                ),
                None,
            )
            if holder is not None or _port_open(port):
                if holder is not None:
                    scope, h_system, h_service = holder
                    message = (
                        f"Port {port} is held by {h_system}/{h_service} in "
                        f"{'the shared scope' if scope == SHARED else 'session ' + self._session_names.get(scope, scope)}, "
                        "which PM Studio started. A fixed port serves one session at a time: "
                        'stop that one first, or declare port = "auto".'
                    )
                else:
                    owner = _port_owner(port)
                    who = f" (held by PID {owner})" if owner else ""
                    message = (
                        f"Port {port} is already in use{who} by a process PM Studio did "
                        "not start. Stop it (or the copy you started by hand), then start again."
                    )
                run.generation += 1
                run.state = PORT_IN_USE
                run.proc = None
                run.pid = None
                run.exit_code = None
                run.port = None
                run.params = dict(effective)
                run.message = message
                self._changed.notify_all()
                raise PortInUse(f"{system_id}/{service.id}: {message}")
        # An "auto" port is picked at launch, not here: a service can wait a long time
        # (a database being created) before it launches, and a port picked now could be
        # taken by then.

        run.generation += 1
        generation = run.generation
        run.state = STARTING
        run.proc = None
        run.pid = None
        run.started_at = None
        run.exit_code = None
        run.port = port
        run.url = ""
        run.ready_url = ""
        run.resource_snapshot = None
        run.params = dict(effective)
        run.lines = collections.deque(maxlen=LOG_LINES)
        run.lines_lock = threading.Lock()
        run.in_setup = False
        self._changed.notify_all()

        needs_prep = not self._prep_ready_locked(sctx, system_id)
        _, pending, _ = self._dependency_states_locked(system_id, local, service, ctx)
        if not service.setup and not needs_prep and not pending:
            self._launch_locked(key, system_id, local, service, ctx)
            return
        if service.setup:
            run.message = "Running setup..."
        else:
            run.message = (
                "Preparing the session checkout." if needs_prep
                else f"Waiting for {', '.join(pending)} to be ready."
            )
        threading.Thread(
            target=self._launch_after_dependencies,
            args=(key, system_id, local, service, ctx, generation, needs_prep),
            name=f"local-env-wait-{key[0]}-{system_id}-{service.id}",
            daemon=True,
        ).start()

    def _allocate_port_locked(self, prefer: int | None = None) -> int:
        taken = {r.port for r in self._runs.values() if r.port is not None and r.state in _LIVE}
        if prefer is not None and prefer not in taken and not _port_open(prefer):
            return prefer
        for _ in range(50):
            port = _free_port()
            if port not in taken:
                return port
        raise LocalEnvError("Could not find a free TCP port for an auto-port service.")

    def _dependency_states_locked(
        self, system_id: str, local: LocalEnvSpec, service: LocalServiceSpec, ctx: SessionContext
    ) -> tuple[list[str], list[str], list[str]]:
        """(ready, pending, broken) over the service's direct dependencies, each named
        for messages ("api", "db is failed"). A service is ready when running and a
        resource when ready; starting/creating is pending; anything else is broken."""
        ready: list[str] = []
        pending: list[str] = []
        broken: list[str] = []
        for dep_id in service.depends_on:
            dep = local.service(dep_id)
            if dep is not None:
                run = self._runs.get(self._key(system_id, dep, ctx))
                state = run.state if run else STOPPED
                ok, wait = state == RUNNING, state == STARTING
            else:
                resource = local.resource(dep_id)
                if resource is None:  # config rejects this; guards specs built by hand
                    continue
                state, _, _ = self._effective_locked(
                    resource, self._resources.get(self._key(system_id, resource, ctx))
                )
                ok, wait = state == READY, state == CREATING
            if ok:
                ready.append(dep_id)
            elif wait:
                pending.append(dep_id)
            else:
                broken.append(f"{dep_id} is {state}")
        return ready, pending, broken

    def _launch_after_dependencies(
        self,
        key: tuple[str, str, str],
        system_id: str,
        local: LocalEnvSpec,
        service: LocalServiceSpec,
        ctx: SessionContext,
        generation: int,
        needs_prep: bool,
    ) -> None:
        """The launch of a service that cannot go at once, in a background thread, in
        this order: its `setup` (when declared), then the session checkout - prepared
        the first time, and after a setup refreshed again, so dependencies the setup
        just installed in the primary checkout get linked into the session - then the
        wait for its dependencies, then the launch itself."""
        if service.setup and not self._run_setup(key, system_id, service, ctx, generation):
            return
        if needs_prep or service.setup:
            if needs_prep and service.setup:
                if not self._set(key, generation, message="Preparing the session checkout."):
                    return
            try:
                self._ensure_prepared(self._ctx_for(service, ctx), system_id)
            except LocalEnvError as exc:
                self._set(key, generation, state=FAILED, port=None, message=f"Not started: {exc}")
                return
        with self._changed:
            while True:
                run = self._runs.get(key)
                if run is None or run.generation != generation:
                    return  # stopped or relaunched while waiting
                _, pending, broken = self._dependency_states_locked(system_id, local, service, ctx)
                if broken:
                    run.state = FAILED
                    run.port = None
                    run.message = f"Not started: dependency {', '.join(broken)}."
                    self._changed.notify_all()
                    return
                if not pending:
                    self._launch_locked(key, system_id, local, service, ctx)
                    return
                run.message = f"Waiting for {', '.join(pending)} to be ready."
                # Woken by any state change; the timeout is only a safety net.
                self._changed.wait(timeout=5.0)

    def _run_setup(
        self,
        key: tuple[str, str, str],
        system_id: str,
        service: LocalServiceSpec,
        ctx: SessionContext,
        generation: int,
    ) -> bool:
        """Runs the service's `setup` to completion, blocking (background thread only,
        never under the manager lock). Returns whether the launch goes on.

        It runs from the primary checkout (repo root) - where the deployment's tooling
        lives and where `link_from_primary` links point - through the shell, in its own
        process group, with the environment the service itself gets: its `env` entries
        rendered with the values that exist before launch (an entry that needs {port} or
        a dependency's value is left out; config refuses those in `setup` itself). Its
        output goes to the service's log, "[setup] "-prefixed, after a header line.

        The process is held in run.proc for as long as it runs, so a stop, restart,
        idle stop, release or shutdown kills it like a launched service; the launch then
        simply does not happen. A non-zero exit or a timeout marks the run failed."""
        sctx = self._ctx_for(service, ctx)
        what = f"{system_id}/{service.id} setup"
        with self._changed:
            run = self._runs.get(key)
            if run is None or run.generation != generation:
                return False
            values = {
                **run.params,
                "worktree": sctx.worktree.resolve().as_posix(),
                "repo_root": self._repo_root_value,
            }
            if not service.shared:
                values["session"] = sctx.id
            trusted = set(run.params)
            try:
                command = render(service.setup, values, what, trusted)
                env_vars = {
                    name: render(template, values, what, trusted)
                    for name, template in service.env
                    if all(token in values for token in LOCAL_PLACEHOLDER.findall(template))
                }
            except LocalEnvError as exc:
                run.state = FAILED
                run.port = None
                run.message = f"Not started: {exc}"
                self._changed.notify_all()
                return False
            run.message = "Running setup..."
            self._changed.notify_all()
            lines, lines_lock = run.lines, run.lines_lock
            setup_lock = self._setup_locks.setdefault(command, threading.Lock())

        # The same setup already running for another launch: wait for it (it is likely
        # to leave nothing to do), while still noticing a stop.
        noted = False
        while not setup_lock.acquire(timeout=POLL_SECONDS):
            with self._changed:
                run = self._runs.get(key)
                if run is None or run.generation != generation:
                    return False
                if not noted:
                    noted = True
                    run.message = "Running setup... (waiting for the same setup, already running, to finish)"
                    self._changed.notify_all()
        try:
            return self._run_setup_locked_out(
                key, generation, service, command, env_vars, lines, lines_lock
            )
        finally:
            setup_lock.release()

    def _run_setup_locked_out(
        self,
        key: tuple[str, str, str],
        generation: int,
        service: LocalServiceSpec,
        command: str,
        env_vars: dict[str, str],
        lines: collections.deque,
        lines_lock: threading.Lock,
    ) -> bool:
        """_run_setup's process part, run holding that command's setup lock (and NOT
        the manager lock)."""
        if not self._set(key, generation, message="Running setup..."):
            return False
        cwd = self._repo_root
        log_path = self.log_path(key[1], service.id, key[0])
        handle = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path.open("a", encoding="utf-8", errors="replace")
        except OSError:
            handle = None  # memory-only; the tail still works

        def emit(line: str) -> None:
            with lines_lock:
                lines.append(line)
                if handle is not None:
                    with contextlib.suppress(OSError, ValueError):
                        handle.write(line + "\n")
                        handle.flush()

        def close() -> None:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()

        emit(f"[pm-studio] {time.strftime('%Y-%m-%d %H:%M:%S')} setup in {cwd}: {command}")
        env = dict(os.environ)
        env.update(env_vars)
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_group_kwargs(),
            )
        except OSError as exc:
            emit(f"[pm-studio] setup could not be launched: {exc}")
            close()
            self._set(key, generation, state=FAILED, port=None, message=f"Setup could not be launched: {exc}")
            return False
        with self._changed:
            run = self._runs.get(key)
            superseded = run is None or run.generation != generation
            if not superseded:
                run.proc = proc
                run.in_setup = True
                self._changed.notify_all()
        if superseded:
            # Stopped between the check above and the launch: nobody wants it now.
            _kill_tree(proc)
            close()
            return False
        self._register_atexit()

        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(service.setup_timeout, expire)
        timer.daemon = True
        timer.start()
        last = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line.strip():
                    last = line.strip()
                emit("[setup] " + line)
        except (OSError, ValueError):
            pass  # pipe closed under us by a stop
        code = proc.wait()
        timer.cancel()
        emit(f"[pm-studio] setup exited with code {code}")
        close()

        with self._changed:
            run = self._runs.get(key)
            if run is None or run.generation != generation:
                return False  # stopped or relaunched while it ran; the stop killed it
            run.proc = None
            run.in_setup = False
            if timed_out.is_set():
                message = f"Setup did not finish within {service.setup_timeout:g}s; stopped it."
            elif code != 0:
                tail = last if len(last) <= 300 else last[:297] + "..."
                message = f"Setup failed (exit {code})" + (f": {tail}" if tail else ".")
            else:
                run.message = ""
                self._changed.notify_all()
                return True
            run.state = FAILED
            run.port = None
            run.pid = None
            run.message = message
            self._changed.notify_all()
            return False

    def _values_locked(
        self,
        system_id: str,
        local: LocalEnvSpec,
        service: LocalServiceSpec,
        ctx: SessionContext,
        run: _Run,
    ) -> dict[str, str]:
        """Every placeholder value available to this service right now: its params,
        its own port, {session}/{worktree}, and the ports and outputs of everything it
        depends on (resolved in the shared scope or this session, whichever the
        dependency lives in). Generated values are checked against the allowlist by
        render(), when substituted; params were checked on the way in."""
        sctx = self._ctx_for(service, ctx)
        generated: dict[str, str] = {
            "worktree": sctx.worktree.resolve().as_posix(),
            "repo_root": self._repo_root_value,
        }
        if not service.shared:
            generated["session"] = sctx.id
        port = run.port if run.port is not None else service.fixed_port
        if port is not None:
            generated["port"] = str(port)
        for dep_id in local.closure(service.id):
            dep = local.service(dep_id)
            if dep is not None:
                dep_run = self._runs.get(self._key(system_id, dep, ctx))
                if dep_run is not None and dep_run.port is not None and dep_run.state in _LIVE:
                    generated[f"{dep_id}.port"] = str(dep_run.port)
        for rid in local.consumed_resources(service.id):
            resource = local.resource(rid)
            if resource is None:
                continue
            state, outputs, _ = self._effective_locked(
                resource, self._resources.get(self._key(system_id, resource, ctx))
            )
            if state == READY:
                for name, value in outputs.items():
                    generated[f"{rid}.{name}"] = value
        # A resource the service only `uses` may legitimately not exist: every one of
        # its placeholders the templates mention then reads as "" (the service's cue
        # to fall back, e.g. to the shared DEV database). A `depends_on` resource gets
        # no such blank - a gap there is an error worth reporting.
        if service.uses:
            templates = [service.command, service.url, service.ready_url, *(v for _, v in service.env)]
            for template in templates:
                for token in LOCAL_PLACEHOLDER.findall(template):
                    if "." in token and token.split(".", 1)[0] in service.uses:
                        generated.setdefault(token, "")
        return {**run.params, **generated}

    def _launch_locked(
        self,
        key: tuple[str, str, str],
        system_id: str,
        local: LocalEnvSpec,
        service: LocalServiceSpec,
        ctx: SessionContext,
    ) -> None:
        run = self._runs[key]
        generation = run.generation
        sctx = self._ctx_for(service, ctx)
        what = f"{system_id}/{service.id}"
        try:
            if service.auto_port:
                run.port = self._allocate_port_locked(prefer=run.last_port)
                run.last_port = run.port
            values = self._values_locked(system_id, local, service, ctx, run)
            trusted = set(run.params)
            command = render(service.command, values, what, trusted)
            env_vars = {name: render(value, values, what, trusted) for name, value in service.env}
            ready_url = render(service.ready_url, values, what, trusted)
            url = render(service.url, values, what, trusted)
            snapshot = self._resource_snapshot_locked(system_id, local, service, ctx)
        except LocalEnvError as exc:
            run.state = FAILED
            run.port = None
            run.message = f"Not started: {exc}"
            self._changed.notify_all()
            return
        cwd = (sctx.worktree / service.cwd).resolve()
        if not cwd.is_dir():
            run.state = FAILED
            run.port = None
            run.message = f"Working directory {cwd} does not exist."
            self._changed.notify_all()
            return

        env = dict(os.environ)
        env.update(env_vars)
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_group_kwargs(),
            )
        except OSError as exc:
            run.state = FAILED
            run.port = None
            run.message = f"Could not launch: {exc}"
            self._changed.notify_all()
            return

        self._register_atexit()
        run.proc = proc
        run.pid = proc.pid
        run.started_at = time.time()
        run.url = url
        run.ready_url = ready_url
        run.resource_snapshot = snapshot
        run.message = ""
        self._changed.notify_all()
        header = f"[pm-studio] {time.strftime('%Y-%m-%d %H:%M:%S')} starting in {cwd}: {command}"
        with run.lines_lock:
            run.lines.append(header)
        # A worker, so shutdown waits (bounded) for it to close the log file: on Windows an
        # open file cannot be removed, and the log outliving its process is a leak.
        self._spawn_worker(
            self._pump,
            (proc, run.lines, run.lines_lock, self.log_path(system_id, service.id, key[0]), header),
            f"local-env-log-{key[0]}-{system_id}-{service.id}",
        )
        threading.Thread(
            target=self._watch,
            args=(key, generation, proc, service, run.port, ready_url),
            name=f"local-env-watch-{key[0]}-{system_id}-{service.id}",
            daemon=True,
        ).start()

    # ---- resources ----

    def _expired_boundary_locked(self, resource: LocalResourceSpec, rec: _Resource) -> datetime | None:
        """The boundary that killed a ready resource, or None while it is still alive."""
        if not resource.expires or rec.state != READY or rec.created_at is None:
            return None
        boundary = last_expiry_boundary(self._clock(), resource.expires_at, resource.timezone)
        return boundary if rec.created_at < boundary.timestamp() else None

    def _effective_locked(
        self, resource: LocalResourceSpec | None, rec: _Resource | None
    ) -> tuple[str, dict[str, str], str]:
        """(state, outputs, message) as everyone outside the record sees it: a ready
        resource past its daily expiry is EXPIRED with no usable outputs. The record
        itself keeps them (and so does resources.json) until a new create overwrites
        them - nothing is lost by merely reading the clock."""
        if rec is None:
            return ABSENT, {}, ""
        boundary = self._expired_boundary_locked(resource, rec) if resource is not None else None
        if boundary is not None:
            return (
                EXPIRED, {},
                f"dropped at {boundary.isoformat(timespec='minutes')} by the nightly job; create it again",
            )
        return rec.state, dict(rec.outputs), rec.message

    def _resource_snapshot_locked(
        self, system_id: str, local: LocalEnvSpec, service: LocalServiceSpec, ctx: SessionContext
    ) -> dict:
        snapshot: dict = {}
        for rid in local.consumed_resources(service.id):
            resource = local.resource(rid)
            if resource is None:
                continue
            state, outputs, _ = self._effective_locked(
                resource, self._resources.get(self._key(system_id, resource, ctx))
            )
            snapshot[rid] = tuple(sorted(outputs.items())) if state == READY else None
        return snapshot

    def create_resource(self, system_id: str, resource_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        """Explicitly provisions a resource in the caller's scope - the way to get one
        a service only `uses`. Runs in the background exactly like an on-demand create
        (same command, timeout, logs, persistence) and answers "creating" at once. 409
        when it is already creating, ready (and not expired) or being destroyed. A
        service already running keeps what it was launched with: restart it (its
        status says restart_needed) to pick the new outputs up."""
        ctx = self._session(session)
        with self._lock:
            resource = self._resource_spec(system_id, resource_id)
            key = self._key(system_id, resource, ctx)
            rec = self._resource_record(key)
            state, _, _ = self._effective_locked(resource, rec)
            if state in (CREATING, READY, DESTROYING):
                raise Busy(f"{system_id}/{resource.id} is already {state} in this scope.")
            self._ensure_resource_locked(system_id, resource, ctx)
            return self._resource_status(system_id, resource, ctx)

    def _ensure_resource_locked(
        self, system_id: str, resource: LocalResourceSpec, ctx: SessionContext
    ) -> None:
        """Kicks off creation of a resource that is not there yet; a ready or creating
        one is left alone (that is the reuse). A failed one is retried - the caller is
        starting something that needs it, which is the retry."""
        rctx = self._ctx_for(resource, ctx)
        key = (rctx.id, system_id, resource.id)
        rec = self._resource_record(key)
        # An expired one falls through and is created again: it no longer exists.
        if rec.state == CREATING or (
            rec.state == READY and self._expired_boundary_locked(resource, rec) is None
        ):
            return
        if rec.state == DESTROYING:
            raise Busy(
                f"{system_id}/{resource.id} is being destroyed; start again once it is gone."
            )
        rec.generation += 1
        rec.state = CREATING
        rec.outputs = {}
        rec.created_at = None
        rec.message = "Preparing the session checkout." if not rctx.primary else ""
        rec.lines = collections.deque(maxlen=LOG_LINES)
        rec.lines_lock = threading.Lock()
        self._changed.notify_all()
        self._spawn_worker(
            self._create_resource, (key, system_id, resource, rctx, rec.generation),
            f"local-env-create-{key[0]}-{system_id}-{resource.id}",
        )

    def _spawn_worker(self, target, args: tuple, name: str) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._workers = {t for t in self._workers if t.is_alive()}
        self._workers.add(thread)
        thread.start()
        return thread

    def _run_resource_command(
        self,
        key: tuple[str, str, str],
        generation: int,
        command: str,
        timeout: float,
        phase: str,
    ) -> tuple[int | None, list[str], bool]:
        """Runs a resource's create or destroy from the repo root, its stdout and
        stderr both copied to the resource's log (stderr lines prefixed), stdout also
        collected for the caller. Returns (exit code, stdout lines, timed out); the
        exit code is None when it could not even be launched or was superseded."""
        with self._lock:
            rec = self._resources.get(key)
            if rec is None or rec.generation != generation:
                return None, [], False
            lines, lines_lock = rec.lines, rec.lines_lock
        log_path = self.log_path(key[1], key[2], key[0])
        header = f"[pm-studio] {time.strftime('%Y-%m-%d %H:%M:%S')} {phase} in {self._repo_root}: {command}"
        handle = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path.open("a", encoding="utf-8", errors="replace")
        except OSError:
            handle = None

        def emit(line: str) -> None:
            with lines_lock:
                lines.append(line)
                if handle is not None:
                    with contextlib.suppress(OSError, ValueError):
                        handle.write(line + "\n")
                        handle.flush()

        emit(header)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self._repo_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_group_kwargs(),
            )
        except OSError as exc:
            emit(f"[pm-studio] could not launch: {exc}")
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
            return -1, [f"could not launch: {exc}"], False
        with self._lock:
            rec = self._resources.get(key)
            superseded = rec is None or rec.generation != generation
            if not superseded:
                rec.proc = proc
        if superseded:
            # Released or shut down between the check above and the launch: nobody will
            # read the result, so do not let it run on unattended.
            _kill_tree(proc)
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
            return None, [], False
        self._register_atexit()

        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()

        def pump_stderr() -> None:
            try:
                assert proc.stderr is not None
                for raw in proc.stderr:
                    emit("[stderr] " + raw.rstrip("\r\n"))
            except (OSError, ValueError):
                pass

        err_thread = threading.Thread(target=pump_stderr, daemon=True)
        err_thread.start()
        stdout: list[str] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                stdout.append(line)
                emit(line)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        timer.cancel()
        err_thread.join(timeout=5)
        emit(f"[pm-studio] {phase} exited with code {code}")
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        with self._lock:
            rec = self._resources.get(key)
            if rec is not None and rec.proc is proc:
                rec.proc = None
        return code, stdout, timed_out.is_set()

    @staticmethod
    def parse_outputs(stdout: list[str], resource: LocalResourceSpec) -> dict[str, str]:
        """The outputs a create reported: its LAST non-empty stdout line, a JSON object
        of string values. Raises LocalEnvError saying exactly what is wrong - each value
        is about to be substituted into shell commands, so anything outside the
        allowlist is refused here, at the source, not at some later launch."""
        last = next((line.strip() for line in reversed(stdout) if line.strip()), "")
        if not last:
            raise LocalEnvError("create printed nothing; its last stdout line must be a JSON object.")
        try:
            data = json.loads(last)
        except ValueError:
            raise LocalEnvError(
                f"create's last stdout line is not JSON ({_quote_note(last)!r}); it must be "
                'a JSON object of string values, e.g. {"name": "..."}.'
            ) from None
        if not isinstance(data, dict):
            raise LocalEnvError(f"create's last stdout line is JSON but not an object ({_quote_note(last)!r}).")
        outputs: dict[str, str] = {}
        for name, value in data.items():
            if not LOCAL_OUTPUT_KEY.match(str(name)):
                raise LocalEnvError(f"output key {name!r} is not allowed (letters, digits and _ only).")
            if not isinstance(value, str):
                raise LocalEnvError(f"output {name!r} must be a string, got {type(value).__name__}.")
            if len(value) > MAX_PARAM_LENGTH or not PARAM_VALUE.match(value):
                raise LocalEnvError(
                    f"output {name!r} = {_quote_note(value)!r} is not allowed: outputs are "
                    "substituted into shell commands, so they may only contain letters, "
                    "digits, spaces and , . _ : / @ = -"
                )
            outputs[str(name)] = value
        missing = [o for o in resource.outputs if o not in outputs]
        if missing:
            raise LocalEnvError(f"create did not report declared output(s) {', '.join(missing)}.")
        return outputs

    def _create_resource(
        self,
        key: tuple[str, str, str],
        system_id: str,
        resource: LocalResourceSpec,
        rctx: SessionContext,
        generation: int,
    ) -> None:
        def fail(message: str) -> None:
            self._set_resource(key, generation, state=FAILED, outputs={}, message=message)

        try:
            self._ensure_prepared(rctx, system_id)
        except LocalEnvError as exc:
            fail(str(exc))
            return
        values = {"worktree": rctx.worktree.resolve().as_posix(), "repo_root": self._repo_root_value}
        if not resource.shared:
            values["session"] = rctx.id
        try:
            command = render(resource.create, values, f"{system_id}/{resource.id} create")
        except LocalEnvError as exc:
            fail(str(exc))
            return
        if not self._set_resource(key, generation, message="Creating."):
            return
        code, stdout, timed_out = self._run_resource_command(
            key, generation, command, resource.timeout, "create"
        )
        if code is None:
            return  # superseded (released or shut down while launching)
        if timed_out:
            fail(f"create did not finish within {resource.timeout:g}s; stopped it.")
            return
        if code != 0:
            tail = next((line.strip() for line in reversed(stdout) if line.strip()), "")
            fail(f"create exited with code {code}." + (f" Last output: {_quote_note(tail)}" if tail else ""))
            return
        try:
            outputs = self.parse_outputs(stdout, resource)
        except LocalEnvError as exc:
            fail(str(exc))
            return
        self._set_resource(
            key, generation, save=True,
            state=READY, outputs=outputs, created_at=self._clock(), message="",
        )

    def _live_dependents_locked(self, system_id: str, resource: LocalResourceSpec, scope: str) -> list[str]:
        local = self._systems[system_id]
        return sorted(
            f"{run_scope}/{service_id}"
            for (run_scope, run_system, service_id), run in self._runs.items()
            if run_system == system_id
            and run.state in _LIVE
            and resource.id in local.consumed_resources(service_id)
            and (resource.shared or run_scope == scope)
        )

    def _begin_destroy_locked(
        self, system_id: str, resource: LocalResourceSpec, scope: str, check_dependents: bool = True
    ) -> tuple[threading.Thread | None, str]:
        """Starts tearing a resource down. Returns (the destroy thread, or None when
        there is nothing to run; what happened, for reports: "destroying", "absent",
        "forgotten", "expired")."""
        self._ensure_loaded_locked()
        key = (scope, system_id, resource.id)
        rec = self._resources.get(key)
        if rec is None or rec.state == ABSENT:
            return None, ABSENT
        if rec.state == CREATING:
            raise Busy(f"{system_id}/{resource.id} is still being created; destroy it once it is ready.")
        if rec.state == DESTROYING:
            return None, DESTROYING
        if self._expired_boundary_locked(resource, rec) is not None:
            # The nightly job already dropped it; running destroy would act on nothing
            # (or, with a reused name, on something else). Forget it.
            rec.generation += 1
            rec.state = ABSENT
            rec.outputs = {}
            rec.message = "Expired (dropped by the nightly job); forgotten without running destroy."
            self._save_locked()
            self._changed.notify_all()
            return None, EXPIRED
        if check_dependents:
            users = self._live_dependents_locked(system_id, resource, scope)
            if users:
                raise Busy(
                    f"{system_id}/{resource.id} is in use by {', '.join(users)}; stop "
                    "those services first."
                )
        if not resource.destroy or not rec.outputs:
            # Nothing PM Studio can run: forget it. A resource with outputs but no
            # destroy command is the operator's to clean up; the report names it.
            outcome = "forgotten" if rec.outputs else ABSENT
            rec.generation += 1
            rec.state = ABSENT
            rec.message = (
                "Forgotten (no destroy command declared): clean it up by hand - "
                + ", ".join(f"{k}={v}" for k, v in rec.outputs.items())
                if rec.outputs else ""
            )
            rec.outputs = {}
            self._save_locked()
            self._changed.notify_all()
            return None, outcome
        rec.generation += 1
        rec.state = DESTROYING
        rec.message = "Destroying."
        rec.lines = collections.deque(maxlen=LOG_LINES)
        rec.lines_lock = threading.Lock()
        self._changed.notify_all()
        thread = self._spawn_worker(
            self._destroy_resource, (key, system_id, resource, dict(rec.outputs), rec.generation),
            f"local-env-destroy-{scope}-{system_id}-{resource.id}",
        )
        return thread, DESTROYING

    def _destroy_resource(
        self,
        key: tuple[str, str, str],
        system_id: str,
        resource: LocalResourceSpec,
        outputs: dict[str, str],
        generation: int,
    ) -> None:
        values = {f"{resource.id}.{name}": value for name, value in outputs.items()}
        values["repo_root"] = self._repo_root_value
        if not resource.shared:
            values["session"] = key[0]
        try:
            command = render(resource.destroy, values, f"{system_id}/{resource.id} destroy")
        except LocalEnvError as exc:
            self._set_resource(key, generation, save=True, state=FAILED, message=f"Destroy not run: {exc}")
            return
        code, stdout, timed_out = self._run_resource_command(
            key, generation, command, resource.timeout, "destroy"
        )
        if code is None:
            return
        if timed_out or code != 0:
            why = f"did not finish within {resource.timeout:g}s" if timed_out else f"exited with code {code}"
            self._set_resource(
                key, generation, save=True, state=FAILED,
                message=f"destroy {why}; outputs kept - destroy again to retry.",
            )
            return
        self._set_resource(
            key, generation, save=True,
            state=ABSENT, outputs={}, created_at=None, message="Destroyed.",
        )

    def destroy_resource(
        self, system_id: str, resource_id: str, session: str = DEFAULT_SESSION_ID, wait: bool = False
    ) -> dict:
        """Destroys one resource in the caller's scope (its session's copy, or the
        shared one). Refused (409) while a running service uses it or while it is still
        being created. Returns its status at once ("destroying") unless `wait`."""
        ctx = self._session(session)
        with self._lock:
            resource = self._resource_spec(system_id, resource_id)
            scope = self._ctx_for(resource, ctx).id
            thread, _ = self._begin_destroy_locked(system_id, resource, scope)
        if thread is not None and wait:
            thread.join()
        with self._lock:
            return self._resource_status(system_id, resource, ctx)

    # ---- release (a session ends) ----

    def release_session(self, session_id: str, wait: bool = True) -> dict:
        """Everything a session's environment holds, given back: its services stopped
        (synchronously - the caller is about to remove the worktree they run from, and
        on Windows a running node holds files in it open), its resources destroyed when
        they declare `destroy` (in the background unless `wait`) or forgotten - and
        reported, with their outputs - when they do not, and its checkout preparation
        forgotten. Shared services and resources are untouched. Does not consult the
        session lookup: the session may already be on its way out."""
        report: dict = {
            "session": session_id, "stopped": [], "destroying": [], "destroyed": [],
            "forgotten": [], "failed": [], "aborted": [], "expired": [],
        }
        if session_id == SHARED:
            return report
        with self._lock:
            procs = []
            for key in [k for k in self._runs if k[0] == session_id]:
                if self._runs[key].state in _LIVE:
                    report["stopped"].append(f"{key[1]}/{key[2]}")
                procs.append(self._reset_locked(key))
        for proc in procs:
            if proc is not None:
                with contextlib.suppress(Exception):
                    _kill_tree(proc)

        threads: list[tuple[tuple[str, str, str], threading.Thread]] = []
        aborted: list[subprocess.Popen] = []
        with self._lock:
            self._ensure_loaded_locked()
            for key in [k for k in self._resources if k[0] == session_id]:
                rec = self._resources[key]
                name = f"{key[1]}/{key[2]}"
                local = self._systems.get(key[1])
                resource = local.resource(key[2]) if local is not None else None
                if rec.state == CREATING:
                    # Whatever it was half-way through making is unknown; say so.
                    rec.generation += 1
                    if rec.proc is not None:
                        aborted.append(rec.proc)
                        rec.proc = None
                    rec.state = ABSENT
                    rec.message = "Creation aborted: the session ended."
                    report["aborted"].append(name)
                    continue
                if resource is None or rec.state == ABSENT:
                    del self._resources[key]
                    continue
                try:
                    thread, outcome = self._begin_destroy_locked(
                        key[1], resource, session_id, check_dependents=False
                    )
                except Busy:
                    continue
                if thread is not None:
                    threads.append((key, thread))
                    report["destroying"].append(name)
                elif outcome == EXPIRED:
                    report["expired"].append(name)
                elif outcome == "forgotten":
                    report["forgotten"].append({"resource": name, "message": rec.message})
                elif outcome == DESTROYING:
                    report["destroying"].append(name)
            kept = []
            for entry in self._orphans:
                if str(entry.get("scope")) == session_id:
                    report["forgotten"].append({
                        "resource": f"{entry.get('system')}/{entry.get('resource')}",
                        "message": "No longer declared in config; clean it up by hand - "
                        + json.dumps(entry.get("outputs")),
                    })
                else:
                    kept.append(entry)
            self._orphans = kept
            for prep_key in [k for k in self._preps if k[0] == session_id]:
                del self._preps[prep_key]
            self._last_activity.pop(session_id, None)
            for size_key in [k for k in self._log_sizes if k[0] == session_id]:
                del self._log_sizes[size_key]
            self._save_locked()
            self._changed.notify_all()
        for proc in aborted:
            with contextlib.suppress(Exception):
                _kill_tree(proc)

        if wait:
            for _, thread in threads:
                thread.join()
            with self._lock:
                for key, _ in threads:
                    name = f"{key[1]}/{key[2]}"
                    rec = self._resources.get(key)
                    if rec is not None and rec.state == FAILED:
                        report["failed"].append({"resource": name, "message": rec.message})
                    else:
                        report["destroyed"].append(name)
                    report["destroying"].remove(name)
        return report

    # ---- background threads ----

    def _pump(
        self,
        proc: subprocess.Popen,
        lines: collections.deque,
        lines_lock: threading.Lock,
        log_path: Path,
        header: str,
    ) -> None:
        """Copies the merged stdout/stderr into the in-memory tail and the log file
        until the pipe closes (the whole tree has exited)."""
        handle = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path.open("a", encoding="utf-8", errors="replace")
            handle.write(header + "\n")
            handle.flush()
        except OSError:
            handle = None  # memory-only; the tail still works
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                with lines_lock:
                    lines.append(line)
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.write(line + "\n")
                        handle.flush()
        except (OSError, ValueError):
            pass  # pipe closed under us by a stop
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()

    def _watch(
        self,
        key: tuple[str, str, str],
        generation: int,
        proc: subprocess.Popen,
        service: LocalServiceSpec,
        port: int | None,
        ready_url: str,
    ) -> None:
        """starting -> running | failed, then running -> exited when it dies."""
        if not self._wait_ready(key, generation, proc, service, port, ready_url):
            return
        code = proc.wait()
        self._set(
            key, generation,
            state=EXITED, exit_code=code, pid=None,
            message=f"Exited with code {code}.",
        )

    def _wait_ready(
        self,
        key: tuple[str, str, str],
        generation: int,
        proc: subprocess.Popen,
        service: LocalServiceSpec,
        port: int | None,
        ready_url: str,
    ) -> bool:
        if port is None and not ready_url:
            try:
                code = proc.wait(timeout=NO_PROBE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return self._set(key, generation, state=RUNNING, message="")
            self._set(
                key, generation,
                state=FAILED, exit_code=code, pid=None,
                message=f"Exited with code {code} right after launch.",
            )
            return False

        deadline = time.monotonic() + service.ready_timeout
        while True:
            try:
                code = proc.wait(timeout=POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            else:
                self._set(
                    key, generation,
                    state=FAILED, exit_code=code, pid=None,
                    message=f"Exited with code {code} before it was ready.",
                )
                return False
            with self._lock:
                run = self._runs.get(key)
                if run is None or run.generation != generation:
                    return False  # stopped or relaunched
            ready = (port is None or _port_open(port)) and (
                not ready_url or _url_ready(ready_url)
            )
            if ready:
                return self._set(key, generation, state=RUNNING, message="")
            if time.monotonic() >= deadline:
                probe = " and ".join(
                    p for p in (f"port {port}" if port is not None else "", ready_url) if p
                )
                # Killed BEFORE the state flips, so "failed" never shows a live pid.
                # A stop() racing this kill bumps the generation, and the _set below
                # then leaves its "stopped" alone.
                _kill_tree(proc)
                self._set(
                    key, generation,
                    state=FAILED, pid=None, exit_code=proc.poll(),
                    message=f"Not ready within {service.ready_timeout:g}s ({probe}); stopped it.",
                )
                return False

    # ---- stop ----

    def stop(self, system_id: str, service_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        """Stops one service and its whole process tree. Its dependencies keep running:
        they may be serving something else, and starting them again is the slow part.
        Stopping a shared service stops it for every session."""
        ctx = self._session(session)
        with self._lock:
            _, service = self._service(system_id, service_id)
            proc = self._reset_locked(self._key(system_id, service, ctx))
        if proc is not None:
            _kill_tree(proc)
        with self._lock:
            return self._service_status(system_id, service, ctx)

    def stop_all(self, system_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        """Stops the session's services of a system, dependents before what they depend
        on. Shared services go too, but only when no OTHER session still has a service
        of this system up - they may be what that session is running against."""
        ctx = self._session(session)
        with self._lock:
            local = self._local(system_id)
            others = {
                scope for (scope, run_system, _), run in self._runs.items()
                if run_system == system_id and run.state in _LIVE and scope not in (ctx.id, SHARED)
            }
            order = [
                s for s in reversed(_dependency_order(local)) if not (s.shared and others)
            ]
        for service in order:
            with self._lock:
                proc = self._reset_locked(self._key(system_id, service, ctx))
            if proc is not None:
                _kill_tree(proc)
        with self._lock:
            return self._system_status(system_id, ctx)

    def restart(
        self, system_id: str, service_id: str, params: dict | None = None,
        session: str = DEFAULT_SESSION_ID,
    ) -> dict:
        """Stop, then start with `params` - or, when none are given, with the params it
        last ran with, so a restart repeats the launch rather than resetting it."""
        ctx = self._session(session)
        with self._lock:
            local, service = self._service(system_id, service_id)
            previous = dict(self._run(self._key(system_id, service, ctx)).params)
            effective = self.effective_params(
                service, params if params is not None else (previous or None)
            )
        self.stop(system_id, service_id, session=ctx.id)
        needs_env = self._needs_session_env(local, [service.id])
        if needs_env:
            self._make_room(ctx, system_id)
        with self._lock:
            if needs_env:
                self._check_limit_locked(ctx, system_id)
            self._start_locked(system_id, local, service, effective, ctx)
            return self._service_status(system_id, service, ctx)

    def _reset_locked(self, key: tuple[str, str, str]) -> subprocess.Popen | None:
        """Marks a service stopped and returns its process, if it is still alive, for
        the caller to kill outside the lock."""
        run = self._run(key)
        proc = run.proc
        run.generation += 1
        run.state = STOPPED
        run.proc = None
        run.pid = None
        run.exit_code = None
        run.port = None
        run.resource_snapshot = None
        run.message = ""
        # A setup still running is in `proc` too (see _run_setup): returned and killed
        # like the service itself.
        run.in_setup = False
        self._changed.notify_all()
        if proc is not None and proc.poll() is None:
            return proc
        return None

    # ---- idle stop ----

    def _stop_session_runs_locked(
        self, scope: str, message: str
    ) -> tuple[list[str], list[subprocess.Popen | None]]:
        """Marks every live session-scoped run of `scope`, in every system, stopped with
        `message` as the reason, and returns ("system/service" names, their processes
        for the caller to kill OUTSIDE the lock, dependents before what they depend on).
        Resources and the prepared checkout are deliberately left alone: what is being
        reclaimed is memory and CPU, and those are what make the next start cheap."""
        stopped: list[str] = []
        procs: list[subprocess.Popen | None] = []
        for system_id, local in self._systems.items():
            for service in reversed(_dependency_order(local)):
                key = (scope, system_id, service.id)
                run = self._runs.get(key)
                if run is None or run.state not in _LIVE:
                    continue
                stopped.append(f"{system_id}/{service.id}")
                procs.append(self._reset_locked(key))
                # After the reset, which clears the message: the generation it bumped
                # keeps the run's watch thread from overwriting this one when the
                # process dies.
                run.message = message
        return stopped, procs

    @staticmethod
    def _kill_all(procs: list[subprocess.Popen | None]) -> None:
        for proc in procs:
            if proc is not None:
                with contextlib.suppress(Exception):
                    _kill_tree(proc)

    def _log_stop(self, scope: str, stopped: list[str], why: str) -> None:
        if stopped:
            with self._lock:
                name = self._session_names.get(scope, scope)
            print(
                f"[local_env] stopped session {name} ({scope})'s environment {why}: "
                f"{', '.join(stopped)}",
                file=sys.stderr,
            )

    def stop_session(self, session_id: str, message: str = "") -> list[str]:
        """Stops every service a session's environment is running, in every system -
        the session's own, never the shared ones - and keeps everything else: its
        resources and prepared checkout stay for the next start (release_session is
        what gives those back, once the session ends). `message` is the reason each
        stopped service then shows. Returns the "system/service" names it stopped."""
        if session_id == SHARED:
            return []
        with self._lock:
            stopped, procs = self._stop_session_runs_locked(session_id, message)
        self._kill_all(procs)
        self._log_stop(session_id, stopped, "on request")
        return stopped

    def reap_idle(self, now: float | None = None) -> list[dict]:
        """One idle-stop pass (see `[local_env] idle_stop_minutes`): stops the
        session-scoped services of every session that has made no local-env call for
        that long and is not busy. A busy session's activity is refreshed instead, so
        the countdown starts when its dev task ends, not when it began. Returns what it
        stopped, one entry per session. A no-op when idle stop is off.

        The reaper thread calls this periodically; tests call it directly with `now` in
        the injected clock's terms."""
        if self._idle_stop_minutes <= 0:
            return []
        now = self._clock() if now is None else now
        threshold = self._idle_stop_minutes * 60
        with self._lock:
            scopes = sorted({scope for scope, _ in self._active_envs_locked()})
        report: list[dict] = []
        for scope in scopes:
            if self._busy(scope):
                with self._lock:
                    self._touch_locked(scope, now)
                continue
            with self._lock:
                self._note_output_locked(scope, now)
                # setdefault: a live run with no recorded activity (it cannot normally
                # happen - starting one is activity) starts its countdown now rather
                # than being stopped on sight.
                idle = now - self._last_activity.setdefault(scope, now)
                if idle < threshold:
                    continue
                message = (
                    f"Stopped after {self._idle_stop_minutes} min without use; start it "
                    "again when needed."
                )
                stopped, procs = self._stop_session_runs_locked(scope, message)
            self._kill_all(procs)
            self._log_stop(scope, stopped, f"after {int(idle // 60)} min without use")
            if stopped:
                report.append({"session": scope, "stopped": stopped, "idle_minutes": int(idle // 60)})
        return report

    def start_idle_reaper(self, interval: float = REAP_INTERVAL_SECONDS) -> bool:
        """Starts the daemon thread that calls reap_idle() every `interval` seconds
        until shutdown(). Explicit (the server calls it from its lifespan hook) rather
        than at construction, so building a manager - or importing server.py - starts
        nothing. Returns whether a thread is running; with idle stop off there is no
        thread at all."""
        if self._idle_stop_minutes <= 0:
            return False
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return True
            stop = self._reaper_stop = threading.Event()

            def loop() -> None:
                while not stop.wait(interval):
                    try:
                        self.reap_idle()
                    except Exception as exc:  # one bad pass must not end idle stops
                        print(f"[local_env] idle-stop pass failed: {exc}", file=sys.stderr)

            self._reaper = threading.Thread(target=loop, name="local-env-idle-reaper", daemon=True)
            self._reaper.start()
            return True

    def shutdown(self) -> None:
        """Stops every process this manager started. Called from the server's shutdown
        hook and, as a backstop, at interpreter exit; safe to call more than once.

        Resources are NOT destroyed: they persist for reuse after the restart. A create
        or destroy still running is killed - a create's half-made resource is left
        unrecorded (absent), a destroy's resource keeps its outputs (failed) so it can
        be destroyed again."""
        self._reaper_stop.set()
        with self._lock:
            procs = [self._reset_locked(key) for key in list(self._runs)]
            for rec in self._resources.values():
                if rec.proc is None:
                    continue
                procs.append(rec.proc)
                rec.proc = None
                rec.generation += 1
                if rec.state == CREATING:
                    rec.state = ABSENT
                    rec.message = "Creation interrupted by shutdown."
                elif rec.state == DESTROYING:
                    rec.state = FAILED
                    rec.message = "Destroy interrupted by shutdown; destroy again to retry."
            if self._loaded:
                self._save_locked()
            workers = list(self._workers)
        for proc in procs:
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(Exception):
                    _kill_tree(proc)
        # Their commands are dead; give the threads a moment to notice and close their
        # log files (bounded - a thread stuck preparing a checkout is not waited out).
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        for thread in workers:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    # ---- reads ----

    def _resolved(self, template: str, values_fn, trusted) -> str:
        """A url for status: as launched when known, else rendered from what is known
        now, else "" - never a template with braces the UI would turn into a link."""
        if not template or not LOCAL_PLACEHOLDER.search(template):
            return template
        try:
            return render(template, values_fn(), "", trusted)
        except LocalEnvError:
            return ""

    def _service_status(self, system_id: str, service: LocalServiceSpec, ctx: SessionContext) -> dict:
        local = self._systems[system_id]
        key = self._key(system_id, service, ctx)
        run = self._runs.get(key) or _Run()
        port = run.port if run.port is not None else service.fixed_port
        launched = run.state in _LIVE and run.proc is not None and not run.in_setup

        def values() -> dict[str, str]:
            return self._values_locked(system_id, local, service, ctx, run)

        return {
            "id": service.id,
            "label": service.label or service.id,
            "scope": service.scope,
            "session": key[0],
            "state": run.state,
            "pid": run.pid,
            "port": port,
            "auto_port": service.auto_port,
            "url": run.url if launched else self._resolved(service.url, values, set(run.params)),
            "ready_url": (
                run.ready_url if launched
                else self._resolved(service.ready_url, values, set(run.params))
            ),
            "command": service.command,
            "setup": service.setup,
            "depends_on": list(service.depends_on),
            "uses": list(service.uses),
            "resources_used": local.consumed_resources(service.id),
            "restart_needed": bool(
                run.state in _LIVE
                and run.resource_snapshot is not None
                and run.resource_snapshot != self._resource_snapshot_locked(system_id, local, service, ctx)
            ),
            "params": {**service.param_defaults, **run.params},
            "started_at": run.started_at,
            "exit_code": run.exit_code,
            "message": run.message,
            "log_path": str(self.log_path(system_id, service.id, key[0])),
        }

    def _resource_status(self, system_id: str, resource: LocalResourceSpec, ctx: SessionContext) -> dict:
        self._ensure_loaded_locked()
        key = self._key(system_id, resource, ctx)
        rec = self._resources.get(key) or _Resource()
        state, outputs, message = self._effective_locked(resource, rec)
        expires_at_iso = None
        if state == READY and resource.expires:
            expires_at_iso = next_expiry_boundary(
                self._clock(), resource.expires_at, resource.timezone
            ).isoformat()
        return {
            "id": resource.id,
            "label": resource.label or resource.id,
            "scope": resource.scope,
            "session": key[0],
            "state": state,
            "outputs": outputs,
            "message": message,
            "created_at": rec.created_at,
            "expires": resource.expires,
            "expires_at_iso": expires_at_iso,
            "has_destroy": bool(resource.destroy),
            "log_path": str(self.log_path(system_id, resource.id, key[0])),
        }

    def _system_status(self, system_id: str, ctx: SessionContext) -> dict:
        local = self._systems[system_id]
        return {
            "id": system_id,
            "label": self._labels.get(system_id, system_id),
            "session": ctx.id,
            "aws_profile": expand_env(local.aws_profile),
            "test_users_secret": local.test_users_secret,
            "checkout": self._prep_status_locked(ctx, system_id),
            "services": [self._service_status(system_id, s, ctx) for s in local.services],
            "resources": [self._resource_status(system_id, r, ctx) for r in local.resources],
        }

    def status(self, session: str = DEFAULT_SESSION_ID) -> list[dict]:
        """Every system that declares a local environment, as the given session sees it:
        its own copy of each session-scoped service and resource, the shared ones."""
        ctx = self._session(session)
        with self._lock:
            return [self._system_status(system_id, ctx) for system_id in self._systems]

    def system_status(self, system_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        ctx = self._session(session)
        with self._lock:
            self._local(system_id)
            return self._system_status(system_id, ctx)

    def service_status(self, system_id: str, service_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        ctx = self._session(session)
        with self._lock:
            _, service = self._service(system_id, service_id)
            return self._service_status(system_id, service, ctx)

    def wait(
        self, system_id: str, service_id: str, session: str = DEFAULT_SESSION_ID,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> dict:
        """Blocks until the service is no longer "starting" - running, failed, exited,
        port_in_use, stopped - or `timeout` seconds (capped at MAX_WAIT_SECONDS) pass,
        then returns its status plus "waited_seconds" and "timed_out". The alternative
        an agent otherwise reaches for is a blind `sleep 60` after a start: too long
        when the service is quick, too short when it is not, and silent about a
        failure. A service that is not starting answers at once.

        Each wake-up also counts as the session's activity: a caller blocked here for
        minutes is using its environment, and must not see it idle-stopped mid-wait."""
        ctx = self._session(session)
        try:
            limit = float(timeout)
        except (TypeError, ValueError):
            raise InvalidParams(f"timeout must be a number of seconds, got {timeout!r}.") from None
        if limit != limit:  # NaN: min/max below would pass it through
            raise InvalidParams("timeout must be a number of seconds.")
        limit = max(0.0, min(limit, MAX_WAIT_SECONDS))
        started = time.monotonic()
        deadline = started + limit
        with self._changed:
            _, service = self._service(system_id, service_id)
            while True:
                status = self._service_status(system_id, service, ctx)
                remaining = deadline - time.monotonic()
                if status["state"] != STARTING or remaining <= 0:
                    break
                # Notified on every state change; the cap is only a safety net for a
                # missed notification.
                self._changed.wait(timeout=min(remaining, WAIT_WAKE_SECONDS))
                self._touch_locked(ctx.id)
        status["waited_seconds"] = round(time.monotonic() - started, 1)
        status["timed_out"] = status["state"] == STARTING
        return status

    def resource_status(self, system_id: str, resource_id: str, session: str = DEFAULT_SESSION_ID) -> dict:
        ctx = self._session(session)
        with self._lock:
            resource = self._resource_spec(system_id, resource_id)
            return self._resource_status(system_id, resource, ctx)

    def overview(self) -> dict:
        """Every active run and every held resource across ALL sessions - what is
        consuming the machine, and who to ask to stop it. Session names come from the
        lookup when it still knows the session, else from the last name seen."""
        with self._lock:
            self._ensure_loaded_locked()
            runs = [
                {
                    "session": scope,
                    "system": system_id,
                    "system_label": self._labels.get(system_id, system_id),
                    "service": service_id,
                    "scope": "shared" if scope == SHARED else "session",
                    "state": run.state,
                    "port": run.port,
                    "pid": run.pid,
                    "url": run.url,
                }
                for (scope, system_id, service_id), run in sorted(self._runs.items())
                if run.state in _LIVE
            ]
            resources = []
            for (scope, system_id, resource_id), rec in sorted(self._resources.items()):
                local = self._systems.get(system_id)
                state, outputs, message = self._effective_locked(
                    local.resource(resource_id) if local is not None else None, rec
                )
                if state in (ABSENT, EXPIRED):
                    continue  # nothing held any more
                resources.append({
                    "session": scope,
                    "system": system_id,
                    "resource": resource_id,
                    "scope": "shared" if scope == SHARED else "session",
                    "state": state,
                    "outputs": outputs,
                    "message": message,
                })
            active = sorted(self._active_envs_locked())
            known = dict(self._session_names)
        names: dict[str, str] = {SHARED: SHARED}
        for scope in {r["session"] for r in runs + resources} | {s for s, _ in active}:
            if scope in names:
                continue
            ctx = None
            with contextlib.suppress(Exception):
                ctx = self._session_lookup(scope)
            names[scope] = ctx.name if ctx is not None else known.get(scope, scope)
        for item in runs + resources:
            item["session_name"] = names.get(item["session"], item["session"])
        return {
            "runs": runs,
            "resources": resources,
            "session_envs": [
                {"session": scope, "session_name": names.get(scope, scope), "system": system_id}
                for scope, system_id in active
            ],
            "active_session_envs": len(active),
            "max_session_envs": self._max_session_envs,
        }

    def logs(
        self, system_id: str, item_id: str, lines: int = DEFAULT_LOG_TAIL,
        session: str = DEFAULT_SESSION_ID,
    ) -> list[str]:
        """The last `lines` lines of a service's current (or most recent) run - or of a
        resource's most recent create/destroy, which share the same mechanism."""
        ctx = self._session(session)
        with self._lock:
            local = self._local(system_id)
            if local.resource(item_id) is not None:
                self._ensure_loaded_locked()
                holder = self._resources.get(self._key(system_id, local.resource(item_id), ctx))
            else:
                _, service = self._service(system_id, item_id)
                holder = self._runs.get(self._key(system_id, service, ctx))
            if holder is None:
                return []
            # Both taken under the manager lock: a relaunch swaps them as a pair.
            buffer, buffer_lock = holder.lines, holder.lines_lock
        lines = max(1, min(int(lines), LOG_LINES))
        with buffer_lock:
            tail = list(buffer)
        return tail[-lines:]
