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
- **This repo IS the shared pm-studio package** (github.com/fromerosk/pm-studio),
  consumed as a pinned dependency by client projects (home-builder, real-state, ...).
  The server coordinating THIS repo runs the installed published version in `.venv`
  (start with `.venv/bin/pm-studio`) - local source changes ship only via a release.
  Read pm_studio_local/PM_INSTRUCTIONS.md for the maintainer rules.
- **[pm_studio/](pm_studio/)** - the package source (the `package` product): server,
  agent, tasks, sessions, roadmap, config, scaffold, static UI pages.
- **[tests/](tests/)** - unittest suite: `python3 -m unittest discover -s tests`.
- **[docs/](docs/)** - the `docs` product: [ARCHITECTURE.md](docs/ARCHITECTURE.md)
  (full technical spec), [WAY_OF_WORKING.md](docs/WAY_OF_WORKING.md) (methodology),
  [CONFIGURATION.md](docs/CONFIGURATION.md) (pm_studio_local reference),
  [MIGRATION.md](docs/MIGRATION.md) (migrate a locally-built pm_agent).
- **[pm_studio_local/](pm_studio_local/)** - PM Studio deployment config and local
  instructions for this repo (dogfood: workspace_root=studio_data, port 8002).

## Live working docs (reset between phases - may be blank right now)
- **[studio_data/workspace/current/SPEC.md](studio_data/workspace/current/SPEC.md)** - the
  spec for what's being actively built right now. Absence does not mean no history -
  check PROJECT_STATUS.md first.

## Product roadmap board (live, structured - not a markdown doc)
- Board at `/roadmap` on the running PM Studio server; data in
  `studio_data/workspace/roadmap/<product>.json` (server-owned, not git-tracked).
