# Architecture Specification

Complete technical spec of PM Studio (~6,100 lines of Python + 10 static HTML pages):
the package `pm_studio/` with modules `config.py`, `models.py`, `gitsnapshot.py`,
`roadmap.py`, `portfolio.py`, `tasks.py`, `agent.py`, `sessions.py`, `accounts.py`,
`authz.py`, `costing.py`, `mailer.py`, `server.py`, `scaffold.py`, `__main__.py`, and
`static/` (10 pages). Dependencies: `fastapi`, `uvicorn`, the `claude` CLI on PATH,
`git` — no database, and no third-party crypto or ORM: everything is JSON files + git,
with password hashing and tokens from the standard library.

The enterprise layer (`accounts.py`, `authz.py`, `mailer.py`, `costing.py`) is inert
unless `[enterprise] mode = "enterprise"` is set, and the work model (`portfolio.py`)
is additive — a deployment that ignores both behaves exactly as the sections below
describe.

Deployment model: the package is installed (pinned) into any git repo — the "target
repo" — and run from that repo's root. Everything project-specific lives in the target
repo under `pm_studio_local/` (see CONFIGURATION.md); `<workspace_root>` below is the
config's `workspace_root` (default `pm_studio`, i.e. runtime state under
`pm_studio/workspace/` in the target repo). The package itself is identical for every
deployment and is never edited locally.

## -1. `config.py` — per-deployment configuration

`load_config(repo_root=cwd)` reads `pm_studio_local/config.toml` plus the local
instruction fragments into a frozen `Config` (see CONFIGURATION.md for the file
format). Loaded once at import as `config.CONFIG` — one process serves one repo.
Everything below that used to be hardcoded (products, port, workspace paths, repo
layout text, default session name, model list) resolves through it. Local prompt
fragments are **append-only**: they are added after the shared prompts and can never
replace them.

## 0. Core concepts

- **Stakeholder** — the one human. Talks to PMs via web chat; owns product decisions.
- **PM session** — one persistent PM conversation bound to one git checkout. The
  **default session** (id `"default"`) is pinned to the primary checkout on `main`.
  Every other session gets its own **git worktree + branch** (`session/<8hex>`) off
  main, created at `<workspace_root>/workspace/sessions/<8hex>`.
- **PM agent** — a resumable headless Claude conversation (`claude -p ... --resume`)
  with a restrictive tool allowlist. It plans, writes specs/docs, and dispatches dev
  tasks over HTTP (curl) to the server it is itself hosted by.
- **Dev task** — a fire-and-forget headless Claude run with full permissions
  (`--permission-mode bypassPermissions`), cwd = the session's worktree root. It builds
  product code. Its completion auto-re-invokes the PM.
- **Workspace** — `<workspace_root>/workspace/` inside each checkout: PM bookkeeping only
  (`current/` live spec+chat+tasks, `archive/`, `sessions/` worktrees, `sessions.json`
  registry, `roadmap/` board data). Product code never goes there.

## 1. `models.py` — model allow-list

```python
MODELS: dict[str, str] = { "<model-id>": "<Label>", ... }   # e.g. opus/sonnet/haiku tiers
DEFAULT_MODEL = "<strongest model id>"
def validate_model(model) -> str   # raises ValueError on unknown
def list_models() -> list[{"id","label"}]
```
Single source of truth shared by session validation/persistence, the live runtime, and
`GET /models`. Default the PM to the strongest allowed model; dev tasks use the
session's model too. (Reference used Opus as default with Sonnet/Haiku selectable.)

## 2. `gitsnapshot.py` — snapshot commits

```python
ARCHIVE_PATH = "<workspace_root>/workspace/archive"
def snapshot(message: str, repo_root: Path) -> None
```
`git add -A`; if `git diff --cached --quiet` says nothing staged, return; else
`git commit -m <message>`. **Never raises** — print a stderr note and continue; a git
hiccup must never break a PM turn or dev task. Snapshots are repo-wide; `.gitignore`
(see SAMPLES.md) is what keeps runtime state out of them.

## 3. `roadmap.py` — product roadmap store

- `PRODUCTS: dict[str, str]` — ordered product taxonomy (ids → display labels),
  customer-facing products first. **Project-specific — from the `[products]` table in
  `pm_studio_local/config.toml`** (TOML order = display order).
