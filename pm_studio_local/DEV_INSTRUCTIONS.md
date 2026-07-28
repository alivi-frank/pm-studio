# Local dev-agent instructions — the PM Studio repo

- Before considering any task done, run the full suite from the repo root:
  `python3 -m unittest discover -s tests` — and `python3 -m compileall -q pm_studio`.
- Never create runtime/workspace state under `pm_studio/` — that is package source
  shipped to every client. Runtime state for this repo lives under `studio_data/`.
- Never modify `.venv/` contents (it holds the installed published version the
  running server uses).
- Keep changes deployment-agnostic: no client-specific names, paths, ports, or
  products in package code — those belong in each client's `pm_studio_local/`.
- Data-format changes must be additive with safe defaults (old JSON must load
  unchanged); add a test proving legacy records still load whenever you touch a
  persisted shape.
- Do not push to any remote; releases are stakeholder-initiated.
