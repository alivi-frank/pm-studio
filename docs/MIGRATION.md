# Migration — from a locally-built `pm_agent/` to the shared PM Studio package

This document is written to be **executed by Claude Code in the target repo**. Paste
it as a message (or say: *"Read docs/MIGRATION.md from the pm-studio repo and do what
it says"*) in a repo that currently runs a locally-built `pm_agent/` module created
from the PM-system reproduction pack.

Outcome: the repo runs the shared, pinned `pm-studio` package with identical
behavior; everything project-specific lives in `pm_studio_local/`; **no data moves
and no data format changes** — sessions, chat history, task records, archives, and
roadmap data stay exactly where they are and keep working.

## Rules (read first, they override everything below)

1. **Never edit the installed package.** `pm-studio` is maintained upstream by its
   sole maintainer; deployments are read-only consumers of pinned versions. If
   something the local `pm_agent/` did cannot be expressed through
   `pm_studio_local/`, do NOT patch the installed package or keep a fork — report it
   to the stakeholder as a candidate upstream feature request and leave that behavior
   behind for now.
2. **Do not touch `pm_agent/workspace/`.** It is the live data (sessions, worktrees,
   chat, tasks, archives, roadmap). The migration removes the local *code* only.
3. **Do not push to any remote** unless the stakeholder explicitly asks.
4. Work on a branch if the repo's conventions call for one; otherwise commit directly
   with clear messages.

## Steps

### 1. Stop the server and snapshot

Make sure the local `pm_agent` server is not running (ask the stakeholder to stop it
if you cannot tell). Then commit any uncommitted work:
`git add -A && git commit -m "Pre-migration snapshot before moving to pm-studio"`
(skip if clean).

If `pm_agent/workspace/sessions.json` shows sessions with status `"merging"`, wait or
retry later — never migrate mid-merge.

### 2. Install the package (pinned)

```bash
pip install "pm-studio @ git+https://github.com/fromerosk/pm-studio@v0.3.1"
```

Verify: `python -m pm_studio version` prints the version. Locate the installed
source for the diff step: `python -c "import pm_studio, pathlib; print(pathlib.Path(pm_studio.__file__).parent)"`.

### 3. Extract the local configuration into `pm_studio_local/`

Create `pm_studio_local/config.toml` in the repo root from what the local module
hardcodes:

- `[project] workspace_root = "pm_agent"` — **required**. This keeps all runtime
  state (and live git worktrees registered under `pm_agent/workspace/sessions/`)
  valid in place. Do not move or rename `pm_agent/workspace/`.
- `[project] default_session_name` — the `"name"` of the `"default"` entry in
  `pm_agent/workspace/sessions.json` (e.g. `"Monolith"`), so the UI and prompts keep
  the familiar name.
- `[project] name` — the project's name.
- `[project] layout` — copy the repo-layout bullet list out of the local
  `pm_agent/agent.py` `PM_SYSTEM_PROMPT_TEMPLATE` (the "`Product source code lives
  in product directories at the REPO ROOT`" block), as a TOML multi-line string.
  Drop any line describing `pm_agent/` itself as the tool's source (the tool is now
  an installed package).
- `[server] port` / `host` — only if the local `pm_agent/__main__.py` was customized
  away from 127.0.0.1:8000.
- `[products]` — copy the `PRODUCTS` dict from the local `pm_agent/roadmap.py`,
  preserving order.
- `[models]` — only if the local `pm_agent/models.py` `MODELS`/`DEFAULT_MODEL` were
  customized; use the reserved `default` key for the default model id.

### 4. Extract local drift into the local instruction files

Diff each local `pm_agent/*.py` against the installed package's corresponding module
(they share ancestry, so diffs are readable). For every local difference decide:

- **Project text** (extra prompt rules, project-specific guidance, custom dev-task
  boilerplate) → move the substance into `pm_studio_local/PM_INSTRUCTIONS.md` (PM
  behavior) or `pm_studio_local/DEV_INSTRUCTIONS.md` (dev-agent behavior). These are
  appended to the shared prompts on every turn/dispatch.
- **Behavioral code changes** → per Rule 1: list them for the stakeholder as
  candidate upstream contributions; do not carry them forward locally.
- Project reference docs the prompts pointed at → consider copying into
  `pm_studio_local/knowledge/` so the PM keeps being pointed at them.

If the local module was never modified after bootstrap, both files can start as
empty stubs (`python -m pm_studio init` scaffolds commented templates without
touching anything that exists).

### 5. Remove the local module code (code only!)

```bash
git rm -r pm_agent/*.py pm_agent/static pm_agent/tests
```

Keep `pm_agent/workspace/` untouched. If `pm_agent/__pycache__/` exists, remove it
too. The directory `pm_agent/` remains as a plain data directory — that is expected
and permanent for migrated systems.

### 6. Update the root documents

In `PROJECT_INDEX.md` (and anywhere else that documents the tooling): the PM/dev
coordination tool is now the installed **pm-studio package** (pinned version, run
with `python -m pm_studio`), configured by `pm_studio_local/`;
`pm_agent/workspace/` remains the runtime-state location. Add a line for
`pm_studio_local/`. `.gitignore` needs no changes — its `pm_agent/workspace/...`
entries still match.

### 7. Verify (acceptance checklist)

Start `python -m pm_studio` from the repo root and confirm every item:

- [ ] Server boots on the configured port; `/` shows the sessions page with the SAME
      sessions as before, same names/titles/statuses, default session included.
- [ ] Opening an existing session's chat shows its full prior history, with dev-task
      cards interleaved.
- [ ] `/roadmap` shows the same products (same order) and the same items.
- [ ] Creating a brand-new session works (worktree created under
      `pm_agent/workspace/sessions/`), its PM responds in chat, and its system prompt
      carried your `PM_INSTRUCTIONS.md` content (ask the PM to confirm a local rule).
- [ ] Dispatching a trivial dev task from the PM works and the PM auto-continues
      when it finishes.
- [ ] `git log` shows snapshot commits appearing as turns/tasks complete.

If any item fails, stop and report the literal failure to the stakeholder — do not
work around it by editing package source.

### 8. Commit

Commit the migration (config + removals + doc updates) with a message like
`Migrate local pm_agent to shared pm-studio package (config in pm_studio_local/)`.

## Upgrading later

Upgrades are always deliberate: bump the pinned tag in the install command and
reinstall, then restart the server. Nothing upgrades implicitly, and session/task/
roadmap data formats remain backward compatible across package versions.
