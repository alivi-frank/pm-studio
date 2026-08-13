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

One file outside `pm_studio_local/` is also read: an optional `.env` at the repo root,
merged into the environment before any `*_env` name below is resolved (see
_load_env_file). It stays outside because it holds the actual secrets and the host repo
git-ignores it, while `pm_studio_local/` is committed.
"""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NoReturn

LOCAL_DIR_NAME = "pm_studio_local"
CONFIG_FILE_NAME = "config.toml"
PM_INSTRUCTIONS_NAME = "PM_INSTRUCTIONS.md"
DEV_INSTRUCTIONS_NAME = "DEV_INSTRUCTIONS.md"
KNOWLEDGE_DIR_NAME = "knowledge"

# Optional per-repo environment file, at the repo ROOT rather than inside
# pm_studio_local/: it carries the credentials themselves, so it belongs in the file the
# host repo already git-ignores, not in the directory it commits.
ENV_FILE_NAME = ".env"

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
class TrackerRoute:
    """One import route: matching tickets become changes on this product, attributed to
    this system.

    Two match shapes, for the two shapes a tracker project comes in. A project that
    serves MANY products routes by `component` (Jira component / ADO area path); a
    project that IS one product routes by `project` - every ticket in it, component or
    not. A route may set either or both (both = this component within that project).
    Component routes always win over project routes: the more specific claim is the
    truer one, and a project-wide default must not swallow the exceptions declared
    beside it.

    `system` may be empty - the import then lands unattributed on an in-scope product,
    which the existing unattributed report surfaces. An imported change you can see
    beats a refused one you can't."""

    product: str
    component: str = ""
    project: str = ""
    system: str = ""


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
    # Jira Cloud authenticates as email + API token; ADO as an empty user + PAT. Both the
    # username and the token can be supplied by env var (`username_env`/`email_env` and
    # `token_env`), which is what lets a deployment publish its pm_studio_local/ safely.
    username: str = ""
    token: str = ""
    # ADO only: the dev.azure.com organization name.
    organization: str = ""
    # How often the background sync re-pulls this tracker. Per tracker because a busy
    # Jira and a quiet ADO project don't deserve the same polling rate.
    sync_interval_minutes: float = DEFAULT_SYNC_INTERVAL_MINUTES
    # IMPORT: which ticket types become changes, and how their components route onto
    # products. Both empty = the historical behavior byte for byte (catalog sync and
    # manual linking only). Declaring exactly one of the two is fatal at load - it
    # would be a config that looks like it imports and silently doesn't.
    import_types: tuple[str, ...] = ()
    routes: tuple[TrackerRoute, ...] = ()
    # Bound on the catalog pull: only tickets CHANGED on/after this date (YYYY-MM-DD)
    # are synced. For a tracker with years of history where only the recent slice is
    # planning material - the catalog, the link picker and every import pass then see
    # just that slice. Linked tickets older than the bound stay resolvable: the sync
    # fetches any linked-but-missing ticket individually (refresh_missing). Empty = no
    # bound, the historical behavior.
    since: str = ""
    # EXCLUSION: components (Jira) / area paths (ADO) whose tickets this deployment
    # declares OUT of every automatic pass - not imported as changes, not imported or
    # linked as projects, not used for assignment. For the slice of a tracker that is
    # tracked authoritatively somewhere else (the classic case: the same work mirrored
    # in a second tracker). A ticket is excluded when one of its components matches,
    # or when anything up its parent chain is excluded - children routinely carry no
    # components of their own. Manual linking stays possible: exclusion governs what
    # the sync does on its own, never what a human does on purpose.
    exclude_components: tuple[str, ...] = ()

    @property
    def imports(self) -> bool:
        return bool(self.import_types and self.routes)

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


# A product's lifecycle stage. Closed vocabulary, unknown value fatal at load - the
# same contract as [enterprise] mode: a typo must not silently mean something the
# operator did not write. "ga" is the steady state and what an undeclared stage means.
PRODUCT_STAGES = ("discovery", "development", "ga", "sunset")


