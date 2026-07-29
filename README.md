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

## Security model

In `personal` mode this is a single-trusted-user, local-only tool: everything binds to
127.0.0.1 and dev agents run with bypassed permissions inside your repo. Do not expose
the port. PM agents run under a strict literal-match Bash allowlist that structurally
scopes them to their own session and their own product's board.

`enterprise` mode adds authentication so a shared instance can serve a team: every
request needs a session cookie, credentials are stored as PBKDF2 hashes outside git,
and login/invite tokens are persisted only as hashes. The dev agents still run with
bypassed permissions, so **being able to dispatch one is equivalent to code execution
on the host** — grant the `pm` role accordingly, and treat `admin` as a trusted
operator role. Enterprise mode makes a networked deployment possible; it does not make
exposing the port to an untrusted network a good idea.

## License

MIT — © Frank Romero ([@fromerosk](https://github.com/fromerosk)).
