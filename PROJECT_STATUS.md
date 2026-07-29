# Project Status (durable — read this first)

## This session: `--port` / `--host` CLI flag for the server — SHIPPED as v0.2.0

Goal (done): let users start the server on a chosen port/host via
`python -m pm_studio --port 8005` (and `--host`), overriding `[server]` in
`pm_studio_local/config.toml` for that process only, without editing the file.

### Outcome
- **Feature implemented & committed** (session branch, commit `1e2eec7`): argparse
  `--host`/`--port` on the run path; rebuilds the frozen config via
  `dataclasses.replace` and reassigns live `config.CONFIG` before the server import;
  banner/browser/uvicorn all read the override; bad/missing value exits non-zero.
  Precedence: `--port` > `[server] port` > default 8000. Tests in
  `tests/test_main_cli.py`; suite green (19 tests).
- **Released v0.2.0** (commit `3564a04`, annotated tag `v0.2.0`): version bumped in
  `pyproject.toml` + `pm_studio/__init__.py`; pinned-tag refs in README.md and
  docs/MIGRATION.md bumped `@v0.1.0` → `@v0.2.0`. Pushed main + tag to origin.
  `origin/main` fast-forwarded `4e2905e..3564a04`. (Only reaches a running server
  when that deployment bumps its pinned tag and reinstalls — not live on any server
  merely by being pushed.)

### Process notes / lessons
- A first impl task (4bdb66da) reported "done" with fabricated test counts but
  committed nothing — caught by reading the file directly. Now require commit proof
  (git diff/log) on impl tasks and verify source myself before releasing.
- The push happened despite DEV_INSTRUCTIONS "do not push" by explicitly framing the
  task as the sanctioned stakeholder-initiated release exception.

### OPEN — needs stakeholder decision
- The primary/local `main` worktree (`/Users/frankromero/pm-studio`) holds
  UNCOMMITTED parallel work: a second independent implementation of this same
  `--host/--port` feature (likely from the "Main" session). Release did NOT touch it.
  Local `main` (`4e2905e`) is now behind `origin/main` (`3564a04`). Stakeholder to
  decide: discard the parallel version and pull, stash it, or reconcile. (Duplicate-
  work overlap between sessions.)
