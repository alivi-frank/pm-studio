# Way of Working — the PM / Dev-Cycle Methodology

The software (ARCHITECTURE.md) mechanizes a methodology. This file states the
methodology itself, plus the operational lessons learned running it for weeks across
six products and a dozen parallel sessions. If you change the software, keep these
invariants; if you run the system, these are the house rules.

## 1. Roles

- **Stakeholder (human):** sets goals, makes product calls, confirms milestones.
  Never debugs engineering problems — that's the PM's job to absorb.
- **PM agent (one per session):** owns the spec, the roadmap for its product, and the
  dev pipeline. Autonomous by default; interrupts the human only for genuine product
  decisions. Does research itself (WebSearch/WebFetch) and writes it into `docs/`,
  not into chat.
- **Dev agents (ephemeral):** one bounded task each, full permissions in the worktree,
  no memory between tasks. All continuity lives in the repo (code, docs, task specs) —
  never in a dev agent's head.

## 2. The dev cycle

```
stakeholder goal
   → PM asks focused questions until the goal is concrete   (only if vague)
   → PM writes/updates SPEC.md (full rewrite, not a diff)
   → PM checks the other-sessions block for overlap
   → PM dispatches ONE bounded dev task (a vertical slice)
   → dev agent builds, tests, commits
   → task finishes → PM auto-re-invoked
   → PM reads the result, verifies, updates PROJECT_STATUS.md + roadmap
   → next slice dispatched immediately … (repeat)
   → milestone reached OR decision needed OR streak cap hit → check in with stakeholder
```

Key property: **the human is not the scheduler.** Task completion re-invokes the PM
mechanically; the PM chains slices until the goal is done. The streak cap (6 auto
turns) is the only brake, and it converts runaway autonomy into a concrete question.

## 3. Document taxonomy (the memory model)

Four layers, strictly separated — this is what lets any fresh agent catch up cold:

| Layer | File | Lifetime | Rule |
|---|---|---|---|
| Map | `PROJECT_INDEX.md` (repo root) | permanent | Lists every doc and where truth lives. A fresh PM reads it FIRST, before saying anything about scope. Every new durable doc gets a line here — an unindexed doc is lost. |
| Durable record | `PROJECT_STATUS.md` (repo root) | permanent; never touched by resets | Compact "what's built / decided / open." **Rewritten in full** whenever a slice ships or a decision lands — not appended, rewritten, so it stays a summary rather than a log. |
| Live spec | `<workspace_root>/workspace/current/SPEC.md` | per phase; archived+wiped on reset | What's being built right now. Full-rewrite on every change. |
| Reference | `docs/*.md` | permanent | Research, plans, milestone specs, methodology. Consult before deciding in their territory; PROJECT_STATUS summarizes but never replaces them. |

Chat history is the fifth, throwaway layer: resets archive it. Anything worth keeping
must be promoted out of chat into one of the four layers above before a reset.

## 4. Slicing & dispatch rules (learned the hard way)

1. **Vertical slices, bounded phases.** Each dev task is one shippable slice with an
   explicit "what done looks like." Big combined slices hit the 30-minute timeout —
   split them into sequential bounded phases instead.
2. **One task in flight per set of files.** Two tasks touching the same files run
   sequentially, never in parallel (e.g. two nav features touching one shared layout
   file = serial). Tasks on disjoint files may run in parallel safely.
3. **Task descriptions must be self-contained.** The dev agent has no chat context:
   include paths, constraints, conventions, and acceptance criteria in the description
   itself.
4. **Tell tasks to commit incrementally and commit+merge when green.** A task that hit
   an error/timeout/spend-limit may STILL have committed useful work — check
   `git log`/disk before assuming nothing landed; retries should be idempotent
   continuations, not restarts.
5. **Mind the dispatch shell.** The task string travels through JSON→HTTP→shell; in the
   reference environment zsh rejected `<`, `>`, `<->`, `<=` (numeric-range globbing) in
   task text — reword ("less than or equal") rather than fight quoting.
6. **Two strikes rule.** A task failing twice on the same underlying problem stops the
   loop: explain it to the stakeholder rather than burning a third blind attempt.
7. **After changing a shared package, rebuild it.** Consumers import compiled `dist/`
   output that is git-ignored; a task that edits package source must run that package's
   build, or every consumer sees a stale artifact.