@dataclass(frozen=True)
class ProductMeta:
    """Operator-declared facts about one product: who owns it, who builds it, where it
    is in its life. Context, not policy - nothing authorizes or bills against these.

    Lives in config rather than a store or UI on purpose: this is the same kind of
    slowly-changing, operator-owned fact as the taxonomy itself, and a second editable
    home for it would drift from the first. All fields optional; a product with no
    metadata simply has no entry (see Config.product_meta)."""

    description: str = ""
    owner: str = ""
    team: str = ""
    stage: str = "ga"

    @property
    def is_empty(self) -> bool:
        return not (self.description or self.owner or self.team) and self.stage == "ga"


@dataclass(frozen=True)
class SystemSpec:
    """One declared system: a bounded piece of technology/code, the unit a change is
    contained within. Distinct from a product - see _parse_systems for the split.

    Everything but the label is optional and, for now, purely descriptive: `path` and
    `repo` say where the code lives, and `guidance`/`pipelines` are the seams for the
    engineering guidance and CI/CD a system will carry later. Nothing enforces them yet,
    so declaring a system costs one line and a deployment can fill the rest in over time.
    """

    label: str
    # Repo-root-relative source folder, and/or the system's own repo when it has one.
    # A system can legitimately have both (a folder in this repo AND its own remote),
    # one, or neither (a system tracked here but built somewhere unrelated).
    path: str = ""
    repo: str = ""
    # Forward-looking, descriptive only: where this system's engineering guidance lives
    # and the pipelines that build it. Surfaced in the UI and the PM prompt; not acted on.
    guidance: str = ""
    pipelines: tuple[str, ...] = ()


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
    # Every product this deployment declares, id -> display label, ordered depth first so
    # each product is immediately followed by its own descendants (see _parse_products).
    # Sub-products are products in every respect - the flatness here is what keeps every
    # existing caller that just wants "is this a real product id" or "what do I call it"
    # unchanged, at any depth.
    products: dict[str, str] = field(default_factory=dict)
    # The hierarchy, held separately and only for the products that HAVE a parent:
    # child id -> parent id, at any depth (a child may itself be a parent). Empty on a
    # deployment with a flat [products] table, which is exactly what every deployment
    # before hierarchy had. Kept out of `products` so the taxonomy stays one dict to
    # iterate and this stays a lookup you opt into.
    product_parents: dict[str, str] = field(default_factory=dict)
    # Every system this deployment declares, id -> spec, in declaration order (which is
    # display order, same convention as `products`). Empty on every deployment that has
    # not declared [systems] - and an empty table is what keeps the whole system layer
    # dormant, exactly as an absent [[trackers]] keeps the tracker feature dormant.
    systems: dict[str, SystemSpec] = field(default_factory=dict)
    # The many-to-many edge, from the product's side: product id -> the systems it
    # touches, in declaration order. Held from this side only because that is the side
    # the operator declares and the side a pinned PM asks about ("what do I touch?");
    # the reverse (which products touch a system) is derived in roadmap.products_of_system
    # rather than stored, so the two can never disagree.
    product_systems: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Operator-declared facts per product (owner, team, stage, description), only for
    # the products that declared any - same opt-in-lookup pattern as product_parents,
    # so a deployment that never writes a metadata key carries an empty dict and no
    # consumer has to know the feature exists.
    product_meta: dict[str, ProductMeta] = field(default_factory=dict)
    # Ids declared as BOTH a product and a system: the explicit, temporary state of an id
    # being reclassified from product to system. Its product board keeps loading and
    # stays fully usable, so the changes on it can be re-homed at the operator's pace
    # instead of being orphaned the moment the [products] entry is deleted. Empty on any
    # deployment that is not mid-restructure.
    transitional_ids: tuple[str, ...] = ()
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


