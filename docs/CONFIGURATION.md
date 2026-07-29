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

# Operating mode. Omit this table entirely for the default, `personal` - the
# historical single-trusted-user tool with no accounts and no login.
# `enterprise` turns on user accounts, email invites and roles. Turning it on
# changes who can reach the dev agents, so it is always opt-in.
# `enabled = true` is accepted as shorthand for mode = "enterprise".
# A value that is neither `personal` nor `enterprise` is a hard startup error -
# a typo here must never silently leave an instance open.
[enterprise]
mode = "personal"

# Optional outbound mail for enterprise invites. Omit the whole table and each
# invite instead gives the admin a copyable link, which needs no mail server.
# Prefer `password_env` (the NAME of an environment variable) over `password`
# so the secret never has to sit in a file you might commit.
[smtp]
host = "smtp.example.com"
port = 587
from_address = "pm-studio@example.com"
username = "pm-studio"
password_env = "PM_STUDIO_SMTP_PASSWORD"
use_tls = true
```

## Operating modes

`personal` (the default) is the tool as it has always been: one trusted user, no
accounts, everything bound to localhost.

`enterprise` adds a roster on top of exactly the same core loop. Nothing about
sessions, dispatch, the roadmap or git behavior changes — what changes is that
requests need an identity.

Turning it on:

1. Set `mode = "enterprise"` under `[enterprise]` and restart the server.
2. Open the server. With no accounts yet, every page redirects to `/setup`, which
   creates the **owner** account — full access, manages the roster. This is the
   personal-to-enterprise conversion step, and it migrates no data: sessions, chat
   history, tasks and roadmap files stay exactly where they are.
3. Invite people from `/people` (admins only). Each invite is mailed if `[smtp]` is
   configured, and always yields a copyable `/accept-invite?token=…` link. Invites
   expire after 7 days, are single-use, and re-inviting someone supersedes their old
   link.

Roles are `admin`, `pm`, `reviewer`, `viewer`. The roster and any cost data are
admin-only; the roadmap is readable by everyone, by design — transparency is the
default.

State lives in `<workspace_root>/workspace/accounts.json`, written `0600` and never
git-tracked, so password hashes and session tokens stay out of the repo. Passwords are
PBKDF2-HMAC-SHA256; login tokens and invite tokens are stored only as hashes, which
also means an invite link cannot be re-read later — revoke and re-invite instead.

**The PM agents keep working.** They reach the server over `curl` and have no browser
cookie, so the process mints a per-run agent token and splices it into the prompts'
curl examples. It is never persisted, holds the `pm` role (never `admin`, so it can't
reach the roster or cost data), and grants nothing beyond the endpoints an agent's
Bash allowlist already matched in personal mode.

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
