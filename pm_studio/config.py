"""Per-deployment configuration: everything about the host project that PM Studio
must not hardcode, loaded from `pm_studio_local/` in the target repo.

The split is deliberate and is the package's core contract:
- The PACKAGE ships the behavior - prompts, lifecycle, endpoints, UI - identical for
  every deployment, and is never edited locally (it is maintained upstream only).
- The TARGET REPO ships `pm_studio_local/`: project identity + append-only local
  instructions (enterprise rules, private domain knowledge). Local content can ADD to
  the shipped prompts but can never replace or reorder them - that's what keeps the
  experience the same across every system using PM Studio.

Layout in the target repo (all optional - missing pieces fall back to defaults):

    pm_studio_local/
      config.toml           # [project], [server], [products], [models], [enterprise],
                            #   [smtp], [costing], [[trackers]]
      PM_INSTRUCTIONS.md    # appended to every PM system prompt
      DEV_INSTRUCTIONS.md   # appended to every dev-agent dispatch
      knowledge/*.md        # local reference docs; the PM is pointed at them

The repo root is the process working directory (`python -m pm_studio` is run from the
target repo's root), NOT this package's install location.
"""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_DIR_NAME = "pm_studio_local"
CONFIG_FILE_NAME = "config.toml"
PM_INSTRUCTIONS_NAME = "PM_INSTRUCTIONS.md"
DEV_INSTRUCTIONS_NAME = "DEV_INSTRUCTIONS.md"
KNOWLEDGE_DIR_NAME = "knowledge"

DEFAULT_WORKSPACE_ROOT = "pm_studio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SESSION_NAME = "Main"

# The two operating modes. `personal` is the historical single-trusted-user tool and
# stays the default forever: an existing deployment that upgrades the package must not
# suddenly demand a login. `enterprise` turns on accounts, invites and roles.
MODE_PERSONAL = "personal"
MODE_ENTERPRISE = "enterprise"
MODES = (MODE_PERSONAL, MODE_ENTERPRISE)

DEFAULT_SMTP_PORT = 587

# How often a configured tracker is re-pulled in the background. Minutes, not seconds:
# ticket types change rarely, and a tight loop against someone's Jira is how a deployment
# earns a rate limit. A stakeholder who needs it sooner has "Sync now" on the board.
DEFAULT_SYNC_INTERVAL_MINUTES = 15.0
MIN_SYNC_INTERVAL_MINUTES = 1.0

# Cost attribution defaults. A deployment sets its own in [costing]; rates are that
# deployment's own compensation data and never ship in this package.
DEFAULT_CAPACITY_HOURS = 40.0
DEFAULT_CURRENCY = "USD"

# Shown in the PM system prompt when the target repo hasn't described its own source
# layout yet - honest about not knowing, and points at the fix.
DEFAULT_REPO_LAYOUT = (
    "- (This project has not described its source layout in "
    f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} yet - explore the repo root to learn where "
    "sources live, and suggest the stakeholder fill in the `layout` setting so future "
    "sessions start oriented.)"
)


@dataclass(frozen=True)
class SmtpConfig:
    """Outbound mail for enterprise invites. Entirely optional: with no [smtp] table
    PM Studio never tries to send anything and the invite flow falls back to a
    copyable link, so no deployment is forced to stand up a mail server."""

    host: str
    port: int
    from_address: str
    username: str = ""
    password: str = ""
    use_tls: bool = True

    @property
    def is_usable(self) -> bool:
        return bool(self.host and self.from_address)