- `RoadmapItem` dataclass: `id` (8-hex), `product`, `title`, `description`,
  `bucket` ("now"|"next"|"later"), `status` ("pending"|"in_progress"|"done"),
  `origin_product`, `triaged: bool`, `created_at`, `updated_at`,
  `shipped_at: float|None` (set when status flips to done, cleared otherwise),
  `owner: str|None` — free-text name of the person/team doing the work when it is
  **external** (not built through this system's dev agents). Additive with default
  None so pre-owner JSON loads unchanged. An owned item is tracked on the board and
  flagged to PMs as `[EXTERNAL - owned by <owner>: track it, never dispatch dev work
  for it]` in the deep context (and `(external: <owner>)` in the shallow digest);
  PATCH `owner: ""` clears it back to built-here, `owner: null`/absent = no change.
- `RoadmapStore`: **server-owned** state — one JSON file per product under
  `<workspace_root>/workspace/roadmap/`, git-ignored, read/written ONLY by the always-running
  server process, never per-worktree. Rationale: every session is its own worktree and
  would otherwise see a stale copy; routing all reads/writes through one in-process
  store (reached from PMs via `curl`) means every session sees the same board.
  Thread-lock around mutations; subscriber/broadcast pattern for websocket fan-out.
- **Triage semantics (critical):** `triaged` is *derived on create*, never taken from
  caller input: `triaged = (origin_product == product)`. A cross-product suggestion
  always lands untriaged, whatever the payload says — a PM cannot stamp its own
  suggestion as pre-accepted on someone else's board.
- `move(item_id, to_product, triaged=False, ...)` — reassigns in place, keeping
  id/created_at. Sets `origin_product` to the product it moved FROM; lands untriaged by
  default (PM-initiated moves still need the destination's review; the board UI passes
  `triaged=True` for stakeholder-initiated moves). Emits **two** events — a delete on
  the old product and an upsert on the new — so clients need no new message type.
- Context builders for PM turn injection:
  - `describe_own_product(product)` — full depth, open items only (status != done),
    with `[UNTRIAGED suggestion from <origin> - accept or drop it]` flags.
  - `describe_other_products(exclude)` — one line per product:
    `- <Label>: [bucket/status] Title; [bucket/status] Title; ...`, open items only.

## 3b. `portfolio.py` — the work model above the roadmap

Optional layer that gives roadmap items a strategy chain. A deployment that ignores it
behaves exactly as before: every field is additive and defaults to "unset".

```
Goals  ⇄  Initiative  →  Project  →  Change
                                       └─ belongs to exactly ONE Product
```

- A **Change** is the existing `RoadmapItem` — no new concept. It gains a single
  `project_id: str|None`, additive with default None so pre-existing boards load
  unchanged.
- A **Project** belongs to exactly one Initiative. `initiative_id = None` is the
  **unaligned** state: permitted (nobody is blocked mid-work) but reported.
- An **Initiative** has many Projects and may serve **several Goals** — the only
  many-to-many relationship in the model.
- **Products are not a level.** Product hangs off the Change. That is what makes the
  cross-cutting relationships free: an initiative spans products because its projects'
  changes each carry their own product, and a product appears in many initiatives for
  the same reason. No association records exist or are needed.
- Goals, Initiatives and Projects each carry a `status` of `"open"|"closed"` plus a
  `closed_at` stamp. Products are persistent (they come from config); everything in
  the chain is temporary.

### Why single-parent below the initiative

It makes cost attribution unambiguous. `Change → Project → Initiative` is a unique
path, so those totals are exact and additive. **The additive tree stops at
Initiative**: because an initiative can serve several goals, the same spend
legitimately contributes to more than one goal, so goal-level figures overlap and must
never be summed into a grand total. The API reflects this — `rollup_path(project_id)`
returns only project + initiative, and `goal_ids_for_initiative()` is a deliberately
separate call so goals cannot be folded in by accident.

### The maintenance scaffold

Two rules interact: goals and initiatives are never auto-created, but the catch-all
project needs a parent. So `ensure_maintenance_scaffold()` (exposed as
`POST /portfolio/bootstrap`, and prompted for on the portfolio page when no catch-all
exists) declares the trio once, explicitly, with deployment-chosen names. From then on
a change created with no `project_id` lands in the catch-all instead of floating. The
catch-all project and its maintenance initiative cannot be closed or deleted.

`PortfolioStore` mirrors `RoadmapStore`'s conventions: one server-owned JSON file at
`<workspace_root>/workspace/portfolio.json`, single lock around mutations, subscriber
fan-out. Its events ride the roadmap websocket, since anything watching the board
cares when a project is re-parented. Writes go through a temp file and `replace()`.

### The two pivots

One dataset, two lenses:

- **By product** — `GET /roadmap/data`, the original shape, unchanged. Now also carries
  an additive `portfolio` block (initiatives, projects, catch-all id) so the board page
  can re-group locally as websocket events arrive instead of refetching on every change
  made anywhere.
- **By initiative** — `GET /roadmap/by-initiative`, returning
  `groups: [{initiative, projects: [{project, changes}]}]`, produced by
  `PortfolioStore.group_changes_by_initiative(changes)`. The changes are passed *in* so
  the function is pure with respect to the roadmap store and testable on its own.

The invariant is that switching lens never loses or duplicates a change. The
per-product board can always show everything, since every change has a product; the
initiative lens has two ways to fall off the tree, and both are caught:

- a project with `initiative_id = None` → appears under a group with `initiative: null`
- a change with `project_id = None`, **or pointing at a deleted project** → appears
  under that same group with `project: null`

Both land in **one** trailing group, not two, so the UI renders a single "Unaligned"
heading. An initiative with no changes still appears — "nothing is happening on this"
is information, not a reason to hide it.

The board page mirrors this grouping in JS for live updates; `portfolio_changed` events
trigger a refetch, since a re-parented project changes how the board groups without
changing any item.

### Referential guards

Deleting is refused rather than silently cascading: a goal an initiative still serves,
an initiative that still has projects, a project that still has changes. The change
count is passed *in* by the server (`roadmap_store.count_by_project`) so this module
never reaches across into the roadmap store.

## 3c. `trackers.py` — external issue trackers (Jira / Azure DevOps)

Owns everything *about* a ticket; `roadmap.py` owns only the link to it.

**The 1:1 link.** A `RoadmapItem` carries `tracker_id` + `ticket_key`, both defaulting to
`None` so pre-existing roadmap JSON loads with no migration. One item holds at most one
ticket by construction; the other direction is enforced in `RoadmapStore.link_ticket`,
which scans for a conflicting holder and raises `TicketAlreadyLinked` (→ HTTP 409) naming
it. The check and the write happen under **one** lock hold, so two concurrent links to the
same ticket cannot both pass. Keys are compared through `normalize_key` (Jira upper-cased,
ADO ids left alone) — imported by `roadmap.py` rather than reimplemented, because the
guarantee is only as good as the definition of "same ticket".

**Why the type is not denormalised onto the item.** The type is the tracker's fact. Copying
it would make every sync rewrite every linked item, and one missed write would leave a card
claiming "Bug" for something converted to a Story months ago. So the item stores the
reference, this store holds one entry per ticket, and `server.py`'s `_with_ticket` joins
them at read time — including on websocket events, or a live edit would blank a card's badge
until the next reload.

**Type normalisation.** Tracker type names map onto `CANONICAL_TYPES` (epic, feature, story,
task, bug, spike, subtask, other), which is what picks the badge colour — so ADO's *Product
Backlog Item* and Jira's *Story* colour alike. `raw_type` keeps whatever the tracker said and
is what the UI actually labels, so a renamed or custom type is never relabelled into a lie;
an unrecognised one lands on `other` (neutral colour) rather than being guessed at. The
palette itself lives in roadmap.html, keyed on these slugs, where light/dark are expressible.

**Three properties worth preserving:**

- **The network is one seam.** Every HTTP call goes through the module-level `_request`,
  which the tests replace wholesale — the suite pins the exact URLs, methods, auth headers
  and pagination we send without anyone needing a live Jira.
- **A sync never raises at the caller.** Each tracker's outcome lands in its own
  `SyncStatus`. On failure the previous catalog is deliberately **kept** and the error is
  surfaced in the board header: a stale type beats a card that suddenly claims no ticket.
  Due-ness keys off the last *attempt*, so a failing tracker keeps retrying.
- **Tokens never leave the module.** They go into an `Authorization` header and nowhere
  else. `_scrub` is applied to every error before it is stored, and `describe()` builds its
  payload field by field (never `asdict`) so adding a config field can't leak one.

**Provider shapes.** Jira tries `/rest/api/3/search/jql` (token-paginated, current Cloud)
and falls back to `/rest/api/{3,2}/search` (offset-paginated) on 404/410 only — any other
status surfaces rather than walking the chain. ADO needs two calls by design: WIQL for ids
(single quotes doubled, so a project named `Bob's Team` can't break the literal), then
hydration in batches of 200, a server limit rather than a preference.

**Sync trigger.** One daemon thread started from `lifespan` *only when a tracker is
configured*, waking each minute and pulling whichever trackers are due on their own
`sync_interval_minutes`; plus `POST /trackers/sync`. After a catalog pull, linked tickets
outside the configured `projects` are fetched individually, so a one-off dependency on
another team's board resolves instead of rendering unresolved forever.

**Read-only.** Nothing writes back to Jira or ADO. The board's bucket/status stays
independent of the ticket's state, on purpose, and the PM prompt says so explicitly.

The cache at `workspace/trackers.json` is in `SENSITIVE_WORKSPACE_FILES`: it holds no
credential of ours but caches another system's ticket titles, and never committing it costs
exactly one re-sync.

## 4. `tasks.py` — dev-task registry (one per session)

```python
DEV_AGENT_TIMEOUT_SECONDS = 1800
class TaskRegistry:
    def __init__(session_id, workspace_dir, repo_root, git_lock, model=DEFAULT_MODEL)
```
- Task records are JSON files at `<workspace_dir>/tasks/<id>.json`:
  `{id: 8hex, kind: "dev"|"merge_resolution", description, status:
  "running"|"done"|"error", started_at, finished_at, result}`.
- `start_task(description)` — writes a `running` record, spawns a **daemon thread**,
  returns immediately. The thread runs `_execute`, writes the final record, then (under
  `git_lock`) snapshots: `Dev task <id> (<status>): <description[:72]>`.
- `_execute(description)` runs:
  `claude -p <description> --output-format json --permission-mode bypassPermissions
  --model <self.model>` with `cwd=repo_root` (the worktree root — product sources live
  there; historically cwd=workspace_dir made agents create code inside the bookkeeping
  folder). Timeout 1800 s. Parse stdout JSON; `result = data["result"]`;
  `is_error = data.get("is_error") or returncode != 0`. Handle timeout /
  CLI-not-found / empty output / non-JSON output as error results with the literal
  evidence (stderr excerpt), never a guess.
- `run_conflict_resolution(description)` — same `_execute`, but **synchronous**, id
  prefixed `merge-`, `kind="merge_resolution"`, and **no snapshot commit** (the merge
  flow verifies and commits or aborts explicitly).
- `_reconcile_stale_tasks()` on construction (i.e. server startup): any record still
  `running` on disk cannot actually be running (its thread died with the old process) —
  flip to `error`, `finished_at=now`, result = "Interrupted: the PM Studio server
  restarted while this task was still running… If this was a merge-conflict resolution,
  check `git status` by hand."
- Subscriber pattern: `subscribe(cb)`; every `_write_task` notifies. The server wires
  this to websocket broadcast + PM auto-continue.
- `model` is a plain attribute read fresh per dispatch, so live model changes apply to
  the next task.

## 5. `agent.py` — the PM agent

Constants: `PM_TIMEOUT_SECONDS = 1800`, `MAX_AUTO_CONTINUE_STREAK = 6`,
`MAX_ATTACHMENTS_PER_TURN = 6`, `MAX_ATTACHMENT_BYTES = 10 MB`, image mime→extension map
(png/jpeg/gif/webp).

### Paths (per session)
- `repo_root` = the session's worktree path.
- `workspace_dir` = `<repo_root>/<workspace_root>/workspace/current/`; inside it: `SPEC.md`,
  `chat_history.json`, `pm_session_id.txt` (the resumable Claude CLI session id —
  distinct from the app-level session id), `uploads/`.
- `archive_dir` = `<repo_root>/<workspace_root>/workspace/archive/<session_id>/`.
- `PROJECT_STATUS.md` and `PROJECT_INDEX.md` at the **repo root** — deliberately outside
  `workspace/current/` so a reset can never wipe them; root-level so the stakeholder
  finds them without digging.

### System prompt & allowlist
Built per session from the templates in PROMPTS.md, with these session-scoped URLs baked
in: `tasks_base_url = http://<host>:<port>/tasks/<session_id>`,
`session_meta_url = http://<host>:<port>/sessions/<session_id>/meta`. The allowlist is
the security model — literal prefix matching means a PM structurally cannot touch
another session or another product's board:

```
Bash(curl -s -X POST {tasks_base_url}*)
Bash(curl -s {tasks_base_url}*)
Bash(curl -s -X POST {session_meta_url}*)
# product-pinned sessions only:
Bash(curl -s -X POST {ROADMAP_BASE_URL}/*)          # POST broad: cross-product handoff is intended
Bash(curl -s -X PATCH {ROADMAP_BASE_URL}/{product}/*)  # PATCH own product only
Write Read WebSearch WebFetch
```

### Turn execution
`_run_pm_turn(text)` runs:
`claude -p <text> --output-format json --system-prompt <sys> --allowedTools <allowlist>
--model <model> [--resume <claude_session_id>]`, `cwd=repo_root` (NOT workspace_dir —
relative paths must resolve against real sources). Save `data["session_id"]` for resume.
Return `{"type":"pm_reply","text":result}` or `{"type":"pm_error","message":...}` (with
literal stderr/exit evidence on failure).

`_run_turn(...)` wraps it: sets `turn_active=True` + fires `on_activity_change` (in a
try/finally so it always clears), yields `{"type":"pm_working"}` first, takes `pm_lock`
(serializes manual turns vs. auto-continuations — two concurrent `--resume` calls on one
session id would race), appends to `chat_history.json`, snapshots under `git_lock`
(`PM turn: <text[:72]>` / `PM auto-continue after dev task <id> (<status>)`), yields the
final event.

### Transcript
`chat_history.json` — a list of `{role, text, ts, attachments?}` with roles
`user | pm | error | system` and epoch-seconds `ts` stamped by `_append_history` as
each entry is written (this is what lets the chat page interleave chat + dev-task
cards into one chronological timeline on reload). Keep `ts` additive/backward-
compatible: legacy entries without it must still load, treated as untimestamped.
`last_message_role()` normalizes pm/error → "assistant" for the waiting-state
computation.

### Two entry points
- `handle_user_message(text, attachments, other_sessions_context, roadmap_context)` —
  resets `_auto_continue_streak = 0`; saves attachments (base64 → files under
  `uploads/`, skipping bad/oversized ones silently) and appends a
  "[The stakeholder attached N image(s)… use your Read tool…]" block with the file
  paths; prepends the two context blocks (see PROMPTS.md §4 — the blocks go only into
  the prompt, never into persisted history: per-turn plumbing, not conversation).
- `handle_task_completion(task, ...)` — increments the streak; builds the auto-continue
  prompt (normal or at-cap variant, PROMPTS.md §3); history entry is a **system** role
  label `Dev task <id> finished (<status>): <description[:100]>`.

### Reset & archive
- `archive_current(reason)` — copy (never delete) SPEC.md, chat_history.json, uploads/
  into `archive/<session_id>/<UTC yyyymmddThhmmssZ>_<reason>/`.
- `reset()` — under `git_lock`: archive with reason "reset", delete SPEC/chat/session
  pointer/uploads, `claude_session_id = None`, snapshot ("PM session <id> reset (prior
  spec/chat archived)"). PROJECT_STATUS/INDEX/docs untouched by construction.
- `_ensure_project_status_seed()` on init — create PROJECT_STATUS.md with an honest
  "No project history yet… proceed to discovery." if missing, so the system prompt's
  read instruction never 404s.

## 6. `sessions.py` — session registry & lifecycle

### Data
`Session` dataclass: `id`, `name`, `branch`, `worktree_path: str|None`,
`base_branch: str|None`, `created_at`, `status`, `is_default`,
`merge_result: dict|None`, `sync_result: dict|None` (separate — a failed sync never
changes status), `product: str|None`, `archived: bool` (visibility flag, independent of
lifecycle), `model` (defaults so old records load), `title: str|None`,
`goal: str|None` (PM-maintained, nullable so old records fall back to `name`).

`Status = active | merging | merged | conflict | archived`.

Persistence: `<workspace_root>/workspace/sessions.json` (git-ignored), whole-file JSON keyed by
id. On load: bootstrap the default session if absent (id "default", branch "main",
worktree = primary checkout, `is_default=True`); flip stale `merging` → `active` (its
background thread died with the old process); backfill migrations for fields added
later.

### Runtime
`SessionRuntime` per session with a worktree: one `threading.Lock` (`git_lock`) shared
by that session's `PMAgent` and `TaskRegistry` so their commits into the same worktree
never interleave (and, for the default session, so merges into main serialize with its
own commits). Wire `pm_agent.on_activity_change` → broadcast a `session_updated` event.

### Cross-session visibility
`describe_other_active_sessions(exclude_id, tasks_per_session=3)` — for every OTHER
`active` session: `- "<title or name>" (<id>) — <goal>: <status>: "<task desc[:100]>"; …`
(up to 3 most-recent tasks; or "active, no dev tasks yet"). Returns `""` when nothing
else is active so callers skip the block entirely rather than injecting noise.

### Live activity (derived, never persisted)
`activity_of(id)` → `{"state","reason","detail"}` with priority:
1. any task `running` in the registry → `working`/`dev_task` (detail = the running
   *dev* task's description; a merge-resolver counts as working but detail stays None);
2. `pm_agent.turn_active` → `working`/`thinking`;
3. last transcript role == assistant → `waiting` (PM answered; stakeholder's move);
4. else `idle`. Also `idle` for any lifecycle status outside active/conflict.

### Lifecycle operations
- `create(name, product, model)` — validate; `git worktree add -b session/<8hex>
  <workspace_root>/workspace/sessions/<8hex> main` from the primary checkout; default name =
  product label or "Session <id>"; build runtime; broadcast `session_created`.
- `merge(id)` — refuse for default or status outside active/conflict; set `merging`,
  broadcast, run `_do_merge` on a background thread. `_do_merge_inner`:
  1. Soft precondition: refuse if the DEFAULT session has a running dev task (cheap
     race guard for a single local user).
  2. `archive_current("merge")` — the session's final spec/chat must not depend on its
     worktree surviving.
  3. Under the session's git_lock: snapshot ("Pre-merge snapshot for session <id>").
  4. Under the DEFAULT session's git_lock: snapshot main ("Pre-merge safety snapshot…"),
     then `_run_merge` (below). Outcome → status `merged` (+`merge_result`) or
     `conflict`; broadcast `session_merged`.
- `_run_merge(worktree_root, branch_to_merge, commit_message, conflict_label,
  conflict_runtime)` — direction-agnostic (session→main and main→session):
  `git merge --no-ff <branch> -m <msg>`; on conflict, collect
  `git diff --name-only --diff-filter=U`, dispatch a **synchronous** resolver task via
  `conflict_runtime.task_registry.run_conflict_resolution` (text in PROMPTS.md §5), then
  **independently verify** (never trust the resolver's self-report):
  `_merge_still_broken` = any porcelain unmerged codes (UU/AA/DD/AU/UA/UD/DU) OR any
  listed file with a line **starting** `<<<<<<<` or `>>>>>>>` (not bare `=======` —
  too common in legitimate content). Broken or resolver-error → `git merge --abort`,
  outcome `failed`. Else `git commit --no-edit` (abort on failure too) → outcome
  `auto_resolved` (deliberate product call: no manual confirm step). Clean merge →
  `clean`. Never raises for a conflict; always returns an outcome dict
  `{outcome, message?, conflicted_files?, resolver_task_id?, merged_at/synced_at}`.
- `sync(id)` — merge main INTO the session's worktree (anti-drift). Same `_run_merge`
  with `conflict_runtime` = the session's own; failure only sets `sync_result`, never
  status. Refuse if that session has a running dev task.
- `terminate(id)` — "merge, then don't stop": run the exact merge flow; only if it
  reaches `merged`, remove worktree (safe delete) + branch, `status=archived`,
  `archived=True`, drop runtime. On conflict it stops there — **there is no path that
  skips the merge**, so a terminated session's work is never lost to a force-deleted
  unmerged branch.
- `cleanup(id)` — post-merge worktree removal (refuse unless status == merged).
  `delete(id)` — force removal (never for default). Both → archived.
- `set_meta(id, title, goal)` — trim; empty/whitespace = "no change" (a PM can't blank
  its own identity by accident); clamp title ≤ 60, goal ≤ 200; persist + broadcast.
- `set_model(id, model)` — validate, persist, AND update the live runtime
  (`pm_agent.set_model`, `task_registry.model`) so it applies to the very next
  turn/task with no restart.
- All lifecycle mutations guarded by one `_registry_lock`; subscriber/broadcast pattern
  identical to TaskRegistry's.

## 7. `server.py` — FastAPI app + websockets

Singletons: `SessionManager`, `RoadmapStore`. Capture the asyncio loop in lifespan; all
background-thread → websocket hops via `loop.call_soon_threadsafe(asyncio.create_task, …)`.

### Endpoints
| Route | Behavior |
|---|---|
| `GET /` | sessions.html |
| `GET /sessions` | list of `_public_session` dicts (session + derived `activity`) |
| `POST /sessions` | create `{name?, product?, model?}` |
| `GET /sessions/{id}` | one session |
| `POST /sessions/{id}/model` | live model switch |
| `POST /sessions/{id}/meta` | PM-maintained title/goal; returns `_public_session` |
| `GET /models` | the allow-list |
| `POST /sessions/{id}/merge` / `/sync` / `/terminate` / `/cleanup` / `/archive` / `/unarchive`, `DELETE /sessions/{id}` | lifecycle (background threads for merge/sync/terminate) |
| `GET /chat/{id}`, `GET /dashboard/{id}`, `GET /roadmap` | static pages |
| `GET /history/{id}` | chat_history.json |
| `GET /chat/{id}/uploads/{filename}` | attachment files (basename-sanitized) |
| `POST /chat/{id}/reset` | archive + clear |
| `POST /tasks/{id}` `{"task": "..."}` | dispatch dev task (immediate return) |
| `GET /tasks/{id}`, `GET /tasks/{id}/{tid}` | list / one |
| `GET /roadmap/data` | `{products, items-by-product}` |
| `GET/POST /roadmap/{product}/items`, `PATCH/DELETE /roadmap/{product}/items/{item_id}` | board CRUD; PATCH/DELETE verify the URL product OWNS the item (this is what makes own-product-scoped allowlists safe); PATCH with `move_to_product` triggers the move flow |
| WS `/ws/chat/{id}`, `/ws/tasks/{id}`, `/ws/sessions`, `/ws/roadmap` | push channels |

Optional: zero-segment aliases to the default session (`/tasks`, `/history`, `/ws/chat`,
`/ws/tasks`, `/dashboard`) for backward compat.

### Chat websocket flow
On message `{text, attachments[]}`: build `other_ctx =
describe_other_active_sessions(id)` and `roadmap_ctx = _roadmap_context_for(id)` (own
product deep + others digest for pinned sessions; digest-of-all for unpinned), run
`handle_user_message` in an executor, stream events back through an asyncio queue.
**Per-connection send lock**: two producers can push to one socket (the normal turn
loop, and an auto-continuation broadcast firing at an arbitrary time) and concurrent
`send_text` isn't safe — every send on both paths takes the lock.

### Task completion fan-out
`TaskRegistry.subscribe` → on every task write: broadcast `task_update` on
`/ws/tasks/{id}`; broadcast an enriched `session_updated` on `/ws/sessions` (activity
changed); and **only for `kind=="dev"` reaching done/error**, spawn a thread running
`handle_task_completion`, broadcasting each resulting event to every open chat socket
for that session with `"auto": true` (a merge-resolver finishing must NOT make the PM
narrate an unrelated merge).

## 8. `__main__.py` + `scaffold.py`
`python -m pm_studio` (or the `pm-studio` script): runs
`uvicorn.run("pm_studio.server:app", host=CONFIG.host, port=CONFIG.port)` plus a
thread that opens the browser to the root URL after ~1 s. `python -m pm_studio init`
runs `scaffold.run_init`: non-destructively creates `pm_studio_local/` (config +
instruction stubs + `knowledge/`), the seed `PROJECT_INDEX.md`/`PROJECT_STATUS.md`,
and appends the runtime-state `.gitignore` block. `version` prints the package
version.

## 9. Static UI (vanilla HTML/JS/CSS, one file per page)

### Shared navigation (`nav.css` + `nav.js`, served at `/static/nav.*`)

The only assets more than one page loads. Every page mounts them the same way — the
stylesheet and a **non-deferred** `<script>` in `<head>`, plus one element in the body:

```html
<div id="pm-nav" data-page="roadmap"></div>
```

`nav.js` renders two rows, and the split is the design:

1. **The bar** — brand, then the flat set of destinations (Portfolio · Roadmap ·
   Sessions │ Time & cost · People) with the current one marked by `aria-current="page"`
   plus weight *and* a tinted chip, so "you are here" survives greyscale. On the right,
   in enterprise mode, the acting user, their role and **Sign out** — on every page, not
   just the sessions list. `Time & cost` and `People` are filtered by capability
   (`view_cost`) and role (`admin`); in personal mode `People` is hidden entirely, since
   its endpoints are enterprise-only and the tab would lead to nothing but an error.
   Hiding a tab is orientation, never protection — §7 enforces every capability.
2. **The context row** — *where* that destination sits. The three pages that make up the
   work model get a flow map, `Portfolio → Roadmap → Sessions`, with the current stop lit
   and each stop a link; `/costing` and `/people` render the same map with no stop
   claimed plus one line on how they relate to it. The pages nested inside a session
   (`chat`, `dashboard`) instead get a breadcrumb — `Sessions › <session title>` — and
   the session's sub-tabs, **Chat** / **Dev lifecycle**.

Two constraints that are easy to break:

- **The script must not be deferred.** Pages run inline scripts at the end of `<body>`,
  which execute before any deferred script would. Loading it plainly from `<head>` means
  `window.PMNav` already exists for them. It exposes `PMNav.auth` and `PMNav.session`
  (both resolve to `null` instead of rejecting), so a page load makes exactly one
  `/auth/me` and at most one `/sessions/{id}` however many consumers there are —
  sessions.html reads `auth` to hide the create form, chat.html reads `session` for its
  metadata.
- **Every rule in `nav.css` is scoped to `#pm-nav`,** and the block after the tokens
  resets the inherited properties the component cares about. Pages style bare elements —
  portfolio.html puts a border and padding on every `li` — and those rules would
  otherwise reshape the nav's own markup. The nav also defines its own `--pmnav-*`
  palette rather than borrowing page tokens, whose names differ per page (chat.html uses
  `--bg`/`--fg`/`--muted`, the board pages `--page`/`--ink-primary`).

Page `<header>`s therefore carry only what the page *is* and controls that act on it —
no per-page link sets, which is what let them drift out of sync and leave
`/dashboard/{id}` with no inbound link from anywhere at all.

- **sessions.html** (`/`) — session cards: title (fallback name → "Untitled session"),
  muted one-line goal, product tag, model selector (POST /model), lifecycle status
  badge, and the LIVE activity signal: pulsing-dot "Working" pill (with the running dev
  task's description when reason=dev_task), a loud amber left-border + tinted
  background + "⏳ Waiting for you" badge for waiting (with a reduced-motion guard),
  ordering waiting → working → rest. Create form: optional name ("leave blank to
  auto-title"), product picker, model picker. Per-card actions: open chat, merge, sync,
  terminate, cleanup/delete, archive toggle (+ "show archived" filter). Live via
  `/ws/sessions`; initial `GET /sessions`.
- **chat.html** (`/chat/{id}`) — ONE chronological timeline, everything reading
  oldest→newest with newest at bottom (no second list sorted the other way — that was
  a real stakeholder complaint that forced a redesign). Chat messages (user / pm /
  error / system) interleave by `ts` with **inline dev-task cards** anchored at their
  chronological position in the stream, visually distinct from chat bubbles (status-
  colored left accent + elbow connector). Card anatomy: an always-visible top row
  (status badge with running-pulse/done/error + relative time / "took Xm Ys"), the
  **description collapsed** behind a `<details>` "Task" disclosure whose summary is a
  one-line ~90-char truncated preview (verbose task specs otherwise fill the screen),
  and a separate collapsed Result disclosure. Cards **update in place** via a
  `taskId → cardElement` map upserted on every `/ws/tasks/{id}` message — never emit
  throwaway "task started/done/failed" status rows; a newly started task appends its
  card at the current bottom. Hydration on reload: merge `GET /history` (ordered by
  `ts`; array-order fallback for legacy entries without `ts`) with `GET /tasks`
  (ordered by `started_at`); never crash on missing timestamps. Auto-scroll to bottom
  only when the user is already near the bottom (don't yank them while reading
  history); jump to bottom on first hydration. Session lifecycle controls live in a
  compact **collapsible strip under the header** (collapsed: branch + status badge;
  expanded: Merge to main / Clean up worktree buttons and `merge_result` details, kept
  live via `/ws/sessions`, expanded state surviving re-renders) — they are
  session-level, not per-task, so they don't belong in the stream. Composer with image
  attach (base64), "PM is working…" indicator on `pm_working`, reset button (confirm
  first); auto-continuation events arrive marked `auto:true`. Markdown-ish rendering
  of PM replies is nice-to-have.
- **dashboard.html** (`/dashboard/{id}`) — the session's task list with statuses,
  durations, results; live via `/ws/tasks/{id}`.
- **roadmap.html** (`/roadmap`) — one section per product; Now/Next/Later columns;
  cards show status (pending/in_progress/done), origin badges ("suggested by X" /
  untriaged flag with accept action), move-to-product control (stakeholder moves pass
  `triaged:true`); done items collapse into a per-product "recently shipped" strip
  rather than disappearing. Live via `/ws/roadmap`; stakeholder can add/edit items
  directly.

## 10. Concurrency model (summary)

| Lock | Guards |
|---|---|
| per-session `git_lock` | all commits into that worktree (PM turns, dev-task snapshots, merges into it) |
| per-session `pm_lock` | PM turn execution + history append (manual vs auto-continue) |
| `SessionManager._registry_lock` | registry mutations (create/cleanup/delete/meta/model/archive) |
| `RoadmapStore._lock` | item mutations |
| per-chat-ws asyncio send lock | concurrent sends on one websocket |

Threads: dev tasks, merge/sync/terminate, and auto-continue each run on daemon threads;
everything crossing into asyncio goes through `call_soon_threadsafe`. Every background
flow is wrapped so an exception marks an outcome (conflict/failed/error event) instead
of crashing the server or wedging a session in `merging`.

## 11. Timeouts & limits
PM turn 1800 s; dev task 1800 s; auto-continue streak cap 6; attachments 6 × 10 MB;
title 60 chars; goal 200 chars; task/session/item ids `uuid4().hex[:8]`.
