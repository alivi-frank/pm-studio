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

# Optional external issue trackers. One [[trackers]] block per connection - a shop
# running both a Jira instance and an ADO project declares both. Omit the whole
# thing and the roadmap board looks exactly as it did before ticket linking
# existed. `projects` is required: it bounds what the sync pulls, and it is what
# lets a bare key like PROJ-123 be attributed to the right tracker.
# ALWAYS use `token_env` (the NAME of an environment variable) rather than
# `token` - pm_studio_local/ is normally committed, so an inline token is a token
# in your git history. PM Studio warns loudly if you use `token`.
[[trackers]]
id = "jira"                      # stable: every linked change stores this id
provider = "jira"
label = "Acme Jira"
base_url = "https://acme.atlassian.net"
projects = ["PROJ", "PLAT"]
email = "pm@acme.com"            # Jira Cloud authenticates as email + API token
token_env = "PM_STUDIO_JIRA_TOKEN"
sync_interval_minutes = 15       # default 15, floor 1

[[trackers]]
id = "ado"
provider = "ado"
label = "Acme ADO"
organization = "acme"            # base_url is derived: https://dev.azure.com/acme
projects = ["Platform"]
token_env = "PM_STUDIO_ADO_PAT"  # an ADO personal access token
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

### Roles

| Capability | admin | pm | reviewer | viewer |
| --- | :-: | :-: | :-: | :-: |
| View sessions and the whole roadmap | ✅ | ✅ | ✅ | ✅ |
| Work in PM sessions (send turns, retitle, reset) | ✅ | ✅ | — | — |
| Create / merge / sync / end sessions | ✅ | ✅ | — | — |
| **Dispatch dev agents** | ✅ | ✅ | — | — |
| Change the roadmap | ✅ | ✅ | — | — |
| Manage people (invites, roles, disable) | ✅ | — | — | — |

Reads are open to every role on purpose: transparency is the deployment default, so
there is no per-user filtering of the board. What a role changes is what you may *do*,
not what you may see.

**`dispatch_dev_task` is a code-execution boundary, not a UI affordance.** Dev agents
run with bypassed permissions inside the repo, so granting `pm` is granting the ability
to run arbitrary code on the host. It is enforced on the HTTP path and on the chat
websocket, and every dispatch is recorded in the audit log with the actor who asked
for it.

`reviewer` is read-only today. The role exists so it can be assigned now, but the
reviewer workflow itself is not implemented yet.

The matrix lives in one table in `pm_studio/authz.py` — that is the single place to
read or change what a role can do.

### Time &amp; cost attribution

Optional `[costing]` table:

```toml
[costing]
blended_rate = 120.0            # fallback hourly rate for anyone with no individual one
default_capacity_hours = 40.0   # default declared capacity per person per week
currency = "USD"                # label only; no conversion is done
weights = { pm_turn = 1.0, dev_task = 3.0, review = 1.0 }   # signal weights
```

**This is a distribution mechanism, not a stopwatch.** Nothing times anybody's screen.
Each person's *declared capacity* is split across projects in proportion to the activity
signals they generated, so a week always reconciles:

> Dana, 2026-W31: 28.4h Signup rewrite, 11.6h Billing — 40h total

That reconciliation is the point. Capacity is the input and signals only decide the
proportions, so the total is always a real week and the denominator isn't arguable.
Because it is explicitly an approximation, an admin can **override** any person's week
(whole-week, so the total stays meaningful) and the derived figures are kept alongside
the override — an estimate is never the only record.

**Two cost streams, never conflated:**

- **Labour** — distributed hours × rate (individual, else blended). An *estimate*.
- **Agent** — token/API spend, read from the Claude CLI's own JSON output. *Measured*.

An agent grinding for twenty minutes while nobody is at the keyboard is machine time,
not somebody's afternoon — so an auto-continuation's tokens are counted while its labour
weight is deliberately zero.

Rates and capacities live in `<workspace_root>/workspace/costing.json` (written `0600`,
never git-tracked); the activity log is append-only at `workspace/activity.jsonl`.
**Rates are the deployment's own compensation data** and never ship in the package. Rate
changes are audited, but the rate value itself is deliberately kept out of the audit
detail, since every admin can read that log.

Attribution needs a project, and sessions are pinned to a *product* (which is not a
level in the work model), so a session can be pointed at a **Project** — its PM turns
and dev tasks then count towards it. A session with no project falls back to the
catch-all, the same way an unparented change does.

Cost is visible to `admin` only (`view_cost`), and is additive **up to initiative**;
goal-level figures overlap and are never summed. See `/costing`.

### Audit log

Consequential actions (dispatches, session lifecycle changes, roadmap writes, role and
invite changes) append one JSON line to
`<workspace_root>/workspace/audit.jsonl`, recording who did it. Admins can read it at
`GET /audit`. It only ever grows and is never rewritten, so a crash cannot corrupt
earlier entries. Nothing is written in personal mode — with one trusted user, the git
snapshot history already answers the question.

