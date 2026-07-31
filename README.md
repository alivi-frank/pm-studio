# PM Studio

A local PM/dev-coordination studio driven by the headless Claude Code CLI. You (the
**stakeholder**) talk to a **PM agent** through a local web chat; the PM turns your
goals into a live spec, dispatches **background dev agents** into your repo, is
auto-re-invoked the instant each dev task finishes so it can verify and dispatch the
next slice without being asked, and only surfaces to you for genuine product
decisions. Multiple PM sessions run in parallel, each in its own git worktree/branch,
with cross-session overlap detection, a shared product roadmap board (including work
owned by other people/teams, tracked but never dispatched), and AI-assisted
merge-back into main as the only way a session ends.

Every PM turn and dev task ends in a git snapshot commit. There is no database —
state is JSON files plus git.

## Requirements

- Claude Code CLI installed and authenticated (`claude` on PATH;
  `claude -p "hi" --output-format json` must work)
- Python 3.11+
- `git`; the target project directory is (or can become) a git repository

## Install

Install a **pinned tag** into the environment of the project you want to run it in:

```bash
pip install "pm-studio @ git+https://github.com/fromerosk/pm-studio@v0.3.1"
```

## Use

From your project's repo root:

```bash
python -m pm_studio init   # once: scaffolds pm_studio_local/ + seed docs + .gitignore block
```

Fill in `pm_studio_local/config.toml` (project name, products, repo layout), then:

```bash
python -m pm_studio
```

and open http://127.0.0.1:8000 — the sessions page. Create a session, chat with the
PM, and watch it dispatch dev tasks.

## Customizing for your project (the only supported way)

All project-specific behavior lives in **your repo**, under `pm_studio_local/` — see
[docs/CONFIGURATION.md](docs/CONFIGURATION.md):

- `config.toml` — project identity: name, products, repo layout, port, workspace
  root, optional model allow-list.
- `PM_INSTRUCTIONS.md` — appended to every PM system prompt (enterprise
  restrictions, private domain knowledge, standing decisions).
- `DEV_INSTRUCTIONS.md` — appended to every dev-agent dispatch (build/test
  conventions, hard restrictions).
- `knowledge/` — private reference docs the PM is pointed at.

Local content is **append-only**: it extends the shared prompts and can never replace
the core loop. That is deliberate — it is what keeps the PM Studio experience
identical across every system running it, and it means your private knowledge and
restrictions stay in your (possibly private) repo, never in this public package.

**Do not edit the installed package.** PM Studio is maintained upstream only
(single maintainer); deployments consume pinned versions and never patch package
source. If `pm_studio_local/` can't express what you need, open an issue on this
repo instead.

## Work model (optional)

Roadmap items can optionally hang off a strategy chain, visible at `/portfolio`:

```
Goals  ⇄  Initiative  →  Project  →  Change
                                       └─ belongs to exactly one Product
```

A **Change** is just the roadmap item you already have — it gains one parent project.
A project belongs to exactly one initiative, and an initiative may serve **several
goals**. Products aren't a level in the chain; a product hangs off the change, which is
what lets an initiative span several products without any extra bookkeeping.

Products have a hierarchy of their own, separate from the chain above: a product can
declare a `parent`, making it a **sub-product**, nested as deep as your org actually is.
A sub-product is a full product — its own board, sessions and ids — and a PM sees and
writes its whole subtree, while the board nests each child inside its parent's section.
See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#hierarchical-products).

A project with no initiative is **unaligned** — allowed, so nobody is blocked
mid-work, but reported so it gets linked up. Declare a maintenance goal + always-open
initiative + catch-all project once (the page prompts you, with names you choose), and
from then on unplanned work like a bug fix rolls up somewhere instead of floating.

The roadmap board reads the same data through either lens — **by product** (Now / Next
/ Later per product) or **by initiative** (initiative → project → change). The second
lens is what shows you that one initiative spans several products, which the
per-product board structurally can't. Switching never hides anything: work with no
project, or a project with no initiative, is collected under an "Unaligned" heading
rather than dropping out of view.

Entirely optional and entirely additive: existing boards load unchanged, and a
deployment that ignores `/portfolio` behaves exactly as before.

### Sessions that work in an initiative

A session can be pinned to a product, scoped to an **initiative**, or both. The second is
for enterprise work that genuinely spans several integrated products, where pinning one
board would be a lie about the scope.

Such a session reads its whole initiative every turn — its projects and their changes,
across every board they sit on — but starts able to **write** nowhere. Which products an
initiative actually touches is something the PM works out as the conversation goes; when
it establishes that one is affected, it **adopts** that board, says so, and gains write
access to it from the next turn. Adopting a parent adopts its sub-products, same as
pinning always has.