@dataclass(frozen=True)
class CostingConfig:
    """Cost-attribution inputs. Optional: with no [costing] table hours are still
    distributed and reported, and only the money columns are unknown."""

    # Fallback hourly rate for anyone with no individual rate. Supporting this is the
    # point of "blended": an org that will not put individual salaries in a tool can
    # still get project cost out.
    blended_rate: float | None = None
    default_capacity_hours: float = DEFAULT_CAPACITY_HOURS
    currency: str = DEFAULT_CURRENCY
    # Relative weight per signal kind, for tuning the split.
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackerConfig:
    """One external issue tracker PM Studio pulls ticket metadata from.

    A deployment declares as many as it has, each as its own `[[trackers]]` block, so a
    shop running both a Jira instance and an ADO project can link roadmap changes into
    either. `projects` is the set of boards/projects to pull from - it bounds the sync
    (nobody wants every issue in the instance) and it is also what lets a bare key like
    `PROJ-123` be resolved to the tracker that owns `PROJ`.

    The token is NEVER stored here from a file by preference: see _parse_trackers, which
    reads `token_env` (the NAME of an environment variable) and only falls back to an
    inline `token` with a loud warning. pm_studio_local/ is normally committed.
    """

    id: str
    provider: str  # "jira" | "ado"
    label: str
    # Jira: the instance root, e.g. https://acme.atlassian.net
    # ADO:  derived from `organization` unless given explicitly.
    base_url: str
    projects: tuple[str, ...]
    # Jira Cloud authenticates as email + API token; ADO as an empty user + PAT.
    username: str = ""
    token: str = ""
    # ADO only: the dev.azure.com organization name.
    organization: str = ""
    # How often the background sync re-pulls this tracker. Per tracker because a busy
    # Jira and a quiet ADO project don't deserve the same polling rate.
    sync_interval_minutes: float = DEFAULT_SYNC_INTERVAL_MINUTES

    @property
    def is_usable(self) -> bool:
        """A tracker with no credential or no projects is declared but cannot sync. It is
        still listed in the UI, with this as the reason - silently skipping it would look
        like the sync was working."""
        return bool(self.base_url and self.token and self.projects)

    @property
    def unusable_reason(self) -> str:
        if not self.base_url:
            return "no base_url (or organization, for ADO)"
        if not self.token:
            return "no API token - set `token_env` to the name of an environment variable"
        if not self.projects:
            return "no `projects` listed, so there is no board to pull from"
        return ""


@dataclass(frozen=True)
class Config:
    repo_root: Path
    project_name: str
    default_session_name: str
    # Directory under repo_root that holds `workspace/` runtime state. Default
    # "pm_studio"; a system migrated from a locally-built pm_agent keeps
    # "pm_agent" here so live worktrees/registered paths stay valid in place.
    workspace_root: str
    host: str
    port: int
    products: dict[str, str] = field(default_factory=dict)
    # Markdown-ish lines describing where product sources live at the repo root -
    # injected verbatim into the PM system prompt's layout section.
    repo_layout: str = DEFAULT_REPO_LAYOUT
    # Optional [models] override; empty means "use the package defaults" (models.py).
    models: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None
    # Append-only local prompt fragments ("" when the file is absent).
    pm_instructions: str = ""
    dev_instructions: str = ""
    # Repo-root-relative paths of local knowledge docs the PM should know exist.
    knowledge_files: tuple[str, ...] = ()
    # "personal" (default) or "enterprise" - see MODES. Personal mode keeps the
    # historical no-accounts behavior byte for byte.
    mode: str = MODE_PERSONAL
    # None unless the deployment configured [smtp].
    smtp: SmtpConfig | None = None
    costing: CostingConfig = field(default_factory=CostingConfig)
    # Empty unless the deployment declared [[trackers]]; the whole Jira/ADO feature is
    # dormant in that case and the board looks exactly as it did before.
    trackers: tuple[TrackerConfig, ...] = ()

    def tracker(self, tracker_id: str) -> TrackerConfig | None:
        return next((t for t in self.trackers if t.id == tracker_id), None)

    @property
    def is_enterprise(self) -> bool:
        return self.mode == MODE_ENTERPRISE

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def workspace_dir(self) -> Path:
        """Absolute `<workspace_root>/workspace` for the PRIMARY checkout. Session
        worktrees use workspace_rel against their own root instead."""
        return self.repo_root / self.workspace_root / "workspace"

    @property
    def workspace_rel(self) -> str:
        """Repo-root-relative workspace path, valid inside any worktree."""
        return f"{self.workspace_root}/workspace"

    @property
    def archive_rel(self) -> str:
        return f"{self.workspace_root}/workspace/archive"

    @property
    def local_dir(self) -> Path:
        return self.repo_root / LOCAL_DIR_NAME


def _read_optional(path: Path) -> str:
    if path.is_file():
        return path.read_text().strip()
    return ""