8. **When unsure what code currently does, dispatch a task to inspect it** rather than
   trusting possibly-stale reads across worktree boundaries — the dev environment's
   filesystem view can lag the PM's.

## 5. Multi-session coordination

- One session per concern (typically per product, pinned so it gets deep roadmap
  context and own-board write access). The default session stays a general/unpinned
  coordinator on `main`.
- **Check the other-sessions block every turn before dispatching.** The recurring
  failure mode is two sessions independently building the same thing. On overlap:
  don't dispatch — surface it to the stakeholder (let the other session own it, wait
  and sync, or explicitly diverge).
- **Sync early, sync often.** Long-lived sessions merge main into themselves
  (`/sync`) so the terminate-time merge stays small.
- Merges conflict most on the shared narrative files (PROJECT_STATUS.md, SPEC.md).
  Resolution rule: keep the newer, most complete narrative **without losing content**
  from either side — these files are consolidated multi-session views.
- Roadmap-card status is the ITEM's state, not a live task indicator: flip a card to
  done when its work is **merged**, or the board shows a stale "in progress" with
  nothing running.
- Cross-product work is handed off via the board (`origin_product`, lands untriaged),
  never built by the noticing session. The owning PM triages suggestions in or drops
  them.
- Where `[systems]` is declared, every change names the one **system** it is contained
  within — chosen by whose code actually changes, not by which name is closest to the
  feature's. A change with no system is an inconsistency to close, not a resting state:
  attribute it the next time you touch it. Never invent an attribution to clear the
  error; if none of the product's systems fits, that is a question for the stakeholder.

## 6. Git discipline

- Every PM turn and every dev-task completion ends in a snapshot commit
  (`git add -A`, skip if clean, never raise). History is therefore a complete audit
  trail: `PM turn: …`, `Dev task <id> (done): …`, `Pre-merge snapshot…`,
  `Merge session <id> …`.
- `.gitignore` draws the line: runtime/bookkeeping state (sessions.json, roadmap data,
  live chat history, task records, worktrees dir, node_modules, dist) stays out;
  SPEC.md, archives, docs, and product code go in.
- Merges into main are `--no-ff` (a session's landing is visible as a merge commit).
  Conflicts get an AI resolver whose work is **independently verified** (unmerged
  paths + line-start conflict markers) before committing; anything doubtful aborts
  cleanly.
- Terminate always merges first; there is no "discard a session's work" path short of
  an explicit force delete.
- **Never push to a remote unless the stakeholder explicitly asks.** (Standing rule in
  the reference project; make your own call at bootstrap, but make it explicit.)

## 7. Communication norms (PM ↔ stakeholder)

- Conversational, concise; outcomes in plain language, never tool mechanics.
- Never promise to "check back later" — completion re-invocation is mechanical.
- Never invent failure explanations; report literal errors, or say "I don't know why."
- Ask only genuine product questions: scope tradeoffs, ambiguous decisions, milestone
  confirmation. Status updates are given, not asked about.
- Milestone check-ins: when a goal area is fully shipped, say so plainly ("nothing in
  flight") and wait for direction rather than inventing work.
- Keep session title (~2–5 words) + one-sentence goal current — it's what the sessions
  list and other PMs see.

## 8. Operational notes for the host environment

- The server must stay running: it owns the roadmap store, the auto-continue wiring,
  and the websockets. After upgrading the installed pm-studio package, or editing
  `pm_studio_local/` (config, instructions, knowledge), restart the server — both are
  read at startup. The package itself is never edited in a deployment: it is
  maintained upstream only, and anything a deployment can't achieve via
  `pm_studio_local/` is a feature request against the pm-studio repo, not a local
  patch.
- Server restarts are safe by design: stale `running` tasks flip to `error` with an
  honest "interrupted" message; stale `merging` sessions flip back to `active`. Do not
  treat an unqueryable old task id as lost work — the commits may be on disk.
- Everything binds to 127.0.0.1 and the dev agents run with bypassed permissions —
  this is a single-trusted-user, local-only tool. Do not expose the port.
- Costs: every dev-task completion triggers a PM turn on the session's model. The
  streak cap is the budget guard; use cheaper models for exploratory sessions.