The two axes stay separate on purpose: the initiative is what the session is *about* and
where its cost lands, the products are what it may *change*. Its turns are attributed to
that initiative from the very first one, not to maintenance — and a session whose scope
drifts (initiative closed, deleted, or disagreeing with its project) is reported on
`/portfolio` rather than silently making a cost figure mean something else.

## Migrating an existing locally-built pm_agent

Systems that built the earlier `pm_agent/` module from the reproduction pack can move
to this package with **zero data migration** — sessions, chat history, tasks, and
roadmap data keep their formats and stay where they are. Follow
[docs/MIGRATION.md](docs/MIGRATION.md): it is written as a prompt you paste into
Claude Code in the target repo.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — complete technical spec: components,
  data model, endpoints, concurrency, session lifecycle, merge/sync/terminate flows.
- [docs/WAY_OF_WORKING.md](docs/WAY_OF_WORKING.md) — the operating methodology:
  document taxonomy, vertical slicing, dispatch rules, multi-session coordination,
  git discipline.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — the `pm_studio_local/` reference.
- [docs/MIGRATION.md](docs/MIGRATION.md) — migrate a locally-built `pm_agent/` to
  this package.

## Operating modes

PM Studio runs in one of two modes, set by `[enterprise]` in
`pm_studio_local/config.toml` (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md)):

- **`personal`** (default) — the tool described above: one trusted user, no accounts,
  no login. Omitting the setting entirely keeps this behavior, so upgrading the
  package never suddenly asks an existing deployment for a password.
- **`enterprise`** — adds user accounts, email invites and roles
  (`admin`/`pm`/`reviewer`/`viewer`) on top of the identical core loop. The first
  visit creates the owner account; admins invite the rest from `/people`. Invites are
  mailed when `[smtp]` is configured and always produce a copyable link, so no mail
  server is required. Converting from personal to enterprise migrates no data.

  Reads are open to every role — the whole roadmap is visible to everyone, by design.
  Roles restrict what you can *do*: only `admin` and `pm` may work sessions, change
  the roadmap, or dispatch dev agents; only `admin` manages people or sees cost.
  Consequential actions are recorded in an audit log with the actor who performed them.

## Time & cost (optional, admin-only)

At `/costing`, a week looks like this:

> Dana, 2026-W31: 28.4h Signup rewrite, 11.6h Billing — 40h total

It is a **distribution, not a stopwatch**. Nothing times anybody's screen. Each person's
declared capacity is split across projects in proportion to the activity they generated,
which is why the total is always a real week — capacity is the input and signals only
decide the proportions. Any week can be overridden by an admin, and the derived figures
are kept alongside, so an approximation is never the only record.

**Labour and agent cost are kept apart.** Labour is an estimate (hours × individual or
blended rate). Agent cost is measured from the model's own reported token spend. An agent
running for twenty minutes while nobody is at the keyboard is machine time, not somebody's
afternoon.

Cost is additive up to **initiative**; because an initiative can serve several goals,
goal-level figures overlap and are never summed. Rates are your deployment's own data —
they live in your workspace, never in this package.

## Security model

In `personal` mode this is a single-trusted-user, local-only tool: everything binds to
127.0.0.1 and dev agents run with bypassed permissions inside your repo. Do not expose
the port. PM agents run under a strict literal-match Bash allowlist that structurally
scopes them to their own session and to the product boards they own — their own, plus
their sub-products' if they are pinned to a parent, plus any board an initiative-scoped
session has explicitly adopted, and nothing else. Adoption widens that allowlist, which is
why it is an explicit call a PM makes for its own session and never something inferred
from the conversation.

Credential-bearing state (`accounts.json`, `costing.json`, `audit.jsonl`,
`activity.jsonl`) is **unstaged from every snapshot commit unconditionally**, not merely
git-ignored. Each PM turn ends in a repo-wide `git add -A`, so relying on an operator's
`.gitignore` being right would make a password hash one stale config line away from being
pushed. `init` also keeps those ignore entries up to date, appending any that a
previously-initialized repo is missing.

`enterprise` mode adds authentication so a shared instance can serve a team: every
request needs a session cookie, credentials are stored as PBKDF2 hashes outside git,
and login/invite tokens are persisted only as hashes. The dev agents still run with
bypassed permissions, so **being able to dispatch one is equivalent to code execution
on the host** — grant the `pm` role accordingly, and treat `admin` as a trusted
operator role. Enterprise mode makes a networked deployment possible; it does not make
exposing the port to an untrusted network a good idea.

## License

MIT — © Frank Romero ([@fromerosk](https://github.com/fromerosk)).