def _parse_env_file(text: str) -> tuple[list[tuple[str, str]], list[int]]:
    """Splits `.env` text into (assignments in file order, 1-based numbers of lines that
    could not be read as one).

    Deliberately small: `KEY=value` one per line, `#` comment lines, blank lines, an
    optional `export ` prefix, and a single- or double-quoted value taken literally (which
    is how a value keeps a leading space or a `#`). No `$VAR` interpolation, no backslash
    escapes, no multi-line values - those are shell features, and a deployment that needs
    them should export the variable from the shell that already implements them rather
    than have this package grow a second, subtly different shell.
    """
    pairs: list[tuple[str, str]] = []
    bad_lines: list[int] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # `export FOO=bar` is what a file meant to be `source`d looks like; the prefix is
        # noise here. (`export=bar` is a variable named export, and splits differently.)
        words = line.split(None, 1)
        if words[0] == "export":
            line = words[1] if len(words) > 1 else ""
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or any(char.isspace() for char in key):
            bad_lines.append(number)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((key, value))
    return pairs, bad_lines


def _load_env_file(path: Path) -> None:
    """Merges the target repo's `.env` into os.environ, before anything here reads it.

    `.env` is a library convention, not an OS or shell feature: nothing in the shell or
    the interpreter reads that file on its own, so a deployment keeping its tracker token
    there has no such variable in os.environ unless something puts it there. This is that
    something - it is what lets `token_env`/`email_env`/`password_env` name a variable the
    operator only ever wrote in `.env`, with no wrapper script exporting it first.

    A variable already set in the environment always WINS over the file: a real export -
    from a run script, CI, systemd, a shell profile - is a deliberate act aimed at this one
    process, while `.env` is a default sitting in the repo, and a stale file must never
    quietly beat the environment. (Set-but-empty counts as unset, because every other env
    read in this module already treats "" that way.)

    A missing file is a silent no-op; most deployments have none. A file that IS read says
    which variables it supplied, by NAME only - the values are credentials and never go
    anywhere near a log.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    except OSError as exc:
        # Unreadable is worth saying out loud (a mode-0600 file owned by someone else is
        # exactly how a token goes missing) but never fatal: the studio boots without it.
        print(f"[pm_studio] WARNING: could not read {path}: {exc}", file=sys.stderr)
        return

    pairs, bad_lines = _parse_env_file(text)
    if bad_lines:
        numbers = ", ".join(str(number) for number in bad_lines)
        print(
            f"[pm_studio] WARNING: {path} line(s) {numbers} are not `KEY=value` and were "
            "skipped. A variable written there is not set.",
            file=sys.stderr,
        )

    applied: list[str] = []
    already_set: list[str] = []
    for key, value in pairs:
        if os.environ.get(key, ""):
            already_set.append(key)
            continue
        os.environ[key] = value
        applied.append(key)

    if applied:
        print(f"[pm_studio] {path}: set {', '.join(applied)}")
    if already_set:
        print(
            f"[pm_studio] {path}: {', '.join(already_set)} already in the environment, "
            "left unchanged"
        )


def _fatal(message: str) -> NoReturn:
    """Refuses to start, loudly. Used for config that would otherwise produce a
    deployment which looks like it is working while quietly meaning something the
    operator did not write - see _parse_mode and _parse_products."""
    print(f"[pm_studio] FATAL: {message}", file=sys.stderr)
    raise SystemExit(2)


def _parse_systems(raw: dict, config_path: Path) -> dict[str, SystemSpec]:
    """Reads [systems] into id -> SystemSpec, in declaration order.

    A SYSTEM is a bounded piece of technology/code - a service, an app, a module - and
    it is the unit a change is contained within: a change belongs to exactly one system,
    which is what makes its blast radius knowable. A PRODUCT is the business-facing
    thing: a line of business, or an umbrella over the technology that serves one. A
    product touches many systems and a system is touched by many products, which is why
    the edge between them is declared per product (`systems = [...]`) rather than being
    a level of either tree.

    The two tables are deliberately separate rather than one renamed into the other:
    products keep owning the roadmap boards (roadmaps are product- and initiative-first;
    a system never has a roadmap of its own), and every deployment that has not declared
    [systems] behaves exactly as it did before this table existed.

    Both entry spellings from [products] work here too - a bare label, or a table:

        [systems]
        claims = "Claims Processor"

        [systems.rides]
        label = "Rides & Logistics"
        path = "services/rides"
        repo = "github.com/org/rides"
    """
    declared = raw.get("systems", {})
    if not isinstance(declared, dict):
        _fatal(f"{config_path} has [systems] as a {type(declared).__name__}; it must be a table")

    systems: dict[str, SystemSpec] = {}
    for key, value in declared.items():
        system_id = str(key)
        if isinstance(value, str):
            systems[system_id] = SystemSpec(label=value or system_id)
            continue
        if not isinstance(value, dict):
            _fatal(
                f"{config_path} has [systems] entry {system_id!r} as a "
                f"{type(value).__name__}; a system is either a label string "
                '(claims = "Claims Processor") or a table with `label` and optional '
                "`path`, `repo`, `guidance` and `pipelines`"
            )
        # Systems do not nest. `parent` is meaningful in the sibling [products] table, so
        # an operator could reasonably expect it here - refuse loudly rather than accept
        # the line and silently drop the relationship they thought they declared.
        if "parent" in value:
            _fatal(
                f"{config_path} has system {system_id!r} with a `parent`, but systems do "
                "not nest. If it is contained by another system, declare it as its own "
                "system; to say which products touch it, list it in that product's "
                "`systems = [...]`."
            )
        pipelines = value.get("pipelines", ())
        if isinstance(pipelines, str) or not isinstance(pipelines, (list, tuple)):
            _fatal(
                f"{config_path} has system {system_id!r} with `pipelines` as a "
                f"{type(pipelines).__name__}; it must be an array, e.g. "
                'pipelines = ["rides-ci"]'
            )
        # A missing label is cosmetic, not structural - same call as [products] makes.
        systems[system_id] = SystemSpec(
            label=str(value.get("label", "")).strip() or system_id,
            path=str(value.get("path", "")).strip(),
            repo=str(value.get("repo", "")).strip(),
            guidance=str(value.get("guidance", "")).strip(),
            pipelines=tuple(str(p).strip() for p in pipelines if str(p).strip()),
        )
    return systems


def _parse_products(
    raw: dict, config_path: Path, systems: dict[str, SystemSpec] | None = None
) -> tuple[dict[str, str], dict[str, str], dict[str, tuple[str, ...]], dict[str, ProductMeta]]:
    """Reads [products] into (id -> label, child id -> parent id, id -> systems touched,
    id -> metadata for the products that declared any).

    Two spellings, both first class. A flat table of plain strings is what every
    deployment had before hierarchy existed, and those lines keep working byte for byte:

        [products]
        web = "Web App"                                  # a top-level product
        auth = { label = "Auth & Identity", parent = "web" }   # a child of web

        [products.billing]                               # the same child, spelled out
        label = "Billing"
        parent = "web"

    (TOML wants every bare `key = "value"` line in [products] before the first
    [products.x] sub-table, which is the only reason the inline form is shown first -
    the two mean the same thing.)

    A child is a full product in every other respect: its own board file, its own
    sessions, its own items, its own id in every URL. `parent` is purely an organizing
    pointer, which is what makes re-parenting one later a config edit rather than a data
    migration - no stored change carries the hierarchy, only the product id it always
    carried.

    Nesting goes as deep as a deployment declares - a child can itself be a parent:

        [products]
        web  = "Web App"
        auth = { label = "Auth & Identity", parent = "web" }
        sso  = { label = "SSO", parent = "auth" }          # three levels

    Nothing in the model counts levels; every consumer walks the pointer (see
    roadmap.subtree_products), so depth is the operator's decision about their own
    org, not this package's opinion. The board indents one step per level, and past
    four or five that gets narrow - a practical limit, not an enforced one.

    A bad `parent` is fatal for the same reason a bad [enterprise] mode is: a child that
    silently became a top-level product is a working-looking deployment with one board
    too many, and nothing anywhere points at the typo. A CYCLE is fatal for a harder
    reason - the products in it are reachable from no root at all, so they would simply
    vanish from the taxonomy while their boards sat on disk holding items.

    A product also declares WHICH SYSTEMS IT TOUCHES (see _parse_systems), which is the
    many-to-many edge between the two taxonomies:

        [products.checkout]
        label   = "Checkout"
        systems = ["claims", "rides"]      # every id must be a declared system

    `parent` and `systems` answer different questions and never substitute for each
    other: `parent` says which product contains this one, `systems` says which
    technology this product is built out of. Neither implies the other - a child product
    touches whatever it touches, independently of its parent.

    A product may also carry METADATA - facts about it rather than structure:

        [products.checkout]
        label = "Checkout"
        description = "Guest and member checkout, up to payment capture"
        owner = "jane.doe"            # product decision-maker, free text
        team = "Payments Engineering" # who builds it, free text
        stage = "ga"                  # discovery | development | ga | sunset

    All optional and all context, not policy: the PM prompt names them and the board
    badges a non-ga stage, but nothing authorizes, blocks, or bills against them. An
    unknown `stage` is fatal (see PRODUCT_STAGES); the free-text fields are not
    validated beyond being strings.
    """
    declared = raw.get("products", {})
    if not isinstance(declared, dict):
        _fatal(f"{config_path} has [products] as a {type(declared).__name__}; it must be a table")

    known_systems = systems or {}
    labels: dict[str, str] = {}
    parents: dict[str, str] = {}
    touches: dict[str, tuple[str, ...]] = {}
    meta: dict[str, ProductMeta] = {}
    for key, value in declared.items():
        product_id = str(key)
        if isinstance(value, str):
            # The historical form: the whole value is the display label.
            labels[product_id] = value
            continue
        if not isinstance(value, dict):
            _fatal(
                f'{config_path} has [products] entry {product_id!r} as a '
                f"{type(value).__name__}; a product is either a label string "
                '(web = "Web App") or a table with `label` and optional `parent` '
                "and `systems`"
            )
        # A missing label is cosmetic, not structural - falling back to the id shows the
        # mistake on the board immediately without refusing to boot over a display string.
        labels[product_id] = str(value.get("label", "")).strip() or product_id
        parent = str(value.get("parent", "")).strip()
        if parent:
            parents[product_id] = parent
        stage = str(value.get("stage", "")).strip() or "ga"
        if stage not in PRODUCT_STAGES:
            _fatal(
                f"{config_path} has product {product_id!r} with stage {stage!r}; it must "
                f"be one of {', '.join(PRODUCT_STAGES)}. A typo here would silently read "
                "as the steady state, so it is refused instead."
            )
        entry_meta = ProductMeta(
            description=str(value.get("description", "")).strip(),
            owner=str(value.get("owner", "")).strip(),
            team=str(value.get("team", "")).strip(),
            stage=stage,
        )
        if not entry_meta.is_empty:
            meta[product_id] = entry_meta
        if "systems" in value:
            touched = value["systems"]
            if isinstance(touched, str) or not isinstance(touched, (list, tuple)):
                _fatal(
                    f"{config_path} has product {product_id!r} with `systems` as a "
                    f"{type(touched).__name__}; it must be an array of declared system "
                    'ids, e.g. systems = ["claims", "rides"]'
                )
            # Order is the operator's, duplicates are a harmless typo rather than a
            # structural error - dedupe and keep the first mention's position.
            touched_ids: dict[str, None] = {}
            for entry in touched:
                system_id = str(entry).strip()
                if not system_id:
                    continue
                if system_id not in known_systems:
                    _fatal(
                        f"{config_path} has product {product_id!r} touching system "
                        f"{system_id!r}, which is not declared in [systems] (declared: "
                        f"{', '.join(known_systems) or 'none'})"
                    )
                touched_ids[system_id] = None
            touches[product_id] = tuple(touched_ids)

    for child, parent in parents.items():
        if parent == child:
            _fatal(f"{config_path} has product {child!r} declared as its own parent")
        if parent not in labels:
            # Pointing a product's `parent` at a system is the predictable confusion once
            # both tables exist, so name the fix instead of only the fact.
            if parent in known_systems:
                _fatal(
                    f"{config_path} has product {child!r} with parent {parent!r}, which "
                    "is a system, not a product. A product's `parent` is the product "
                    "that contains it; to say this product is built on that system, add "
                    f'it to {child!r}\'s `systems = ["{parent}"]` instead.'
                )
            _fatal(
                f"{config_path} has product {child!r} with parent {parent!r}, which is "
                f"not a declared product (declared: {', '.join(labels) or 'none'})"
            )

    # Every chain has to terminate at a top-level product. Walking up from each child is
    # what proves it: with a cycle, the products in it have no root above them, so the
    # depth-first ordering below would never reach them and they would silently drop out
    # of the taxonomy - the board short a section, `create` rejecting a product id the
    # config plainly declares.
    for child in parents:
        seen = {child}
        cursor = parents[child]
        while True:
            if cursor in seen:
                _fatal(
                    f"{config_path} has a cycle in [products]: "
                    f"{' -> '.join([*seen, cursor])}. Every product's `parent` chain must "
                    "end at a top-level product."
                )
            seen.add(cursor)
            if cursor not in parents:
                break
            cursor = parents[cursor]

    # Declaration order, re-laid depth first so each product is immediately followed by
    # its own descendants. Every consumer treats iteration order as display order (the
    # board's sections, the session picker), so the tree is ordered once, here, rather
    # than re-derived by each of them.
    ordered: dict[str, str] = {}

    def emit(product_id: str) -> None:
        ordered[product_id] = labels[product_id]
        for child, parent in parents.items():
            # `parents` is in declaration order, so siblings come out in the order the
            # operator wrote them.
            if parent == product_id:
                emit(child)

    for product_id in labels:
        if product_id not in parents:
            emit(product_id)
    # Guaranteed by the cycle check above; asserted because a product missing here is
    # invisible everywhere downstream rather than loudly broken.
    assert len(ordered) == len(labels), "product ordering dropped a declared product"
    return ordered, parents, touches, meta


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


def _parse_trackers(
    raw: dict,
    config_path: Path,
    products: dict[str, str] | None = None,
    product_systems: dict[str, tuple[str, ...]] | None = None,
    systems: dict[str, SystemSpec] | None = None,
) -> tuple[TrackerConfig, ...]:
    """Reads the optional `[[trackers]]` array-of-tables.

    Malformed entries are dropped with a warning rather than being fatal: an unreachable
    tracker must never stop the studio from booting, because the roadmap board is useful
    without it. A bad *provider*, though, is loud - it is a typo that would otherwise look
    like "sync silently does nothing".

    IMPORT ROUTES are the exception to drop-with-warning: they are validated fatally,
    against the product and system taxonomies passed in. A route that quietly skipped a
    misspelled product would import that component's tickets nowhere while the config
    plainly says otherwise - the exact failure mode _fatal exists for:

        [[trackers]]
        # ... connection keys ...
        import_types = ["Epic", "Story", "Bug"]   # which ticket types become changes

        [[trackers.routes]]
        component = "Checkout Web"   # Jira component / ADO area path
        product = "checkout"         # must be a declared product
        system = "claims"            # optional; must be one that product touches

    `import_types` and `routes` only work together, so declaring exactly one of them
    is fatal too, rather than a config that looks like it imports and silently doesn't.
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

        # The account name (Jira Cloud wants the email). Not a secret the way the token is,
        # but pm_studio_local/ is committed and a deployment may publish that repo, so
        # `username_env` / `email_env` exist to keep a work address out of it too.
        username = str(entry.get("username", "") or entry.get("email", "")).strip()
        username_env = str(
            entry.get("username_env", "") or entry.get("email_env", "")
        ).strip()
        if username_env:
            username = os.environ.get(username_env, "").strip()
            if not username:
                print(
                    f"[pm_studio] WARNING: {where} in {config_path} sets username_env/"
                    f"email_env = {username_env!r}, but that environment variable is empty "
                    f"or unset, so tracker {tracker_id!r} will fail to authenticate.",
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

        import_types_raw = entry.get("import_types", [])
        if isinstance(import_types_raw, str) or not isinstance(import_types_raw, (list, tuple)):
            _fatal(
                f"{config_path} {where} has `import_types` as a "
                f"{type(import_types_raw).__name__}; it must be an array of ticket "
                'types, e.g. import_types = ["Epic", "Story"]'
            )
        import_types = tuple(
            str(t).strip() for t in import_types_raw if str(t).strip()
        )

        since = str(entry.get("since", "")).strip()
        if since:
            try:
                date.fromisoformat(since)
            except ValueError:
                _fatal(
                    f"{config_path} {where} has since = {since!r}; it must be a "
                    'calendar date as "YYYY-MM-DD"'
                )

        exclude_raw = entry.get("exclude_components", [])
        if isinstance(exclude_raw, str) or not isinstance(exclude_raw, (list, tuple)):
            _fatal(
                f"{config_path} {where} has `exclude_components` as a "
                f"{type(exclude_raw).__name__}; it must be an array of component/area "
                'names, e.g. exclude_components = ["CapAdmin"]'
            )
        exclude_components = tuple(
            str(c).strip() for c in exclude_raw if str(c).strip()
        )

        routes_raw = entry.get("routes", [])
        if not isinstance(routes_raw, list):
            _fatal(
                f"{config_path} {where} has `routes` as a {type(routes_raw).__name__}; "
                "declare each route as its own [[trackers.routes]] table"
            )
        known_products = products or {}
        known_product_systems = product_systems or {}
        known_systems = systems or {}
        routes: list[TrackerRoute] = []
        seen_matchers: set[tuple[str, str]] = set()
        for r_index, route in enumerate(routes_raw):
            r_where = f"{where} route #{r_index + 1}"
            if not isinstance(route, dict):
                _fatal(
                    f"{config_path} {r_where} must be a table with `product` and a "
                    "`component` and/or `project` to match on"
                )
            component = str(route.get("component", "")).strip()
            project = str(route.get("project", "")).strip()
            product = str(route.get("product", "")).strip()
            system = str(route.get("system", "")).strip()
            if not product:
                _fatal(f"{config_path} {r_where} needs a `product`")
            if not component and not project:
                _fatal(
                    f"{config_path} {r_where} matches nothing - give it a `component`, "
                    "a `project`, or both"
                )
            if project and project not in project_keys:
                # A route scoped to a project the tracker does not sync can never fire:
                # a dead route that the config plainly declares is the silent-no-op
                # failure _fatal exists for.
                _fatal(
                    f"{config_path} {r_where} is scoped to project {project!r}, which "
                    f"this tracker does not sync (projects: "
                    f"{', '.join(project_keys) or 'none'})"
                )
            matcher = (project.casefold(), component.casefold())
            if matcher in seen_matchers:
                # Two routes with one matcher would import each ticket wherever the
                # loop happened to look first - an ambiguity, not a preference.
                _fatal(
                    f"{config_path} {r_where} repeats the matcher "
                    f"(project={project or '*'!r}, component={component or '*'!r})"
                )
            seen_matchers.add(matcher)
            if product not in known_products:
                _fatal(
                    f"{config_path} {r_where} routes to product {product!r}, which is "
                    f"not declared (declared: {', '.join(known_products) or 'none'})"
                )
            if system:
                # The same two rules validate_system enforces on create, applied at load
                # so a bad route fails at boot, not at 3am mid-sync.
                if system not in known_systems:
                    _fatal(
                        f"{config_path} {r_where} attributes to system {system!r}, which "
                        f"is not declared (declared: {', '.join(known_systems) or 'none'})"
                    )
                declared = known_product_systems.get(product, ())
                if declared and system not in declared:
                    _fatal(
                        f"{config_path} {r_where} attributes to system {system!r}, but "
                        f"product {product!r} touches: {', '.join(declared)}"
                    )
            routes.append(
                TrackerRoute(
                    product=product, component=component, project=project, system=system
                )
            )

        if bool(import_types) != bool(routes):
            missing, present = (
                ("routes", "import_types") if import_types else ("import_types", "routes")
            )
            _fatal(
                f"{config_path} {where} declares `{present}` without `{missing}`; the "
                "two only import together, so this config looks like it imports and "
                "silently would not"
            )

        excluded_set = {c.casefold() for c in exclude_components}
        for route in routes:
            if route.component and route.component.casefold() in excluded_set:
                # Routed AND excluded is a contradiction, not a preference - one of the
                # two lines is a leftover, and which one wins should never be implicit.
                _fatal(
                    f"{config_path} {where} both routes and excludes component "
                    f"{route.component!r}; delete one of the two declarations"
                )

        trackers.append(
            TrackerConfig(
                id=tracker_id,
                provider=provider,
                label=str(entry.get("label", "")).strip()
                or ("Jira" if provider == "jira" else "Azure DevOps"),
                base_url=base_url,
                projects=project_keys,
                username=username,
                token=token,
                organization=organization,
                sync_interval_minutes=interval,
                import_types=import_types,
                routes=tuple(routes),
                since=since,
                exclude_components=exclude_components,
            )
        )
    return tuple(trackers)


def load_config(repo_root: Path | None = None) -> Config:
    """Builds the Config for one target repo. Every part is optional: with no
    pm_studio_local/ at all you get a fully generic (but working) deployment."""
    root = (repo_root or Path.cwd()).resolve()
    # FIRST, before any `*_env` name is resolved below ([smtp] password, tracker tokens):
    # the file can only supply a credential if it is merged in before the read.
    _load_env_file(root / ENV_FILE_NAME)
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

    # Systems first: a product declares which of them it touches, so they have to be
    # known before [products] can validate those references.
    systems = _parse_systems(raw, config_path)
    products, product_parents, product_systems, product_meta = _parse_products(raw, config_path, systems)
    # An id in both tables is the reclassification-in-progress state, not an error: see
    # Config.transitional_ids. Ordered by the [products] table so the report reads in the
    # order the operator sees their own board.
    transitional_ids = tuple(p for p in products if p in systems)

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
        products=products,
        product_parents=product_parents,
        systems=systems,
        product_systems=product_systems,
        product_meta=product_meta,
        transitional_ids=transitional_ids,
        repo_layout=str(project.get("layout", "")).strip() or DEFAULT_REPO_LAYOUT,
        models={str(k): str(v) for k, v in models_raw.items()},
        default_model=default_model,
        pm_instructions=_read_optional(local_dir / PM_INSTRUCTIONS_NAME),
        dev_instructions=_read_optional(local_dir / DEV_INSTRUCTIONS_NAME),
        knowledge_files=knowledge_files,
        mode=_parse_mode(raw, config_path),
        smtp=_parse_smtp(raw),
        costing=_parse_costing(raw),
        trackers=_parse_trackers(raw, config_path, products, product_systems, systems),
    )


# Loaded once at import from the process cwd - the same "one process, one repo"
# assumption the rest of the package (module-level stores, registries) already makes.
# Tests replace this module attribute directly with a Config built around a tmp dir.
CONFIG = load_config()