def _parse_mode(raw: dict, config_path: Path) -> str:
    """Reads [enterprise] mode / enabled. A typo here decides whether the whole
    instance requires authentication, so an unrecognized value is fatal rather than
    silently falling back to the permissive default."""
    enterprise = raw.get("enterprise", {})
    mode = str(enterprise.get("mode", "")).strip().lower()
    if not mode:
        # Convenience form: `enabled = true` is the same as `mode = "enterprise"`.
        mode = MODE_ENTERPRISE if bool(enterprise.get("enabled", False)) else MODE_PERSONAL
    if mode not in MODES:
        print(
            f"[pm_studio] FATAL: {config_path} has [enterprise] mode = {mode!r}; "
            f"must be one of: {', '.join(MODES)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return mode


def _parse_costing(raw: dict) -> CostingConfig:
    """Reads the optional [costing] table."""
    costing = raw.get("costing")
    if not isinstance(costing, dict):
        return CostingConfig()
    blended = costing.get("blended_rate")
    weights = costing.get("weights")
    return CostingConfig(
        blended_rate=float(blended) if blended is not None else None,
        default_capacity_hours=float(
            costing.get("default_capacity_hours", DEFAULT_CAPACITY_HOURS)
        ),
        currency=str(costing.get("currency", DEFAULT_CURRENCY)).strip() or DEFAULT_CURRENCY,
        weights={str(k): float(v) for k, v in (weights or {}).items()},
    )


def _parse_smtp(raw: dict) -> SmtpConfig | None:
    """Reads the optional [smtp] table. The password may be given inline, but
    `password_env` (the name of an environment variable to read it from) is preferred
    so the secret never has to sit in a file."""
    smtp = raw.get("smtp")
    if not isinstance(smtp, dict) or not smtp:
        return None
    password = str(smtp.get("password", ""))
    password_env = str(smtp.get("password_env", "")).strip()
    if password_env:
        password = os.environ.get(password_env, "")
    elif password:
        # CONFIGURATION.md tells operators to commit pm_studio_local/ (session worktrees
        # need it), so an inline password is a password in git. Warn rather than refuse:
        # a deployment that has already done it should still boot, but nobody should be
        # able to do it without being told.
        print(
            "[pm_studio] WARNING: [smtp] password is set inline in "
            f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME}, which is normally committed to your "
            "repo - that puts the password in git history. Use `password_env = "
            '"YOUR_ENV_VAR_NAME"` instead and unset `password`.',
            file=sys.stderr,
        )
    return SmtpConfig(
        host=str(smtp.get("host", "")).strip(),
        port=int(smtp.get("port", DEFAULT_SMTP_PORT)),
        from_address=str(smtp.get("from_address", "")).strip(),
        username=str(smtp.get("username", "")).strip(),
        password=password,
        use_tls=bool(smtp.get("use_tls", True)),
    )


TRACKER_PROVIDERS = ("jira", "ado")


