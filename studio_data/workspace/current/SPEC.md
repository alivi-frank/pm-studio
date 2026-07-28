# SPEC — `--port` CLI flag for the server

## Goal
Let a user start the PM Studio server on a chosen port without editing
`pm_studio_local/config.toml`, e.g.:

    python -m pm_studio --port 8005
    pm-studio --port 8005

## Current behavior
- `pm_studio/__main__.py` dispatches on `sys.argv[1]`: `version`/`--version`,
  `init`, or (no arg) run the server via `_serve()`.
- The port is fixed at load time by `config.py`: `[server] port` in
  `pm_studio_local/config.toml`, defaulting to `DEFAULT_PORT = 8000`.
- `CONFIG` is a frozen dataclass loaded once at import; `server.py` and
  `_serve()` read `CONFIG.port` / `CONFIG.base_url`.

## Requirements
1. Add a `--port <int>` option to the run path of `python -m pm_studio` (and the
   `pm-studio` console script). When present, it overrides the configured port
   for this process only — no file is written.
2. Precedence: `--port` flag > `[server] port` in config.toml > `DEFAULT_PORT`.
3. Apply the override BEFORE the server module is imported. `server.py` binds
   `CONFIG` at import time, and `_serve()`/`base_url` (used for the browser-open
   and the startup print) must all reflect the overridden port. Recommended:
   rebuild via `dataclasses.replace(CONFIG, port=...)` and reassign the
   `pm_studio.config.CONFIG` module attribute, then have `_serve()` read the
   current module attribute rather than a stale local binding.
4. Invalid/missing value (e.g. `--port` with no number, or non-integer) exits
   with a clear error message and non-zero status — do not fall back silently.
5. `--port` must not break the existing `init` / `version` subcommands or the
   plain no-argument run. Optional: also accept `--host` the same way (nice to
   have, only if trivial and consistent).
6. Update the module docstring / usage text in `__main__.py` to document the
   flag.

## Constraints (maintainer rules)
- Package must stay deployment-agnostic; this is a generic CLI affordance, no
  project-specific behavior.
- Additive only — no changes to config.toml schema, data formats, or existing
  field meanings.
- Change reaches any server only via a release; nothing here goes "live" on merge.

## Tests
- Add unit coverage for the argument parsing / override logic (e.g. `--port 8005`
  yields `CONFIG.port == 8005`; bad value exits non-zero; no `--port` keeps the
  configured/default port). Keep the full suite green:
  `python3 -m unittest discover -s tests`.

## Done when
- `python -m pm_studio --port 8005` starts the server on 8005 with the startup
  banner and browser-open URL both showing 8005; tests cover the new logic and
  the suite passes.
