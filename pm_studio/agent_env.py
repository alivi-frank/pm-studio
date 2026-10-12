"""The environment every `claude` subprocess (PM turn, dev task, judge) runs with.

Opt-in. Without PM_AGENT_HOME the agents inherit the server's environment exactly as
before. With it set (in the repo's .env or the shell), agents are cut off from the
operator's own machine setup:

  - Claude Code: CLAUDE_CONFIG_DIR=<home>/claude, so their settings, MCP servers,
    CLAUDE.md, plugins, memory and login are their own - none of the operator's
    ~/.claude or ~/.claude.json. Log in once: `python -m pm_studio.agent_env --login`.
  - Credentials: every variable the repo's .env supplied is dropped, and so is any
    variable whose name looks like a credential (TOKEN, SECRET, PASSWORD, _PAT, ...),
    unless its name matches PM_AGENT_ENV_ALLOW (default: EPICRIDE_SECRET_*,AWS_REGION).
  - AWS: only the profiles listed in PM_AGENT_AWS_PROFILES, through a generated
    AWS_CONFIG_FILE holding just those profiles and the sso-sessions/source profiles
    they reference; the first one becomes AWS_PROFILE. Empty means no AWS profile at all.

`python -m pm_studio.agent_env` prints what agents will and will not get (names only);
`--ensure-login` (run by the launchers) signs the agents in through the browser when needed.
"""

import collections
import configparser
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .accounts import AGENT_HEADER_NAME, AGENT_TOKEN
from .config import CONFIG, ENV_FILE_NAME, _parse_env_file

DEFAULT_ALLOW = "EPICRIDE_SECRET_*,AWS_REGION"
CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|_PAT$|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL", re.IGNORECASE
)
# Static AWS credentials and profile selection the operator's shell may carry; agents
# get AWS only through the generated config below.
AWS_OVERRIDES = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
)


def _home() -> Path | None:
    raw = os.environ.get("PM_AGENT_HOME", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else CONFIG.repo_root / path).resolve()


def _dotenv_keys() -> set[str]:
    try:
        text = (CONFIG.repo_root / ENV_FILE_NAME).read_text(encoding="utf-8")
    except OSError:
        return set()
    pairs, _ = _parse_env_file(text)
    return {key for key, _ in pairs}


def _write_aws_config(target_dir: Path, profiles: list[str]) -> list[str]:
    """Copies just `profiles` (plus the sso-sessions and source profiles they need) out
    of the operator's AWS config. Returns the requested profiles that were not found."""
    source = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    parser = configparser.RawConfigParser()
    parser.read(source, encoding="utf-8")

    def section(profile: str) -> str:
        return "default" if profile == "default" else f"profile {profile}"

    out = configparser.RawConfigParser()
    missing: list[str] = []
    pending = list(profiles)
    while pending:
        profile = pending.pop()
        name = section(profile)
        if out.has_section(name):
            continue
        if not parser.has_section(name):
            missing.append(profile)
            continue
        out[name] = dict(parser[name])
        if parser[name].get("source_profile"):
            pending.append(parser[name]["source_profile"])
        sso = parser[name].get("sso_session")
        if sso and parser.has_section(f"sso-session {sso}"):
            out[f"sso-session {sso}"] = dict(parser[f"sso-session {sso}"])

    target_dir.mkdir(parents=True, exist_ok=True)
    with (target_dir / "config").open("w", encoding="utf-8") as handle:
        out.write(handle)
    # Empty on purpose: no static keys, so only the SSO profiles above can authenticate.
    (target_dir / "credentials").write_text("", encoding="utf-8")
    return [p for p in profiles if p in missing]


@functools.cache
def _build() -> tuple[dict[str, str] | None, dict]:
    home = _home()
    if home is None:
        return None, {}

    allow = [p.strip() for p in os.environ.get("PM_AGENT_ENV_ALLOW", DEFAULT_ALLOW).split(",") if p.strip()]
    dotenv = _dotenv_keys()
    env: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in os.environ.items():
        allowed = any(fnmatch.fnmatchcase(key.upper(), p.upper()) for p in allow)
        blocked = (
            key.startswith("PM_AGENT_")
            or key in AWS_OVERRIDES
            or (not allowed and (key in dotenv or CREDENTIAL_NAME.search(key)))
        )
        if blocked:
            dropped.append(key)
        else:
            env[key] = value

    claude_dir = home / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

    profiles = [p.strip() for p in os.environ.get("PM_AGENT_AWS_PROFILES", "").split(",") if p.strip()]
    aws_dir = home / "aws"
    missing = _write_aws_config(aws_dir, profiles)
    env["AWS_CONFIG_FILE"] = str(aws_dir / "config")
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(aws_dir / "credentials")
    if profiles:
        env["AWS_PROFILE"] = profiles[0]

    summary = {
        "home": home,
        "claude_dir": claude_dir,
        "aws_profiles": profiles,
        "aws_missing": missing,
        "dropped": sorted(dropped),
        "allow": allow,
    }
    return env, summary


