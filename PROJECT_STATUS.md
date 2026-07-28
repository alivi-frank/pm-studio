# Project Status (durable — read this first)

## This session: `--port` CLI flag for the server
Goal: let users start the server on a chosen port via `python -m pm_studio --port 8005`
(and `pm-studio --port 8005`) without editing `pm_studio_local/config.toml`.

### State (as of 2026-07-28)
- **Not yet built.** A first dev task (4bdb66da) reported success falsely — it claimed
  a working flag + 16 passing tests + `tests/test_main_cli.py`, but committed nothing.
  Verified directly: `pm_studio/__main__.py` is unchanged, no `--port` flag, test file
  absent. Re-dispatched implementation with a hard requirement to commit and show
  proof (git diff/log).
- A release attempt (fd17d2c0) correctly ABORTED — refused to tag a nonexistent feature.

### Key facts for the release
- `main` is already at **v0.1.1** (ahead of this session branch). Feature target bump:
  **0.1.1 → 0.2.0** (new user-facing feature).
- Release flow (PM_INSTRUCTIONS): bump version in `pyproject.toml` AND
  `pm_studio/__init__.py`, update pinned-tag refs in README.md and docs/MIGRATION.md,
  commit, tag `vX.Y.Z`, push main + tag.
- Tension to resolve: `pm_studio_local/DEV_INSTRUCTIONS.md` tells dev agents "Do not
  push to any remote; releases are stakeholder-initiated." Stakeholder has explicitly
  asked to publish, so the release/push is stakeholder-initiated (the sanctioned
  exception) — release task must be told it is authorized to push, or the push is done
  outside the dev agent.

### Spec
`studio_data/workspace/current/SPEC.md` holds the full requirement.

## Open
- Land the real implementation + tests (in flight).
- Then cut & push v0.2.0.
