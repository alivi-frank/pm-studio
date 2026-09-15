# Engineering Intelligence (`pm_studio.signals`)

The rest of PM Studio models **state**: a ticket's status, a change's bucket, a
project's lifecycle. This layer models **events** - every observable act of engineering
- and derives, at read time and for any time window, where effort actually went, what
it produced, how work flows, how people work, what is drifting, and an audit-ready
statement of capitalizable time. An independent AI judge scores the whole picture and
feeds back into the rules.

Page: `/intelligence`. Package: `pm_studio/signals/`. Config: `[signals]` in
`pm_studio_local/config.toml` (see CONFIGURATION.md). State: `<workspace>/signals/`.

## The signal

```
Signal(id, at, source, kind, actor, actor_email, refs=["jira:NDT-5561", ...],
       repo, system, weight, minutes, meta)
```

`kind` names what happened (`commit`, `merge`, `status`, `assignee`, `comment`,
`worklog`, `field`, `created`, `pr_opened`, `pr_merged`, `pr_review`, `ai_turn`,
`meeting`, `message`); `source` names where it was observed. `weight` is the kind's
effort weight in minutes-equivalent (tunable), used only to SPLIT a person's day.
`minutes` is explicit time the actor recorded (a worklog, a meeting) and is booked as-is.
Ids are stable hashes of the event's identity, so a refresh never duplicates and
feedback keyed on ids survives.

## Sources (`signals/sources/`)

One interface (`base.SignalSource`): `configured`, `describe()`, `collect(since,
previous) -> CollectResult(signals, facts, notes, truncated)`. Registered in
`service.IntelligenceService._build_sources`.

| id | reads | how |
|---|---|---|
| `git` | every nested checkout under each `[systems]` path (+ `extra_repos`), discovered to depth 3 | `git log --all --numstat` since the configured date; ticket keys from subject+body (Jira `PROJ-123`, ADO `#1234` / `ticket #1234` / `AB#1234`; PR numbers are stripped first); merge vs commit; AI co-author trailers; bot authors carry zero weight |
| `jira` | full changelog, worklogs and comments for every issue in the tracker's projects | `/rest/api/3/search/jql?expand=changelog` in pages of 100 (~4k issues in ~60 requests); per-issue top-ups where a list overflowed; status names mapped to categories via `/rest/api/3/status`; incremental on `updated` |
| `ado` | every work item revision | `wit/reporting/workitemrevisions` (1,000 per call, continuation token kept for the next run); revisions diffed per item into state / assignee / CompletedWork (booked as worklog) / comment / field signals; state categories from `workitemtypes` |
| `ado-prs` | pull requests with reviewers and votes | `git/pullrequests` per project in `ado_pr_projects`; attributed by work item ids in title and branch |
| `pm` | PM Studio's own agent turns and dev tasks, with measured spend | `activity.jsonl` + every session's task records |
| `inbox` | calendar / email / chat exports | any `*.json` / `*.jsonl` dropped in `<workspace>/signals/inbox/` (record shape documented in `sources/inbox.py`); one signal per attendee, meeting durations booked as explicit time |

Adding a source is one class and one line in `_build_sources`; nothing downstream knows
which source a signal came from.

## Attribution (`attribution.py`)

Every signal becomes one *slice* per resolved ticket reference (equal shares), each
carrying the chain `ticket -> change -> project -> initiative -> goals`, the person,
the product/system, the nature of the work (feature / defect / task / discovery), and
`via` - how the chain was established:

1. `change` - a change linked 1:1 to the ticket;
2. `parent-change` / `epic-project` - the ticket's parent chain (three hops);
3. `own-epic` - the ticket is itself a project's epic;
4. `route-unplanned` - known ticket, planned nowhere; product via import routes;
5. `unknown-ticket` - a key the catalog does not hold;
6. `repo-only` - no key at all; the repository's system is all we know.

`coverage()` reports the share of weight placed on a real project - the first number
to trust before any per-initiative figure.

People: `identity.IdentityResolver` reads the people directory (tracker identities,
emails, names), an alias table (`signals/aliases.json`), and falls back to exact then
first-name+surname fuzzy matching. Unknown actors get a stable synthetic person so
effort is still counted, and surface as `unresolved_author` findings with candidates.

## Allocation (`allocation.py`)

- A person's *active day* (any non-bot signal that day, in the configured zone) is worth
  `capacity_hours_per_day`. No signal, no hours - nothing is invented.
- Explicit minutes (worklogs, meetings) are booked first, exactly where logged.
- The remaining capacity is split across everything else touched that day by weight.
- Logged time above capacity stands; nothing is inferred on top.

