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
pip install "pm-studio @ git+https://github.com/fromerosk/pm-studio@v0.2.0"
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

## Security model

Single-trusted-user, local-only tool: everything binds to 127.0.0.1 and dev agents
run with bypassed permissions inside your repo. Do not expose the port. PM agents run
under a strict literal-match Bash allowlist that structurally scopes them to their own
session and their own product's board.

## License

MIT — © Frank Romero ([@fromerosk](https://github.com/fromerosk)).