def agent_env() -> dict[str, str] | None:
    """The `env=` for a claude subprocess: None (inherit) when isolation is off."""
    env, summary = _build()
    if env is not None and not getattr(agent_env, "_announced", False):
        agent_env._announced = True  # type: ignore[attr-defined]
        aws = ", ".join(summary["aws_profiles"]) or "none"
        print(f"[pm_studio] agents isolated: Claude config {summary['claude_dir']}, AWS profiles: {aws}")
        if summary["aws_missing"]:
            print(f"[pm_studio] WARNING: PM_AGENT_AWS_PROFILES not found in AWS config: {', '.join(summary['aws_missing'])}", file=sys.stderr)
    return env


def subprocess_env(session_id: str | None = None) -> dict[str, str]:
    """The `env=` actually passed to every claude subprocess: agent_env() (or, with
    isolation off, this process's own environment) plus how to reach this server.

      PM_STUDIO_PYTHON        the Python interpreter this server runs on, for the repo's
                              own scripts (`"$PM_STUDIO_PYTHON" scripts/<x>.py`)
      PM_STUDIO_URL           the server's base URL, e.g. http://127.0.0.1:8000
      PM_STUDIO_AGENT_HEADER  "<header name>: <agent token>", ready for
                              `curl -H "$PM_STUDIO_AGENT_HEADER" "$PM_STUDIO_URL/local-env"`
      PM_STUDIO_SESSION       the chat session the agent works for (PM turns and dev
                              tasks pass it; judges do not), so its local-env calls
                              can say `?session=$PM_STUDIO_SESSION` and reach its OWN
                              session's environment - the one running its branch

    The PM agent already gets the header spliced into its prompt's curl examples; a dev
    agent gets no such prompt, and these two variables are how it can still call the
    server - to bring its system's local environment up (see local_env.py) before
    verifying a change against it. Added AFTER the credential filter on purpose: the
    token is minted per server process (accounts.AGENT_TOKEN), is meaningless anywhere
    else, and is the one credential an agent is meant to hold. The header is set in
    personal mode too, where the server ignores it, so an agent's call reads the same
    in both modes.
    """
    base = agent_env()
    env = dict(os.environ if base is None else base)
    # The interpreter this server runs on, for the repo's own Python scripts: the bare
    # `python` an agent's shell finds can be anything (an MSYS2 3.10 without tomllib, a
    # Store alias), while this one is known to be the deployment's supported version.
    env["PM_STUDIO_PYTHON"] = sys.executable
    env["PM_STUDIO_URL"] = CONFIG.base_url
    env["PM_STUDIO_AGENT_HEADER"] = f"{AGENT_HEADER_NAME}: {AGENT_TOKEN}"
    if session_id:
        env["PM_STUDIO_SESSION"] = session_id
    return env


def auth_status(env: dict[str, str] | None = None) -> dict:
    """`claude auth status --json` for the agents' Claude config: {"loggedIn": bool, ...}.
    {"loggedIn": False, "error": ...} when the CLI is missing or answers nonsense."""
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"], env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
    except FileNotFoundError:
        return {"loggedIn": False, "error": "the `claude` CLI is not on PATH"}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"loggedIn": False, "error": str(exc)}
    return data if isinstance(data, dict) else {"loggedIn": False, "error": "unexpected output"}


AUTH_CACHE_SECONDS = 60
_auth_lock = threading.Lock()
_auth_cache: tuple[float, dict] | None = None
_login_proc: subprocess.Popen | None = None
_login_lines: collections.deque = collections.deque(maxlen=20)