Hours therefore reconcile (active days x capacity, or more only where logged) and every
row keeps the signal ids that earned it. `rollup()` and `series()` fold rows by any
dimension; goals are non-additive by design (an initiative serving two goals counts
under both) and are never summed into a grand total.

## Metrics (`metrics.py`)

- **flow**: for tickets finished in the window - cycle (first in-progress -> done), lead
  (created -> done), review/QA dwell, blocked dwell, reopen rate, weekly started /
  finished / WIP. Timelines come from the trackers' own transitions.
- **workflow & developer experience**: commit hour-of-day heatmap (author-local),
  after-hours and weekend share, projects per person-day and fragmented days, tagging
  discipline per repo, AI-assisted share, pull-request open time and review counts.
- **impact**: per initiative - hours, people, commits, tickets touched/done, changes
  shipped (by the tracker's done date, not the board's mirrored stamp), agent spend,
  done per 100 hours; per goal (overlapping shares); releases in the window.

## Findings (`findings.py`)

Rules over the same slices, each firing on an entity with evidence; ids are stable
(`rule + entity`). Families: lifecycle (`stale_in_progress`, `zombie_in_progress`,
`stale_project`, `ideation_with_commits`, `idle_assignee`), investment
(`silent_initiative`, `unplanned_work`, `maintenance_share`), hygiene
(`work_after_done`, `status_lag`, `unkeyed_commits`), flow (`review_dwell`,
`reopened`), workflow (`fragmentation`, `bus_factor`), data (`unresolved_author`).
A stale ticket produces one finding: the long-running and review rules stay quiet for
it. Thresholds: package defaults <- `[signals.thresholds]` <- `signals/tuning.json`.

Feedback (`state.FeedbackStore`, `signals/feedback.json`): confirm / dismiss / snooze /
reopen per finding, plus the judge's verdict. Dismissed and snoozed findings leave the
default list but stay counted; rules can be muted. Nothing is ever deleted.

## Finance (`finance.py`)

Period statement, person x project: hours (logged vs inferred), capitalizable vs
expensed hours, treatment, rate (costing roster via the person's account, else blended,
else "no rate" - money totals are `None`, never zero, when nothing is priced), evidence
counts and basis. Capitalizable by default = development work on a live project of a
non-maintenance initiative, excluding defects; per-initiative overrides live in the
tuning file. `GET /intelligence/finance.csv` exports it; `GET /intelligence/trace`
returns the day rows and signals behind one cell.

## The judge (`judge.py`) and the feedback cycle

A read-only `claude -p` run (allowlist: `Read Grep Glob Bash(git log:*) Bash(git
show:*) Bash(git shortlog:*)`) over a dossier written to `signals/judge/dossier-*.json`:
coverage, allocation, impact, flow, workflow, findings with evidence and prior verdicts,
human feedback, thresholds, sources, and its own previous judgment. It answers with
scores per dimension, a verdict per finding (`confirm` / `noise` / `needs_data`),
threshold suggestions, data fixes (aliases, repo mappings, ticket links) and next
metrics. Verdicts land on the findings immediately; suggestions and aliases apply only
when a person accepts them (`POST /intelligence/judge/suggestions/{i}/apply`,
`POST /intelligence/aliases`). Judgments append to `signals/judgments.jsonl` so the
scores trend. Any failure is an `inconclusive` judgment, visibly. A deterministic
`self_assessment` runs every time beside it.

## HTTP (`api.py`)

Reads need `view`; refresh / judge / feedback / tuning / aliases need `manage_roadmap`;
money columns need `view_cost` (hours are always visible).

```
GET  /intelligence                      the page
GET  /intelligence/data?from&to&<filter> the report (filters: initiative_id, project_id,
                                         goal_id, product, system, person_id, repo, source)
GET  /intelligence/finance.csv?from&to  capitalization statement
GET  /intelligence/trace?person_id&project_id&from&to
GET  /intelligence/signals?ref=|project_id=
GET  /intelligence/sources
POST /intelligence/refresh {sources?}   POST /intelligence/judge
POST /intelligence/findings/{id}/feedback {state, note?, snooze_days?}
POST /intelligence/tuning {kind, key, value, reason?}
POST /intelligence/aliases {handle, person_id}
POST /intelligence/judge/suggestions/{index}/apply
```

## Files under `<workspace>/signals/`

- `cache/<source>.json.gz` - each source's last result (rebuildable, large; ignore in git)
- `feedback.json`, `tuning.json`, `aliases.json`, `judgments.jsonl` - human/judge state
- `judge/dossier-*.json` - what each judge run was shown
- `inbox/` - drop folder for collaboration exports
