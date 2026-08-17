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

One optional file sits **outside** that directory: `.env` at the repo root, holding the
secrets `config.toml` refers to by name. See [`.env`](#env) below.

## `.env`

`config.toml` is committed, so credentials are named there, never written: `token_env`,
`email_env` and `password_env` hold the **name** of an environment variable. That
variable has to exist in the server process, and at startup PM Studio fills it from an
optional `.env` at your repo root:

```bash
# <repo root>/.env  - git-ignore this file
PM_STUDIO_JIRA_TOKEN=your-api-token
PM_STUDIO_JIRA_EMAIL=pm@acme.com
```

`python -m pm_studio` then picks the token up with no wrapper script and no `export`
first. Worth knowing about it:

- **Absent is normal.** No `.env`, no output, no behaviour change — export the variables
  however you already do (shell profile, CI, systemd, a run script) and nothing here
  applies.
- **The environment always wins.** A variable already set in the process is left exactly
  as it is; `.env` only fills the gaps. A real `export` is a deliberate act for that one
  run, and a stale file must never quietly override it. (Set-but-empty counts as unset,
  matching how every other credential read here treats `""`.)
- **It reports names, never values.** A loaded file prints which variables it supplied,
  so a missing credential is diagnosable from the startup log without putting a token in
  it.
- **The format is small on purpose:** `KEY=value` one per line, `#` comment lines, blank
  lines, an optional `export ` prefix, and quoted values taken literally (`X="  spaced "`
  keeps its spaces). No `$VAR` interpolation, no backslash escapes, no multi-line values
  — a file needing those wants a shell, which is what `export` is for. Lines that are not
  `KEY=value` are skipped with a warning naming the line number, rather than silently
  leaving the variable unset.
- **The whole file is loaded**, not just the keys PM Studio reads, and the dev agents it
  spawns inherit the process environment. If your `.env` also carries unrelated settings
  that would change how a subprocess behaves, keep those elsewhere or export only what
  you want with a run script.

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
# Declaration order is display order (customer-facing first reads best). Omit for
# a single-product repo: sessions then run unpinned, with no per-product boards.
#
# A product with a `parent` is a SUB-PRODUCT of it (see "Hierarchical products"
# below). Both spellings below mean the same thing; TOML wants every plain
# `key = "value"` line to come before the first [products.x] sub-table.
[products]
web = "Web App"
platform = "Platform"
auth = { label = "Auth & Identity", parent = "web" }

[products.billing]
label = "Billing"
parent = "web"

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
# Each sync also pulls the projects' RELEASES (Jira versions; ADO iterations) into
# the same cache - no extra configuration, read back on GET /trackers/releases.
# Data only for now: nothing on the board consumes them yet.
# ALWAYS use `token_env` (the NAME of an environment variable) rather than
# `token` - pm_studio_local/ is normally committed, so an inline token is a token
# in your git history. PM Studio warns loudly if you use `token`. Put the value in
# the git-ignored `.env` at your repo root (see the ".env" section) or export it.
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

## Tracker import routing

A tracker can go beyond syncing a catalog: it can **create changes** from its own
tickets, routed onto products by component.

```toml
[[trackers]]
provider = "jira"
# ... connection keys ...
import_types = ["Epic", "Story", "Bug"]   # which ticket types become changes

[[trackers.routes]]
component = "Checkout Web"   # Jira component / ADO area path
product = "checkout"         # must be a declared product — fatal if not
system = "claims"            # optional; must be one that product touches — fatal if not

[[trackers.routes]]
component = "Search"
product = "search"

[[trackers.routes]]
project = "SHOP"             # the project-IS-the-product shape: every ticket in
product = "checkout"         # SHOP routes here, component or not
```

A route matches by `component`, by `project`, or by both (that component within that
project). **Component routes win over project routes** — the specific claim is the
truer one, so a project-wide default never swallows the exceptions declared beside it.
A `project` route must name a project the tracker actually syncs; anything else is a
dead route and refuses to boot.

On **ADO**, an area path is a tree, so a route on an area claims its **whole subtree**:
`component = "Portal"` covers `Portal\Authorizations\Queue` too, matched on the path
separator so `Portal-Legacy` is never mistaken for a child. Among matching area routes
the deepest one wins, and the shallower route keeps the rest of its subtree. Jira
components are flat labels and always match exactly.

`import_types` and `routes` only work together — declaring exactly one refuses to boot,
because it would be a config that looks like it imports and silently doesn't. With
neither, tracker behavior is exactly what it always was.

Two more knobs bound what the sync takes in:

```toml
since = "2025-01-01"   # only tickets CHANGED on/after this date are synced
```

`since` bounds the **catalog pull itself** — by last activity, not creation, so an old
epic still being worked stays current while abandoned history ages out. Linked tickets
older than the bound remain resolvable (the sync fetches any linked-but-missing ticket
individually). Task-level tickets are never imported as changes of their own; when a
parent ticket imports, its tasks ride along as a snapshot list inside the parent's
description — tasks are how a story gets done, not separate planning material, and a
board of task-changes buries the plan in execution detail. The tracker keeps the live
task list; the badge links straight to it.

After each sync, every catalog ticket whose type is in `import_types` and whose
component matches a route becomes a change on the routed product — `later` bucket,
status mapped from the ticket's state category, linked to its ticket in the same call.
The 1:1 link is the dedupe: re-syncs and restarts never double-import, and a manually
linked ticket is never re-imported. Nothing is ever written back to the tracker.

Tickets resolved as **won't-do** are never imported — not as changes and not as
projects. Jira files "Won't Do" under the Done status category, so without this the
declined work would land on the board as shipped-looking `done` changes. The check
reads the Jira resolution (and a status literally named "Won't Do"), spelled
leniently — apostrophes, hyphens, and case don't matter. Won't-do tickets are counted
in the sync report rather than silently dropped.

A ticket declined **after** it was imported leaves the board too: each sync ends with
a removal pass that deletes imported changes and projects whose ticket has since been
resolved won't-do. Only provably untouched imports are removed — the import stamp
still opening the description, no owner, the change still in `later`, the project
under no initiative and with no changes left beneath it. Anything a human touched
stays and is counted in the sync report instead. A linked ticket absent from the
catalog is unknown, not declined — nothing is removed on absence of evidence.

A ticket with **no matching route is reported, never guessed**: the board's tracker
strip counts unrouted importable tickets and names their components (the fix is a
config line, not an investigation). No catch-all product is invented. A route without
a `system` imports unattributed — into the existing unattributed report — because an
imported change you can see beats a refused one you can't.

When a slice of a tracker is tracked authoritatively **somewhere else** (the classic
case: the same work mirrored in a second tracker), declare it out entirely:

```toml
exclude_components = ["CapAdmin"]   # components / area paths owned elsewhere
```

Excluded tickets are invisible to every automatic pass — never imported as changes,
never imported or linked as projects, never used to assign a change to a project — and
the exclusion covers the **whole parent chain**, so a story or task under an excluded
epic is out too, even though children rarely carry components of their own. Excluded
is not unrouted: unrouted means "route this someday" and is reported as debt, excluded
means "never, it lives elsewhere" and is only counted. A component that is both routed
and excluded refuses to boot — one of the two lines is a leftover. Manual linking
stays possible: exclusion governs what the sync does on its own, never what a human
does on purpose.

## Hierarchical products

A product declared with a `parent` is a **sub-product** of it:

```toml
[products]
web = "Web App"
platform = "Platform"
auth     = { label = "Auth & Identity", parent = "web" }
billing  = { label = "Billing",         parent = "web" }
sso      = { label = "SSO",             parent = "auth" }   # three levels
```

A sub-product is a full product, not a label: its own roadmap board file, its own
pinned sessions, its own id in every `/roadmap/<product>/...` URL. `parent` adds
three things and nothing else:

- **The board nests it.** The Product lens draws one section per top-level product,
  with its own project rows first and a folding sub-section per child inside it, one
  indent step per level. A parent's heading counts its own open work and, separately,
  what is open across its whole subtree — both are true at once, so neither number is
  quietly inflated. Folding a product folds everything under it.
- **A parent's PM owns its whole subtree.** A session pinned to `web` gets `web`,
  `auth`, `sso` and `billing` at full detail in its roadmap block, each under its own
  heading naming its own parent, and may create/update/schedule/triage on all of
  them. A session pinned to `auth` gets `auth` and `sso`; `web` reaches it as a
  one-line digest like any other product. Nothing outside the subtree is writable —
  that stays a suggestion (`origin_product`), enforced by the agent's own URL
  allowlist. A product in the middle is told it is both a parent and a child. (The one
  way a session reaches beyond its pinned subtree is by *adopting* another board, which
  only an initiative-scoped session can do, and only explicitly — see the README's
  "Sessions that work in an initiative".)
- **Names disambiguate.** Wherever a product is named with no section around it, it
  reads as its full path: `Web App / Auth & Identity / SSO`.

**Depth is yours to choose.** Nothing counts levels — a sub-product can have
sub-products of its own, as deep as your org actually is. The practical limit is
width, not the model: the board indents one step per level, so past four or five the
sections get narrow. An unknown or self-referential `parent`, or a **cycle**, is a
hard startup error, for the same reason a bad `[enterprise] mode` is — a child that
silently became a top-level product is a working-looking deployment with one board
too many, and the products in a cycle would hang off no top-level product at all and
vanish from the taxonomy while their boards sat on disk holding items.

Nothing stores the hierarchy: a change records the product id it always recorded.
So **re-parenting is a config edit**, not a migration — move the `parent` pointer,
restart, and the same boards appear in their new place. Deleting a `[products]`
entry does not delete its board file; items on an undeclared product stop loading
and come back if the id returns.

Adding sub-products to an existing deployment changes nothing about the products
already there: their items, boards and sessions stay exactly where they are, and a
parent simply gains children underneath it. The same goes for deepening an existing
tree later — pointing a new product at a product that already has a parent needs no
migration either.

## Product metadata

A product entry may carry facts about the product alongside its structure:

```toml
[products.checkout]
label = "Checkout"
parent = "web"
description = "Guest and member checkout flows, up to payment capture"
owner = "jane.doe"            # product decision-maker — free text
team = "Payments Engineering" # who builds it — free text
stage = "ga"                  # discovery | development | ga | sunset
```

All four are optional, and all four are **context, not policy**: a pinned PM's prompt
gains one line naming them (so it raises product decisions with the owner by name
instead of guessing), the board badges a product whose `stage` is not `ga`, and the
owner/team/description ride the section heading's tooltip. Nothing authorizes, blocks,
or bills against them — costing has its own roster, and roles live in `[enterprise]`.

`stage` is a closed vocabulary and an unknown value refuses to boot, the same contract
as `[enterprise] mode`: a typo must not silently mean the steady-state default. The
free-text fields are not validated beyond being strings.

Metadata lives here rather than in a store or UI deliberately. It is the same kind of
slowly-changing, operator-owned fact as the taxonomy itself, and it changes by config
edit — versioned by your own repo, where accountability for "who owns this product"
belongs.

## Systems

A **system** is a bounded piece of technology — a service, an app, a module — and it is
the unit a change is contained within. **A change belongs to exactly one system.** That
is what makes its blast radius knowable.

A **product** is the business-facing thing: a line of business, or an umbrella over the
technology that serves one. The two are not the same shape and neither contains the
other:

| | Product | System |
|---|---|---|
| What it is | a business-facing offering | a bounded piece of code |
| Owns a roadmap board | **yes** | no, never |
| A change relates to | exactly one (its board) | exactly one (its attribution) |
| Relationship | touches many systems | serves many products |

```toml
[products]
web = "Web App"

[products.checkout]
label   = "Checkout"
systems = ["claims", "rides"]     # which systems this product is built on

[systems]
claims = "Claims Processor"

[systems.rides]
label    = "Rides & Logistics"
path     = "services/rides"             # source folder, repo-root-relative
repo     = "github.com/org/rides"       # its own repo, when it has one
guidance = "docs/rides/GUIDANCE.md"     # declared now, acted on later
gitflow  = "docs/rides/GITFLOW.md"      # non-negotiable git workflow rules - LIVE
pipelines = ["rides-ci"]                # declared now, acted on later
```

`path`, `repo`, `guidance` and `pipelines` are all optional and, today, purely
descriptive: they are shown on the Systems page and named in the PM's prompt (so it
knows where a change's code lives before dispatching a dev task), but nothing enforces
or triggers them yet.

`gitflow` is optional too, but it is **acted on**. It points (repo-root-relative) at a
file of that system's non-negotiable git workflow rules — branching model, PR targets,
commit conventions — and the file must exist, or the server refuses to start. Two
things then happen on every dev task dispatched for the system:

- **Delivery.** The file's contents are appended to the dev agent's prompt verbatim at
  dispatch time, after (and explicitly overriding) `DEV_INSTRUCTIONS.md`, read fresh
  from the dispatching session's own worktree — an edit to the rules applies to the
  very next task, and a task whose rules file cannot be read is refused before any
  agent spend rather than run without them. The PM is told the rules travel
  automatically and never to restate or paraphrase them in a task description, so the
  only copy that reaches a dev agent is the declared one.
- **Verification.** After the task finishes, an independent compliance judge — a
  read-only agent that inspects the task's exact commit range and never sees the dev
  agent's own claims — rules `pass` / `violation` / `inconclusive` against the file.
  The verdict lands on the task record (and its card), and the PM's auto-continue turn
  is told about violations with instructions to dispatch a remediation task. A judge
  that fails is a visible `inconclusive`, never a silent pass.

Once `[systems]` is declared, every dev-task dispatch must name the one system whose
code it changes (`"system"` in the POST /tasks payload) — that attribution is what
routes the rules, so it is required deployment-wide, unlike roadmap attribution below.
Prompt injection guarantees delivery, not obedience: branch protection and CI in each
repo remain the hard enforcement floor, and the judge is how the studio tells you when
they're about to be tested.

**The edge is many-to-many, so systems totals never sum across products.** A system
touched by three products would be counted three times. This is the same overlap rule
goals already carry — see `docs/ARCHITECTURE.md`.

**Systems have no roadmap, deliberately.** Roadmaps are product-first and
initiative-first. Work that belongs to a system rather than to any product — infra,
performance, an upgrade — is an **initiative**, which is exactly what initiatives are
for. Grouping a system's own changes is a filter on the board, not a board of its own.

### Turning it on, and what changes

**Declaring `[systems]` shows the tab; declaring a product's `systems = [...]` is what
puts that product in scope.** With no `[systems]` table at all, a deployment behaves
exactly as it did before this feature existed: no attribution, no Systems tab, nothing
new on the board.

Attribution is then scoped **per product**, not per deployment — which is what makes the
layer adoptable one product at a time. A product that declares no systems requires
nothing, and its board looks exactly as it did before. Bring products in scope as you
work out their systems; the Systems page lists the ones still outside.

That scoping is deliberate, not laziness. A deployment with two dozen products and
thousands of existing changes would otherwise, the moment its first system was declared,
require every board to attribute to whichever systems happened to exist yet — which is
worse than not attributing at all, because it manufactures wrong data instead of leaving
data missing.

For a product that **is** in scope:

- Every **new** change must name its system — from the board's create form, or
  `"system": "<id>"` in a PM's `POST`. A create without one is a 400 listing the valid
  ids.
- Changes that predate it carry no system. That is an **inconsistency to close, not a
  supported state**: the board shows a `no system` chip and a banner, the PM's context
  block marks each one `[NO SYSTEM]`, and the Systems page lists them with a control to
  attribute each. It is never a hard block — refusing to load a board would be worse
  than showing the work left to do.
- A change's system can be corrected, never removed: `"system": ""` is refused rather
  than treated as a reset.

An **out-of-scope** product (no `systems` declared) requires nothing, and its system-less
changes are counted separately from the debt above — they measure missing *config*, not
owed attribution, and no amount of attributing would bring that number down. Such a
product will still *accept* an explicit system if you give it one: permissive rather than
refused, since attributing a change while its product's edge is undeclared is useful and
harmless.

### Reclassifying a product that was really a system

If an id in `[products]` turns out to be a system, **an id may be declared in both
tables at once** — that is the explicit, temporary reclassification state, not an error:

```toml
[products]
claims = "Claims Processor"   # still here, so its board keeps loading

[systems]
claims = "Claims Processor"   # and now also a system
```

Its product board stays live and fully usable, and the Systems page flags it
`reclassifying` and offers each of its changes a "re-home to product X, attribute to
system Y" control (one `move_to_product` + `system` PATCH). When its board is empty,
delete the `[products]` entry.

Do it in that order. Deleting the `[products]` entry first would orphan the board:
board files are loaded only for **declared product ids**, so its items would silently
stop loading — the same behavior described for any deleted product above, which is
harmless for an empty board and data loss for a full one.

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
>   credential is in it — put the values in the root `.env`, which is not — and remember
>   that `base_url` and `projects` alone disclose which
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

**Credentials and cached data.** Tokens are read from the environment via `token_env`
(filled from the git-ignored root `.env` if you keep it there), are only ever sent in an
`Authorization` header, and are scrubbed from every error
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
