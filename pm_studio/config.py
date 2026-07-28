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
      config.toml           # [project], [server], [products], [models]
      PM_INSTRUCTIONS.md    # appended to every PM system prompt
      DEV_INSTRUCTIONS.md   # appended to every dev-agent dispatch
      knowledge/*.md        # local reference docs; the PM is pointed at them

The repo root is the process working directory (`python -m pm_studio` is run from the
target repo's root), NOT this package's install location.
"""

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

# Shown in the PM system prompt when the target repo hasn't described its own source
# layout yet - honest about not knowing, and points at the fix.
DEFAULT_REPO_LAYOUT = (
    "- (This project has not described its source layout in "
    f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} yet - explore the repo root to learn where "
    "sources live, and suggest the stakeholder fill in the `layout` setting so future "
    "sessions start oriented.)"
)


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
    )


# Loaded once at import from the process cwd - the same "one process, one repo"
# assumption the rest of the package (module-level stores, registries) already makes.
# Tests replace this module attribute directly with a Config built around a tmp dir.
CONFIG = load_config()
