# Configuration — `pm_studio_local/`

Everything project-specific lives in the **target repo** under `pm_studio_local/`,
never in the installed package. Every file is optional: with nothing present, PM
Studio runs fully generic (no products, default port, generic prompts). All of it is
read once at server startup — restart after editing.

The core rule: local content is **append-only**. It fills identity slots and adds
instructions after the shared prompts; it cannot replace, reorder, or relax the
shared behavior. Deployments that need more than this can offer upstream a feature
request — never a local patch of package source.

```
pm_studio_local/
  config.toml           # identity + server + products + models
  PM_INSTRUCTIONS.md    # appended to every PM system prompt
  DEV_INSTRUCTIONS.md   # appended to every dev-agent dispatch
  knowledge/            # private reference docs, listed to the PM
```

Run `python -m pm_studio init` to scaffold all of it with commented templates
(non-destructive: existing files are never touched).

## `config.toml`

```toml
[project]
# Display name of this deployment (defaults to the repo directory name).
name = "Acme Intranet"
# Name of the default session pinned to the primary checkout on main.
# Migrated systems typically keep whatever their default session was named.
default_session_name = "Main"
# Directory (relative to the repo root) holding `workspace/` runtime state.
# Fresh installs: "pm_studio". Migrated systems keep "pm_agent" so live session
# worktrees and registered git-worktree paths stay valid in place.
workspace_root = "pm_studio"
# Markdown lines describing where product sources live at the repo root -
# injected verbatim into the PM system prompt so every session starts oriented.
layout = '''
- `web/` - the customer-facing web app
- `packages/` - shared libraries consumed by the apps
'''

[server]
host = "127.0.0.1"   # keep it local - see the security model in the README
port = 8000

# Product taxonomy for the roadmap board: id = "Display Label".
# TOML order is display order (customer-facing first reads best). Omit for a
# single-product repo: sessions then run unpinned, with no per-product boards.
[products]
web = "Web App"
platform = "Platform / Shared Packages"

# Optional model allow-list override (id = "Label"). The reserved key `default`
# names the model new sessions start on. Omit the table entirely to use the
# package defaults. Useful when a deployment is restricted to specific model ids.
[models]
default = "claude-opus-4-8"
"claude-opus-4-8" = "Opus"
sonnet = "Sonnet"
haiku = "Haiku"
```

## `PM_INSTRUCTIONS.md`

Appended to the PM agent's system prompt for every session, framed as
project-specific rules that add to (never replace) the shared behavior. This is the
place for what must not live in the public package:

- enterprise restrictions ("never dispatch work touching `infra/` without an
  explicit stakeholder go-ahead")
- compliance constraints and review requirements
- domain knowledge and conventions
- standing stakeholder decisions

## `DEV_INSTRUCTIONS.md`

Appended to **every** dev-agent dispatch (including merge-conflict resolutions), at
dispatch time — the task record and UI keep showing exactly what the PM wrote.
Build/test conventions and hard restrictions dev agents must always follow:

- "Run `make test` before considering any task done."
- "Never touch files under `secrets/` or commit credentials of any kind."

## `knowledge/`

Private reference docs (any files). The PM's system prompt lists their paths and
tells it to Read the relevant one before making calls in its territory. Keep an
index line for each in your `PROJECT_INDEX.md` too if the stakeholder should find
them.

## Committing `pm_studio_local/`

Commit it to the target repo — it is project configuration, and PM sessions in other
worktrees need it. If parts are too sensitive even for the project repo, keep those
files out via your own `.gitignore` entries and accept that fresh worktrees won't
carry them until the session syncs from a checkout that has them.