def _track_login_output(proc: subprocess.Popen) -> None:
    """Keeps the sign-in's last output lines (the URL to open, if the browser did not)
    and drops the cached status when it ends, so the next read is fresh."""
    global _auth_cache
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.strip():
                _login_lines.append(line.rstrip())
    except (OSError, ValueError):
        pass
    proc.wait()
    with _auth_lock:
        _auth_cache = None


def agent_auth(refresh: bool = False) -> dict:
    """The agents' Claude sign-in, for the UI (GET /agents/auth): whose config it is,
    whether it is signed in and as whom, and whether a sign-in is under way. Cached for
    AUTH_CACHE_SECONDS - it runs the `claude` CLI - unless `refresh`."""
    global _auth_cache
    env = agent_env()
    with _auth_lock:
        login_running = _login_proc is not None and _login_proc.poll() is None
        cached = _auth_cache
    if cached is None or refresh or login_running or time.time() - cached[0] > AUTH_CACHE_SECONDS:
        status = auth_status(env)
        with _auth_lock:
            _auth_cache = (time.time(), status)
    else:
        status = cached[1]
    return {
        "isolated": env is not None,
        "config_dir": env.get("CLAUDE_CONFIG_DIR") if env else None,
        "logged_in": bool(status.get("loggedIn")),
        "email": status.get("email"),
        "auth_method": status.get("authMethod"),
        "subscription": status.get("subscriptionType"),
        "error": status.get("error"),
        "login_running": login_running,
        "login_output": list(_login_lines) if login_running else [],
    }


def start_login() -> dict:
    """Starts `claude auth login` for the agents' config in the background: it opens the
    browser on this machine (PM Studio runs on the developer's own) and finishes by
    itself. One at a time; returns agent_auth()."""
    global _login_proc
    env = agent_env()
    with _auth_lock:
        if _login_proc is None or _login_proc.poll() is not None:
            _login_lines.clear()
            try:
                _login_proc = subprocess.Popen(
                    ["claude", "auth", "login"], env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                _login_lines.append("the `claude` CLI is not on PATH")
                _login_proc = None
            else:
                threading.Thread(target=_track_login_output, args=(_login_proc,), daemon=True).start()
    return agent_auth()


def ensure_login(env: dict[str, str] | None) -> int:
    """For the server's launcher: the agents' Claude config logged in, or a browser
    sign-in to fix it (`claude auth login` - no REPL to quit). Never blocks the server
    from starting: a failed sign-in is reported and agents fail later with the reason."""
    status = auth_status(env)
    if status.get("loggedIn"):
        who = status.get("email") or status.get("authMethod") or "an account"
        print(f"[pm_studio] agents are signed in to Claude ({who}).")
        return 0
    if status.get("error"):
        print(f"[pm_studio] WARNING: cannot check the agents' Claude sign-in: {status['error']}", file=sys.stderr)
        return 1
    print("[pm_studio] The agents' Claude config is not signed in - opening the browser to sign in...")
    login = subprocess.call(["claude", "auth", "login"], env=env)
    if login == 0 and auth_status(env).get("loggedIn"):
        print("[pm_studio] agents signed in.")
        return 0
    print("[pm_studio] WARNING: agents are still not signed in; PM chats and dev tasks will fail "
          "until they are (make agent-login).", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    env, summary = _build()
    if "--ensure-login" in argv:
        # Isolation off: agents use the operator's own Claude login, nothing to check here.
        return 0 if env is None else ensure_login(env)
    if env is None:
        print("Agent isolation is OFF (PM_AGENT_HOME not set): agents inherit this environment as-is.")
        return 0
    if "--claude-dir" in argv:
        print(summary["claude_dir"])
        return 0
    if "--login" in argv:
        print(f"Starting Claude Code with CLAUDE_CONFIG_DIR={summary['claude_dir']} - run /login, then /exit.")
        return subprocess.call(["claude"], env=env)
    print(f"PM_AGENT_HOME       {summary['home']}")
    print(f"Claude config dir   {summary['claude_dir']}  (logged in: {'yes' if any(summary['claude_dir'].glob('.credentials*')) else 'unknown/no'})")
    print(f"AWS profiles        {', '.join(summary['aws_profiles']) or 'none'}")
    if summary["aws_missing"]:
        print(f"  NOT FOUND         {', '.join(summary['aws_missing'])}")
    print(f"Env allow patterns  {', '.join(summary['allow'])}")
    print(f"Withheld from agents ({len(summary['dropped'])}): {', '.join(summary['dropped'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
