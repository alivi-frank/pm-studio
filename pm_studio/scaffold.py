"""`python -m pm_studio init`: scaffolds a target repo for PM Studio.

Creates pm_studio_local/ (config + local instruction stubs), the seed root documents
(PROJECT_INDEX.md, PROJECT_STATUS.md), and the .gitignore block for runtime state.
Strictly non-destructive: anything that already exists is left untouched, so running
init in an already-configured (or migrated) repo is always safe.
"""

from pathlib import Path

from .config import (
    CONFIG,
    CONFIG_FILE_NAME,
    DEV_INSTRUCTIONS_NAME,
    KNOWLEDGE_DIR_NAME,
    LOCAL_DIR_NAME,
    PM_INSTRUCTIONS_NAME,
)

CONFIG_TEMPLATE = """\
# PM Studio deployment config - project identity for THIS repo.
# Everything here is optional; missing values fall back to package defaults.
# Docs: https://github.com/fromerosk/pm-studio (docs/CONFIGURATION.md)

[project]
name = "{project_name}"
# Name of the default session pinned to the primary checkout on main.
default_session_name = "Main"
# Directory (relative to the repo root) holding workspace/ runtime state.
# Migrated deployments keep their historical "pm_agent" here.
workspace_root = "pm_studio"
# Markdown lines describing where product sources live at the repo root -
# injected verbatim into the PM system prompt so every session starts oriented.
layout = '''
- (describe each product/source directory here, one line each, e.g.:)
- `web/` - the customer-facing web app
- `packages/` - shared libraries
'''

[server]
host = "127.0.0.1"
port = 8000

# The product taxonomy for the roadmap board: id = "Display Label".
# TOML order is display order. Delete or leave empty for a single-product repo
# with unpinned sessions only.
[products]
# web = "Web App"
# platform = "Platform / Shared Packages"

# Optional model allow-list override (id = "Label"); the reserved key `default`
# names the model new sessions start on. Omit the whole table to use package
# defaults (Opus/Sonnet/Haiku).
# [models]
# default = "claude-opus-4-8"
# "claude-opus-4-8" = "Opus"
# sonnet = "Sonnet"
"""

PM_INSTRUCTIONS_TEMPLATE = """\
# Local PM instructions

Everything in this file is appended to the PM agent's system prompt for every
session in this repo. Use it for deployment-specific rules and knowledge that the
shared PM Studio package must not contain: company restrictions, compliance rules,
domain conventions, standing stakeholder decisions.

These instructions ADD to the shared PM behavior; they cannot replace it.

<!-- Replace this comment with your rules, e.g.:
- Never dispatch work that touches `infra/` without an explicit stakeholder go-ahead.
- All customer-facing copy must be reviewed against docs/BRAND_VOICE.md.
-->
"""

DEV_INSTRUCTIONS_TEMPLATE = """\
# Local dev-agent instructions

Everything in this file is appended to every dev-agent dispatch (and merge-conflict
resolution) in this repo. Use it for build/test conventions and hard restrictions
dev agents must always follow, independent of what any single task says.

<!-- Replace this comment with your rules, e.g.:
- Run `make test` before considering any task done.
- Never touch files under `secrets/` or commit credentials of any kind.
-->
"""

PROJECT_INDEX_TEMPLATE = """\
# Project Index (read this first)

This is the map of every document in this project. PM agent: read this file at the
start of any conversation where you have no prior turns in context (a fresh session),
before you say anything to the stakeholder about scope, status, or next steps. It tells
you what exists and where - follow the links to whichever doc answers your question,
rather than guessing or telling the stakeholder you're starting from scratch.

## Start here
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** - the current, durable summary of what's
  built, what's decided, what's open. Always read in full on a fresh session. Never
  wiped when the live spec/chat gets reset.

## Repo layout (product sources live at the root)
- (list each product/source directory here with a one-line description)
- **[{local_dir}/]({local_dir}/)** - PM Studio deployment config and local
  instructions for this repo.
- **[docs/](docs/)** - durable reference docs (list each with a one-line hook).

## Live working docs (reset between phases - may be blank right now)
- **[{workspace_rel}/current/SPEC.md]({workspace_rel}/current/SPEC.md)** - the
  spec for what's being actively built right now. Absence does not mean no history -
  check PROJECT_STATUS.md first.

## Product roadmap board (live, structured - not a markdown doc)
- Board at `/roadmap` on the running PM Studio server; data in
  `{workspace_rel}/roadmap/<product>.json` (server-owned, not git-tracked).
"""

PROJECT_STATUS_TEMPLATE = """\
# Project Status (durable — read this first)

No project history yet. This is a brand-new project - proceed to discovery.
"""

GITIGNORE_BLOCK_TEMPLATE = """\

# PM Studio runtime/bookkeeping state (per-session, not product content)
{workspace_rel}/current/chat_history.json
{workspace_rel}/current/pm_session_id.txt
{workspace_rel}/current/tasks/
{workspace_rel}/sessions/
{workspace_rel}/sessions.json
{workspace_rel}/roadmap/
"""


def _write_if_absent(path: Path, content: str, created: list[str], root: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    created.append(str(path.relative_to(root)))


def run_init(root: Path) -> None:
    created: list[str] = []
    local_dir = root / LOCAL_DIR_NAME

    _write_if_absent(
        local_dir / CONFIG_FILE_NAME,
        CONFIG_TEMPLATE.format(project_name=root.name),
        created,
        root,
    )
    _write_if_absent(local_dir / PM_INSTRUCTIONS_NAME, PM_INSTRUCTIONS_TEMPLATE, created, root)
    _write_if_absent(local_dir / DEV_INSTRUCTIONS_NAME, DEV_INSTRUCTIONS_TEMPLATE, created, root)
    knowledge_dir = local_dir / KNOWLEDGE_DIR_NAME
    if not knowledge_dir.exists():
        knowledge_dir.mkdir(parents=True)
        created.append(f"{LOCAL_DIR_NAME}/{KNOWLEDGE_DIR_NAME}/")

    seed_format = {"local_dir": LOCAL_DIR_NAME, "workspace_rel": CONFIG.workspace_rel}
    _write_if_absent(
        root / "PROJECT_INDEX.md", PROJECT_INDEX_TEMPLATE.format(**seed_format), created, root
    )
    _write_if_absent(root / "PROJECT_STATUS.md", PROJECT_STATUS_TEMPLATE, created, root)

    # .gitignore: append the runtime-state block only if this workspace root isn't
    # mentioned yet (a migrated repo already ignores its pm_agent/ equivalents).
    gitignore = root / ".gitignore"
    block = GITIGNORE_BLOCK_TEMPLATE.format(workspace_rel=CONFIG.workspace_rel)
    existing = gitignore.read_text() if gitignore.exists() else ""
    if f"{CONFIG.workspace_rel}/sessions/" not in existing:
        gitignore.write_text(existing + block)
        created.append(".gitignore (runtime-state block appended)")

    if created:
        print("PM Studio init - created:")
        for entry in created:
            print(f"  {entry}")
    else:
        print("PM Studio init - nothing to do, everything already in place.")
    print(
        f"\nNext: fill in {LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} (products, layout), "
        f"then run `python -m pm_studio` from the repo root."
    )