def _parse_trackers(raw: dict, config_path: Path) -> tuple[TrackerConfig, ...]:
    """Reads the optional `[[trackers]]` array-of-tables.

    Malformed entries are dropped with a warning rather than being fatal: an unreachable
    tracker must never stop the studio from booting, because the roadmap board is useful
    without it. A bad *provider*, though, is loud - it is a typo that would otherwise look
    like "sync silently does nothing".
    """
    entries = raw.get("trackers")
    if not isinstance(entries, list):
        return ()

    trackers: list[TrackerConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        where = f"[[trackers]] #{index + 1}"

        provider = str(entry.get("provider", "")).strip().lower()
        if provider not in TRACKER_PROVIDERS:
            print(
                f"[pm_studio] WARNING: {where} in {config_path} has provider "
                f"{provider!r}; expected one of {', '.join(TRACKER_PROVIDERS)}. Skipping it.",
                file=sys.stderr,
            )
            continue

        organization = str(entry.get("organization", "")).strip().strip("/")
        base_url = str(entry.get("base_url", "")).strip().rstrip("/")
        if provider == "ado" and not base_url and organization:
            base_url = f"https://dev.azure.com/{organization}"

        # A stable id is what links on roadmap items point at, so it must not drift.
        # Defaulting it to the provider keeps a single-tracker config to a few lines.
        tracker_id = str(entry.get("id", "")).strip() or provider
        if tracker_id in seen:
            print(
                f"[pm_studio] WARNING: {where} in {config_path} reuses id "
                f"{tracker_id!r}; ids identify a tracker on every linked item, so this "
                "one is skipped. Give each tracker its own `id`.",
                file=sys.stderr,
            )
            continue
        seen.add(tracker_id)

        token = str(entry.get("token", ""))
        token_env = str(entry.get("token_env", "")).strip()
        if token_env:
            token = os.environ.get(token_env, "")
            if not token:
                print(
                    f"[pm_studio] WARNING: {where} in {config_path} sets token_env = "
                    f"{token_env!r}, but that environment variable is empty or unset, so "
                    f"tracker {tracker_id!r} cannot sync until it is exported.",
                    file=sys.stderr,
                )
        elif token:
            # Same reasoning as [smtp] password above: pm_studio_local/ is normally
            # committed, so an inline token is a token in git history.
            print(
                f"[pm_studio] WARNING: {where} in {config_path} sets `token` inline, "
                "which is normally committed to your repo - that puts an API token in git "
                'history. Use `token_env = "YOUR_ENV_VAR_NAME"` instead and unset `token`.',
                file=sys.stderr,
            )

        projects = entry.get("projects")
        if isinstance(projects, str):
            projects = [projects]
        project_keys = tuple(
            str(p).strip() for p in (projects or []) if str(p).strip()
        )

        try:
            interval = float(
                entry.get("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES)
            )
        except (TypeError, ValueError):
            interval = DEFAULT_SYNC_INTERVAL_MINUTES
        # Floored rather than rejected: a config asking for every 5 seconds is a mistake,
        # and hammering someone's tracker is worse than quietly slowing down.
        interval = max(MIN_SYNC_INTERVAL_MINUTES, interval)

        trackers.append(
            TrackerConfig(
                id=tracker_id,
                provider=provider,
                label=str(entry.get("label", "")).strip()
                or ("Jira" if provider == "jira" else "Azure DevOps"),
                base_url=base_url,
                projects=project_keys,
                username=str(entry.get("username", "") or entry.get("email", "")).strip(),
                token=token,
                organization=organization,
                sync_interval_minutes=interval,
            )
        )
    return tuple(trackers)


def load_config(repo_root: Path | None = None) -> Config:
    """Builds the Config for one target repo. Every part is optional: with no
    pm_studio_local/ at all you get a fully generic (but working) deployment."""
    root = (repo_root or Path.cwd()).resolve()
    local_dir = root / LOCAL_DIR_NAME

    raw: dict = {}
    config_path = local_dir / CONFIG_FILE_NAME
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text())
        except tomllib.TOMLDecodeError as exc:
            # A broken config must be loud, not silently generic: a deployment that
            # thinks it has products/instructions and silently loses them would
            # behave differently with no visible cause.
            print(f"[pm_studio] FATAL: {config_path} is not valid TOML: {exc}", file=sys.stderr)
            raise SystemExit(2)

    project = raw.get("project", {})
    server = raw.get("server", {})
    # [models] maps model id -> UI label; the reserved key "default" names which id
    # new sessions start on. Empty table = use the package defaults (models.py).
    models_raw = dict(raw.get("models", {}))
    default_model = str(models_raw.pop("default", "")).strip() or None

    knowledge_dir = local_dir / KNOWLEDGE_DIR_NAME
    knowledge_files: tuple[str, ...] = ()
    if knowledge_dir.is_dir():
        knowledge_files = tuple(
            sorted(
                str(p.relative_to(root))
                for p in knowledge_dir.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )
        )

    return Config(
        repo_root=root,
        project_name=str(project.get("name", "")).strip() or root.name,
        default_session_name=str(project.get("default_session_name", "")).strip()
        or DEFAULT_SESSION_NAME,
        workspace_root=str(project.get("workspace_root", "")).strip()
        or DEFAULT_WORKSPACE_ROOT,
        host=str(server.get("host", "")).strip() or DEFAULT_HOST,
        port=int(server.get("port", DEFAULT_PORT)),
        products={str(k): str(v) for k, v in raw.get("products", {}).items()},
        repo_layout=str(project.get("layout", "")).strip() or DEFAULT_REPO_LAYOUT,
        models={str(k): str(v) for k, v in models_raw.items()},
        default_model=default_model,
        pm_instructions=_read_optional(local_dir / PM_INSTRUCTIONS_NAME),
        dev_instructions=_read_optional(local_dir / DEV_INSTRUCTIONS_NAME),
        knowledge_files=knowledge_files,
        mode=_parse_mode(raw, config_path),
        smtp=_parse_smtp(raw),
        costing=_parse_costing(raw),
        trackers=_parse_trackers(raw, config_path),
    )


# Loaded once at import from the process cwd - the same "one process, one repo"
# assumption the rest of the package (module-level stores, registries) already makes.
# Tests replace this module attribute directly with a Config built around a tmp dir.
CONFIG = load_config()
