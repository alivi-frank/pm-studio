# Local PM instructions — the PM Studio repo itself

This repo IS the shared pm-studio package. Sessions here develop the tool that other
projects (home-builder, real-state, ...) consume as a pinned dependency. That gives
you maintainer duties no client deployment has:

- **The running server is the INSTALLED published version, not this repo's source.**
  A code change in `pm_studio/` does nothing to the live server — not even after a
  restart. Changes reach any server (this one included) only through a release:
  version bump, tag, push, and a client-side reinstall. Never tell the stakeholder a
  merged change is "live"; say it ships with the next release.
- **Keep the package deployment-agnostic.** Anything project-specific must be
  expressible through `pm_studio_local/` (config + append-only instruction
  fragments) — never hardcoded. If a feature only makes sense for one client, it
  belongs in that client's local config, not here.
- **Data formats are a compatibility contract.** sessions.json, chat_history.json,
  task records, and roadmap items are read by every client deployment across
  versions. Additive fields with safe defaults only; never rename or repurpose
  existing fields.
- **Release process** (only when the stakeholder asks to ship): bump the version in
  `pyproject.toml` AND `pm_studio/__init__.py`, update pinned-tag references in
  README.md and docs/MIGRATION.md, commit, tag `vX.Y.Z`, push main + tag. Clients
  upgrade by bumping their pinned tag and reinstalling.
- **Do not push to the remote outside that release flow** unless the stakeholder
  explicitly asks - this is a public repo; every push publishes.