State lives in `<workspace_root>/workspace/accounts.json`, written `0600`. It is kept
out of git by two independent mechanisms, because one was not enough: `init` lists it in
`.gitignore`, **and** every snapshot commit unstages it unconditionally (see
`gitsnapshot.SENSITIVE_WORKSPACE_FILES`). A PM turn snapshots the whole repo with
`git add -A`, so a deployment whose `.gitignore` predates this file would otherwise have
committed password hashes on its next turn. If one of these files is already tracked,
the server says so on every snapshot with the `git rm --cached` command to run. Passwords are
PBKDF2-HMAC-SHA256; login tokens and invite tokens are stored only as hashes, which
also means an invite link cannot be re-read later — revoke and re-invite instead.

**The PM agents keep working.** They reach the server over `curl` and have no browser
cookie, so the process mints a per-run agent token and splices it into the prompts'
curl examples. It is never persisted, holds the `pm` role (never `admin`, so it can't
reach the roster or cost data), and grants nothing beyond the endpoints an agent's
Bash allowlist already matched in personal mode.

## External trackers (Jira / Azure DevOps)

Declare one `[[trackers]]` block per connection (see the sample above). With none
declared the whole feature is dormant: no extra threads, no ticket controls on the
board, nothing.

> **Tracker config belongs in your repo, not in this package.** This package is public and
> its own `pm_studio_local/` is the config for developing PM Studio itself, so it declares
> no tracker. Yours names your Jira site, your project keys and — via the synced catalog —
> your ticket titles. That is your organisation's internal structure, and it stays in the
> repo you control.
>
> Two consequences worth internalising before you point this at a real instance:
>
> - `pm_studio_local/config.toml` is **committed**. Use `token_env` and `email_env` so no
>   credential is in it, and remember that `base_url` and `projects` alone disclose which
>   vendor instance you run and how your work is organised. If your repo is public, that is
>   published.
> - A sync writes `<workspace>/trackers.json` — a cache of ticket titles from the synced
>   instance — into whichever checkout the server runs from. It is git-ignored and unstaged
>   from every snapshot, but do not run a sync from a checkout that is not yours to fill
>   with that data.
>
> `tests/test_no_deployment_data.py` enforces both halves of this for the package itself:
> no tracked file may name a real tracker host, the dogfood config may declare no tracker,
> and no synced catalog may exist anywhere in the checkout. To exercise the sync while
> developing, point a throwaway config at a local fake tracker; the suite covers the client
> behaviour with no network at all.

**What a link is.** A roadmap change may be linked to exactly **one** ticket, and a
ticket to exactly one change — 1:1 in both directions. The change stores only
`tracker_id` + `ticket_key`; the ticket's type, title, state and URL come from the
synced catalog, so a sync updates one place instead of rewriting every linked change.
Attempting to link a ticket another change already holds returns **409** naming that
change. Keys are compared case-insensitively for Jira, so `proj-1` cannot sneak in
alongside `PROJ-1`.

**Linking.** On the board, each card gets a *Link ticket…* control opening a picker
over the synced catalog (already-linked tickets are shown but greyed out). You can
also paste a full ticket URL or a bare key. The PM agent can do the same from a
session pinned to that product:

```bash
curl -s -X PATCH http://127.0.0.1:8000/roadmap/<product>/items/<item_id> -H "Content-Type: application/json" -d '{"ticket": "PROJ-123"}'
```

`"ticket": ""` unlinks. A key that does not exist in the tracker is refused with a
**404** rather than stored, so a typo can't sit on a card forever looking like a sync
outage.

**Syncing.** Each tracker is pulled on its own `sync_interval_minutes` by one
background thread, plus **Sync now** in the board header. Jira uses
`/rest/api/3/search/jql` and falls back to the older offset-paginated
`/rest/api/{3,2}/search` for Server/DC; ADO uses WIQL then batched work-item hydration.
Linked tickets that fall outside `projects` are fetched individually, so a one-off
dependency on another team's board still resolves.

**When a tracker is down** its previous catalog is deliberately *kept* — a card showing
last week's type is more useful than one that suddenly claims the ticket is gone. The
failure and its reason appear in the board header, and the tracker keeps retrying. A
link whose ticket isn't in the catalog renders as a dashed **NOT SYNCED** badge rather
than disappearing.

**Ticket types and colour.** Each tracker's own type names are normalised onto a small
vocabulary — epic, feature, story, task, bug, spike, subtask, other — which is what
picks the badge colour, so ADO's *Product Backlog Item* and Jira's *Story* look alike.
The badge always shows the tracker's **own** name for the type, so a team that renamed
Story to Deliverable sees "Deliverable", and an unrecognised custom type gets a neutral
colour rather than a wrong one. Colour is never the only signal.

**Nothing is written back.** The sync is read-only: PM Studio never changes a ticket's
type, state or anything else in Jira or ADO. The board's own bucket/status stays
independent, on purpose.

**Credentials and cached data.** Tokens are read from the environment via `token_env`,
are only ever sent in an `Authorization` header, and are scrubbed from every error
message before it is stored or served — `GET /trackers` cannot return one. The catalog
cache at `<workspace>/trackers.json` holds ticket titles from your tracker, so it is
treated like the other credential-bearing state: gitignored *and* unstaged from every
snapshot unconditionally (see `gitsnapshot.SENSITIVE_WORKSPACE_FILES`). Deleting it
costs one sync.

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
