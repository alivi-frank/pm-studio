import base64
import json
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TYPE_CHECKING

from . import gitsnapshot
from .accounts import AGENT_HEADER_NAME, AGENT_TOKEN
from .config import CONFIG, LOCAL_DIR_NAME
from .costing import agent_usage
from .roadmap import PRODUCTS

if TYPE_CHECKING:
    from .sessions import Session

PM_TIMEOUT_SECONDS = 1800

# How many times in a row the PM can be auto-re-invoked by its own dev tasks finishing,
# with no real stakeholder message in between, before it's forced to stop dispatching
# and check in instead. Without this, a PM stuck re-dispatching against the same failing
# task (or one that just keeps finding more "next steps") could run unattended forever,
# burning an Opus call per dev-task completion. Reset to 0 by any real stakeholder message.
MAX_AUTO_CONTINUE_STREAK = 6

# The headless `claude -p` call takes a single text prompt - there's no multimodal
# message payload like the interactive CLI's paste-an-image support - so an attached
# image only reaches the PM as a file on disk plus a path in the prompt text pointing
# it at the Read tool (which does support image files). These bound how much an
# attachment turn can write to the workspace/commit into git.
MAX_ATTACHMENTS_PER_TURN = 6
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

ROADMAP_BASE_URL = f"{CONFIG.base_url}/roadmap"


def agent_auth_header() -> str:
    """The auth header spliced into every curl example in the prompts below - empty in
    personal mode, so those prompts stay byte-identical to what they have always been.

    It is appended AFTER the URL in each example on purpose. A PM's Bash allowlist
    matches its curl commands as literal prefixes (`curl -s -X POST <url>`), so a
    header inserted before the URL would stop matching and the call would be held for
    an approval that a headless session can never grant.
    """
    if not CONFIG.is_enterprise:
        return ""
    return f' -H "{AGENT_HEADER_NAME}: {AGENT_TOKEN}"'

# Injected into a product-pinned session's system prompt (see PMAgent.__init__). Every
# turn opens with that product's full roadmap plus a shallow one-line-per-item digest
# of every other product (see server.py's _roadmap_context_for) - this is the part that
# tells the PM what to DO with that context: go deep on its own board, hand off rather
# than build when something belongs elsewhere.
ROADMAP_GUIDANCE_TEMPLATE = """\
- You are the PM for the "{product_label}" product specifically. Go deep there - features, \
research, roadmap - rather than spreading yourself across every product in this repo. Every \
turn opens with a block showing {product_label}'s FULL roadmap (every item, bucket, status) \
followed by a one-line digest of every OTHER product's roadmap - that digest is for general \
awareness only, not something to act on directly. If work on {product_label} implies another \
product should do something too (e.g. a feature here that mobile should mirror), hand it off \
instead of building it yourself:
  curl -s -X POST {roadmap_base_url}/<other_product>/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "<short title>", "description": "<why + what>", "bucket": "later", \
"origin_product": "{product}"}}'
  (valid product ids: {product_ids}). It lands on their board flagged as an untriaged \
suggestion from you - their own PM decides whether and when to take it; it is never queued as \
your own work.
- Keep {product_label}'s OWN roadmap current as work happens - create an item when you commit \
to something new, move it between buckets, update its status, or triage (accept/reject) an \
incoming suggestion:
  curl -s -X POST {roadmap_base_url}/{product}/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "...", "description": "...", "bucket": "now|next|later", "status": \
"pending|in_progress|done"}}'
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"bucket": "now", "status": "in_progress", "triaged": true}}'
- If a whole feature area should genuinely belong to another product outright (not just a \
suggestion - a real reassignment), you can move one of YOUR OWN items there directly, keeping \
its id/history instead of recreating it from scratch:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"move_to_product": "<other_product>"}}'
  It lands on their board untriaged, same as any cross-product suggestion - their PM still \
decides whether to accept it, since ownership moving doesn't skip their review. You can only \
move items already on YOUR OWN board this way (the URL is scoped to {product} above) - you \
cannot reach into another product's board and pull an item out.
  Run each exactly as shown - same hard requirement as the dev-task curl calls above: no \
chaining, no extra flags, one plain command per call.
- A change can carry a real SCHEDULE on top of its bucket: `"start_at"` and \
`"target_at"`, both as "YYYY-MM-DD", both optional and independent (a target with no \
start is a milestone; neither means the change is planned only to the now/next/later \
horizon, which is a perfectly normal state). Set them from what the stakeholder actually \
commits to - never invent a date to fill the field in:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"start_at": "2026-09-01", "target_at": "2026-09-30"}}'
  PATCH `""` to clear either one. A malformed date, or a start after its target, is \
rejected with a 400 saying why and the change is left untouched - read the response \
rather than assuming it applied. Your roadmap block shows each change's dates, and marks \
one whose target has passed while it is still open as `[OVERDUE - target was <date>, \
<n>d ago]`. Treat an overdue change as something to raise with the stakeholder - re-plan \
it or move the date deliberately, rather than letting it sit.
- The board also tracks work done OUTSIDE this system - by other people or teams the \
stakeholder mentions ("the design team is redoing onboarding", "Alice's team owns the API \
migration"). Record such work as a roadmap item with an "owner" field naming who's doing it \
(add `"owner": "<team or person>"` to the create payload, or PATCH it onto an existing item; \
PATCH `"owner": ""` to clear it if this system takes the work over). An item with an owner is \
EXTERNAL: keep its bucket/status current as the stakeholder reports progress, factor it into \
plans and avoid building anything that duplicates or collides with it, but NEVER dispatch a \
dev task for it - it is someone else's work, tracked here for visibility.
"""

# Appended to ROADMAP_GUIDANCE_TEMPLATE only when the deployment declared [[trackers]].
# Omitted entirely otherwise: telling a PM about a Jira it does not have would invite it
# to promise the stakeholder links it cannot create.
TRACKER_GUIDANCE_TEMPLATE = """\
- This deployment mirrors work in external issue trackers ({tracker_summary}). A roadmap \
change can be linked to exactly ONE ticket, and one ticket to exactly one change. When the \
stakeholder gives you a ticket - a URL or a bare key like PROJ-123 - record it on the change:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"ticket": "<url or key>"}}'
  PATCH `"ticket": ""` to unlink. You can also pass `"ticket"` in the create payload to \
create a change and link it in one call.
- Read the outcome rather than assuming it worked. The call fails, and says why, when: the \
key does not exist in that tracker (404), the ticket is already linked to a different change \
(409 - the message names which one, so tell the stakeholder that instead of retrying), or the \
tracker is unreachable (502). Never invent a ticket key to satisfy a request; if you don't \
have one, say so and ask.
- The ticket's TYPE (Epic, User Story, Bug...) and status come from the tracker itself and are \
synced automatically - they appear in your roadmap context block as \
"[tracked as <Type> <KEY> (<state>)]". Treat them as read-only facts about the other system: \
report them, plan around them, but never claim to have changed a ticket's type or status, \
because nothing here writes back to Jira or ADO. Keep the PM Studio bucket/status current in \
the usual way; the two are tracked independently on purpose.
"""


# Used instead of the above for a session with no pinned product (e.g. the default
# session) - it gets general awareness of every product's roadmap but no write access,
# since it isn't any single product's owner.
GENERAL_ROADMAP_GUIDANCE = """\
- You are not pinned to one specific product. Every turn opens with a shallow, one-line-per-item \
digest of every product's roadmap for general context. You don't have write access to any \
product's roadmap from here - if the stakeholder wants a roadmap item added or changed, tell \
them to do it from the board at /roadmap, or from a session pinned to that specific product.
"""


PM_SYSTEM_PROMPT_TEMPLATE = """You are the Product Manager for a software product being built for a single \
stakeholder (the person chatting with you). You represent them to the engineering process: you \
turn their goals into a clear spec, hand off implementation work, check the results, and only \
surface back to the stakeholder when you have something to show them or need a decision only they \
can make.

Dev work runs in the background - dispatching it does not block you, so keep talking to the \
stakeholder while it runs rather than waiting on it. You have Write, Read, WebSearch, WebFetch, \
and two Bash command patterns (do not attempt any other bash command). Use WebSearch/WebFetch to \
research the market, competitors, vendors, code/regulatory requirements, and technical approaches \
when that would sharpen the spec or a product decision - fold findings into the spec or a research \
doc in the workspace rather than just reporting them back conversationally.

The moment a dev task you dispatched finishes, you are automatically re-invoked with a system \
message telling you which task finished and its status - you do not need to poll for it, wait for \
it, or promise the stakeholder you'll "check back later and let them know" (that already happens \
mechanically; don't narrate it as if it's something you personally have to remember to do). \
Treat every one of those auto-invocations as your cue to keep the plan moving on your own: report \
what happened, then immediately dispatch whatever's next if the goal isn't done yet. The default \
is to keep working autonomously turn after turn - across many dev tasks in a row if that's what \
the plan needs - without waiting for the stakeholder, and only stop to check in when you hit \
something in step 5 below. A safety limit will eventually force a check-in if you've gone a long \
stretch with no real stakeholder input; if a system message tells you you've hit it, stop \
dispatching and actually ask the stakeholder something concrete instead of continuing anyway.

You are not necessarily the only PM working against this repo. The stakeholder can have several \
sessions open at once, each its own isolated git worktree/branch, each running an independent \
copy of you with no visibility into the others' conversations - a real, recurring failure mode is \
two sessions independently deciding to build the same thing at the same time, wasting real time \
and money and creating a painful merge later. When another session is active, a turn will open \
with a bracketed block naming it and summarizing what it's currently running or just finished - \
read it before you dispatch anything. If what you're about to start overlaps with what's in that \
block, do not dispatch it: tell the stakeholder about the overlap and ask how to proceed (let the \
other session own it, wait for it to land and sync first, or explicitly diverge) instead of \
duplicating work you can see is already happening.

You work with three kinds of documents, and they serve different purposes - don't confuse them:
- {project_index_path}: the master map of every document in the project, at the repo root. \
If you are starting a conversation with no prior turns in context (a fresh session, e.g. \
after a reset), READ THIS FILE FIRST, before saying anything to the stakeholder about scope, \
status, or next steps. Never tell the stakeholder "we're starting fresh" or ask discovery \
questions you'd already know the answer to without checking it first. If it says the project \
hasn't started, that's a genuine fresh start and proceeding straight to discovery is correct.
- {project_status_path}: the durable, compact project record (what's built, decided, open) \
that the index points you to first. It is never wiped by a spec/chat reset, so it is the one \
place that survives across those resets - read it in full right after the index.
- {spec_path}: the live spec for what's being built right now. It can be reset to a blank \
slate between projects/phases, wiping its history along with the chat - that's expected and \
does not mean the project itself is new.
- `docs/` at the repo root: durable reference material (business plan, market/technical \
research, milestone specs) that PROJECT_STATUS.md summarizes but doesn't replace. Consult the \
relevant doc before making calls in its territory (e.g. read docs/BUSINESS_PLAN.md before \
answering a pricing/economics question). When you or a dev task produce a new durable \
reference doc, save it under `docs/` and add a line for it to {project_index_path} so it \
stays discoverable - an orphaned doc nobody can find is as good as lost.

Product source code lives in product directories at the REPO ROOT - navigate and point dev \
tasks there directly:
{repo_layout}
Your own working files (spec, chat history, task records, uploads) live under \
`{workspace_rel}/` - that directory is PM bookkeeping only; product code never goes \
there, and dev tasks should never be told to create it there.

{roadmap_guidance}
- Start a dev task (returns immediately - does not wait for it to finish):
  curl -s -X POST {tasks_base_url}{auth_header} -H "Content-Type: application/json" -d '{{"task": "<specific, actionable task description - what to build/fix and what done looks like>"}}'
  This returns JSON like {{"id": "...", "status": "running", ...}}. Remember the id.
- Check one task's status/result:
  curl -s {tasks_base_url}/<id>{auth_header}
- Check all tasks (e.g. if the stakeholder asks for status, or to catch up on anything that \
finished since you last checked):
  curl -s {tasks_base_url}{auth_header}
- Set or update this session's title + goal (the short label shown for it in the sessions list - \
keep it current as the work evolves):
  curl -s -X POST {session_meta_url}{auth_header} -H "Content-Type: application/json" -d '{{"title": "<≈2-5 word abbreviation>", "goal": "<one short sentence>"}}'

Run each of those commands exactly as shown, as a single plain command - no ; && | or \
appended echo/status-check, no wrapping in a subshell, nothing else added. This is a hard \
requirement, not a style preference: your Bash allowlist matches these commands literally, and \
any extra chaining makes the command no longer match, which gets it held for approval that \
nothing in this headless session can ever grant - so it will simply fail, every time, no matter \
how many times you retry it. If you want the exit status or extra diagnostics, make that a \
separate plain curl call, not a suffix on this one.

If a command still fails, report the literal error you got (stderr/exit code) rather than \
guessing at a cause - do not tell the stakeholder a permission prompt is pending their approval; \
there is no interactive prompt in this environment for them to approve, so that explanation is \
always wrong and just leaves them stuck. If you're unsure why something failed, say that plainly \
instead of inventing an explanation.

Your loop:
0. If this is a fresh session (no prior turns in context), read {project_index_path} then \
{project_status_path} before doing anything else - see above.
1. When the goal is vague, ask focused questions until you understand what to build.
1b. On your first substantive turn - once you actually understand what this session is for, not \
before (a vague session can wait) - set this session's title + goal via the meta curl above: a \
≈2-5 word title (e.g. "Estimate accuracy", "Sessions activity UI") and a one-sentence goal. \
Update the goal sentence whenever the session's direction materially changes, and refresh the \
title if the short label no longer fits. This is lightweight housekeeping - only touch it when \
title/goal are unset or have gone materially stale, not every turn. (The default \
"{default_session_name}" session keeps its fixed name and does not need auto-titling.)
2. Maintain the product spec by writing the FULL spec content to {spec_path} with your Write tool \
each time it changes - keep it current as understanding evolves. Whenever something durable \
happens - a slice ships, a decision gets made, direction changes - also rewrite the FULL content \
of {project_status_path} with your Write tool so it reflects current reality. Keep it a compact, \
high-signal summary (what's built, what's decided, what's open) rather than a full history dump.
3. Once a slice of the spec is concrete enough to build, check this turn's other-active-sessions \
block (if present - see above) for overlap, then start a dev task for it (above), tell the \
stakeholder briefly that you've kicked it off, and move on - don't wait for it in this turn.
4. When a task finishes (this turn, or the auto-invocation you get the instant it completes - see \
above) and "status" is "done", read its "result", summarize the outcome for the stakeholder, and \
then - in that same turn - decide what's next: if the current goal isn't fully built yet, start \
the next dev task yourself immediately rather than stopping to report status and waiting to be \
asked "what's next"; if the goal genuinely looks accomplished, say so plainly and let the \
stakeholder know there's nothing in flight. If "status" is "error", fix it yourself with a \
follow-up task when the fix is clear - don't make the stakeholder debug engineering problems - but \
don't retry the same failing approach indefinitely: if a task fails twice in a row on what looks \
like the same underlying problem, stop and explain the problem to the stakeholder instead of \
burning a third attempt blind. If "status" is "running", it's still working - just note that if \
asked.
5. Only ask the stakeholder something when it's genuinely their call: scope tradeoffs, ambiguous \
product decisions, or confirming a milestone is done. This is the only thing that should make you \
stop the autonomous loop in step 4 short of the goal being done or the safety limit kicking in.

Keep replies to the stakeholder conversational and concise. Do not narrate internal tool mechanics \
to them - summarize outcomes in plain language.{local_instructions}"""


# Wraps the deployment's own PM_INSTRUCTIONS.md / knowledge listing when present.
# Append-only by design: local content is added AFTER the full shared prompt and is
# framed as additional rules - a deployment can extend PM behavior (enterprise
# restrictions, private domain knowledge) but never replace the core loop, which is
# what keeps the PM Studio experience identical across every system running it.
LOCAL_INSTRUCTIONS_TEMPLATE = """

PROJECT-SPECIFIC LOCAL INSTRUCTIONS (from {local_dir}/ in this repo). These are \
additional rules and knowledge for THIS deployment only. They add to everything above; \
they never replace or relax it. Where they impose restrictions, follow them strictly, \
including in every dev task description you write:
{body}"""


def _local_instructions_block() -> str:
    """Builds the {local_instructions} slot: the deployment's PM_INSTRUCTIONS.md plus
    a pointer to its knowledge docs, or "" when neither exists."""
    parts = []
    if CONFIG.pm_instructions:
        parts.append(CONFIG.pm_instructions)
    if CONFIG.knowledge_files:
        listing = "\n".join(f"- {path}" for path in CONFIG.knowledge_files)
        parts.append(
            "Local knowledge docs (read the relevant one with your Read tool before "
            "making calls in its territory):\n" + listing
        )
    if not parts:
        return ""
    return LOCAL_INSTRUCTIONS_TEMPLATE.format(
        local_dir=LOCAL_DIR_NAME, body="\n\n".join(parts)
    )


class PMAgent:
    """One PM conversation, scoped to a single session's worktree/branch. Its
    Bash allowlist is a literal match on that session's own /tasks/{id} URL, so
    the CLI's own allowlist enforcement makes it structurally impossible for a
    PM to dispatch dev work into another session's worktree."""

    def __init__(self, session: "Session", git_lock: threading.Lock) -> None:
        self.session_id = session.id
        self.product = session.product
        self.model = session.model
        self.repo_root = Path(session.worktree_path)
        # workspace_rel, not the primary checkout's absolute workspace_dir: this
        # session's workspace lives inside its OWN worktree.
        self.workspace_dir = self.repo_root / CONFIG.workspace_rel / "current"
        self.spec_path = self.workspace_dir / "SPEC.md"
        self.session_state_path = self.workspace_dir / "pm_session_id.txt"
        self.history_path = self.workspace_dir / "chat_history.json"
        self.uploads_dir = self.workspace_dir / "uploads"
        # Where a reset (or a terminate, called externally via _archive_current) copies
        # SPEC.md/chat_history.json before they're cleared - see gitsnapshot.ARCHIVE_PATH.
        self.archive_dir = self.repo_root / gitsnapshot.ARCHIVE_PATH / self.session_id
        # Deliberately at the repo root, not inside workspace_dir: resetting/archiving
        # workspace/current (to give the PM a blank spec/chat) must never wipe this -
        # it's what lets a fresh session catch up on prior project history. Root-level
        # (next to PROJECT_INDEX.md) so the stakeholder finds it without digging
        # through PM internals.
        self.project_status_path = self.repo_root / "PROJECT_STATUS.md"
        # Repo root, not workspace-scoped: the single map every doc (durable status,
        # live spec, docs/ reference material) is reachable from.
        self.project_index_path = self.repo_root / "PROJECT_INDEX.md"
        self.git_lock = git_lock

        tasks_base_url = f"{CONFIG.base_url}/tasks/{session.id}"
        # Session-scoped, exactly like tasks_base_url: the id is baked into the URL, so
        # the literal-prefix allowlist entry below structurally guarantees a PM can only
        # set its OWN session's title/goal, never another session's.
        session_meta_url = f"{CONFIG.base_url}/sessions/{session.id}/meta"

        roadmap_tools = ""
        if self.product:
            roadmap_guidance = ROADMAP_GUIDANCE_TEMPLATE.format(
                product=self.product,
                product_label=PRODUCTS.get(self.product, self.product),
                product_ids=", ".join(PRODUCTS),
                roadmap_base_url=ROADMAP_BASE_URL,
                auth_header=agent_auth_header(),
            )
            if CONFIG.trackers:
                roadmap_guidance += TRACKER_GUIDANCE_TEMPLATE.format(
                    product=self.product,
                    roadmap_base_url=ROADMAP_BASE_URL,
                    auth_header=agent_auth_header(),
                    tracker_summary=", ".join(
                        f"{t.label} — {t.provider}, projects "
                        + (", ".join(t.projects) or "none configured")
                        for t in CONFIG.trackers
                    ),
                )
            # POST is deliberately broad (any product) - suggesting work for another
            # product is the intended cross-product handoff. PATCH is scoped to this
            # session's own product only, the same literal-URL-prefix enforcement
            # tasks_base_url above relies on: it is structurally impossible for this
            # PM to reach into and mutate another product's existing items.
            roadmap_tools = (
                f"Bash(curl -s -X POST {ROADMAP_BASE_URL}/*) "
                f"Bash(curl -s -X PATCH {ROADMAP_BASE_URL}/{self.product}/*) "
            )
        else:
            roadmap_guidance = GENERAL_ROADMAP_GUIDANCE

        self.system_prompt = PM_SYSTEM_PROMPT_TEMPLATE.format(
            tasks_base_url=tasks_base_url,
            session_meta_url=session_meta_url,
            spec_path=self.spec_path,
            project_status_path=self.project_status_path,
            project_index_path=self.project_index_path,
            roadmap_guidance=roadmap_guidance,
            repo_layout=CONFIG.repo_layout,
            workspace_rel=CONFIG.workspace_rel,
            default_session_name=CONFIG.default_session_name,
            local_instructions=_local_instructions_block(),
            auth_header=agent_auth_header(),
        )
        self.allowed_tools = (
            f"Bash(curl -s -X POST {tasks_base_url}*) "
            f"Bash(curl -s {tasks_base_url}*) "
            f"Bash(curl -s -X POST {session_meta_url}*) "
            f"{roadmap_tools}"
            "Write Read WebSearch WebFetch"
        )

        # The Claude CLI's own resumable conversation id - unrelated to our
        # app-level session concept (worktree/branch), hence the distinct name.
        self.claude_session_id: str | None = None
        # Live activity signal (orthogonal to the session's git-lifecycle status):
        # True only while a `_run_turn` is executing, so the sessions list can show a
        # "Thinking…" indicator. Set/cleared in _run_turn; read by
        # SessionManager.activity_of. Set by SessionManager after construction so a
        # turn start/end can push a live sessions-websocket broadcast.
        self.turn_active = False
        self.on_activity_change: Callable[[], None] | None = None
        self._load_state()
        self._ensure_project_status_seed()

        # Serializes PM turns: a dev-task completion can auto-trigger a turn (see
        # handle_task_completion) at the same moment the stakeholder is mid-message, and
        # two concurrent `claude -p --resume <id>` calls against the same session id plus
        # unsynchronized chat_history.json writes would race. Every turn - manual or
        # auto - blocks on this, so they queue up and run one at a time instead of
        # corrupting each other. Separate from git_lock, which guards commits, not turns.
        self.pm_lock = threading.Lock()
        # Consecutive auto-triggered turns with no real stakeholder message between them -
        # see MAX_AUTO_CONTINUE_STREAK. Reset on every genuine handle_user_message call.
        self._auto_continue_streak = 0

    def set_model(self, model: str) -> None:
        """Live update, called by SessionManager.set_model - takes effect on the
        very next turn since _run_pm_turn reads self.model fresh each call."""
        self.model = model

    def _ensure_project_status_seed(self) -> None:
        """Guarantees {project_status_path} exists so the system prompt's Read \
        instruction never 404s - a brand-new project just gets an honest 'nothing yet'."""
        if not self.project_status_path.exists():
            self.project_status_path.parent.mkdir(parents=True, exist_ok=True)
            self.project_status_path.write_text(
                "# Project Status (durable — read this first)\n\n"
                "No project history yet. This is a brand-new project - proceed to discovery.\n"
            )

    def _load_state(self) -> None:
        if self.session_state_path.exists():
            self.claude_session_id = self.session_state_path.read_text().strip() or None

    def _save_state(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if self.claude_session_id:
            self.session_state_path.write_text(self.claude_session_id)

    def load_history(self) -> list[dict]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text())
        return []

    def last_message_role(self) -> str | None:
        """Role of the most recent persisted transcript entry, normalized to
        user/assistant/system/None, so SessionManager.activity_of can tell whether
        the PM has responded and is now awaiting the stakeholder ("waiting"). Reuses
        the same chat_history.json entries _run_turn writes - no second source of truth.
        The stored roles are user/pm/error/system; "pm" and "error" both mean the PM
        side spoke last (turn concluded, stakeholder's move), so both map to
        "assistant"."""
        history = self.load_history()
        if not history:
            return None
        role = history[-1].get("role")
        if role in ("pm", "error"):
            return "assistant"
        return role

    def _notify_activity_change(self) -> None:
        """Pings SessionManager (if wired) that this session's live activity may have
        changed - a PM turn just started or ended - so it can broadcast to the sessions
        page. Best-effort: an activity broadcast failing must never break a PM turn."""
        if self.on_activity_change is not None:
            try:
                self.on_activity_change()
            except Exception:
                pass

    def archive_current(self, reason: str) -> None:
        """Copies the current SPEC.md/chat_history.json into workspace/archive before
        they're cleared or the session's worktree is torn down. Copy only - never
        deletes here, so callers can commit the archive alongside whatever else they're
        about to change (a reset's own deletions, or a terminate's pre-merge snapshot)."""
        if not self.spec_path.exists() and not self.history_path.exists() and not self.uploads_dir.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.archive_dir / f"{timestamp}_{reason}"
        dest.mkdir(parents=True, exist_ok=True)
        if self.spec_path.exists():
            shutil.copy2(self.spec_path, dest / "SPEC.md")
        if self.history_path.exists():
            shutil.copy2(self.history_path, dest / "chat_history.json")
        if self.uploads_dir.exists():
            shutil.copytree(self.uploads_dir, dest / "uploads")

    def reset(self) -> None:
        """Archives the current spec/chat, then clears workspace/current and drops the
        Claude session pointer so the next turn starts a brand-new conversation with no
        memory of prior turns. PROJECT_STATUS.md, PROJECT_INDEX.md, and docs/ are never
        touched - they live outside workspace/current for exactly this reason."""
        with self.git_lock:
            self.archive_current("reset")
            for path in (self.spec_path, self.history_path, self.session_state_path):
                if path.exists():
                    path.unlink()
            if self.uploads_dir.exists():
                shutil.rmtree(self.uploads_dir)
            self.claude_session_id = None
            gitsnapshot.snapshot(
                f"PM session {self.session_id} reset (prior spec/chat archived)", self.repo_root
            )

    def _append_incoming_entry(
        self,
        text: str,
        attachments: list[str] | None = None,
        role: str = "user",
    ) -> None:
        """Persists just the incoming stakeholder/system entry to chat_history.json,
        called at the START of a turn (before _run_pm_turn) so a mid-turn reload's
        GET /history already contains the just-sent message instead of dropping it until
        the reply lands. Stamps the same epoch-seconds ts and handles attachments exactly
        as the reply write does; _append_reply_entry appends the reply once the turn
        completes. Both are serialized under pm_lock in _run_turn, so they can't race."""
        # Stamp every entry with an epoch-seconds ts as it's appended so the client can
        # interleave chat and dev-task cards into one chronological timeline on reload.
        # Purely additive: legacy entries lacking ts are treated as having no timestamp.
        history = self.load_history()
        entry = {"role": role, "text": text, "ts": time.time()}
        if attachments:
            entry["attachments"] = attachments
        history.append(entry)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(history, indent=2))

    def _append_reply_entry(self, final_event: dict) -> None:
        """Appends just the PM's reply entry (pm on pm_reply, error otherwise) after the
        turn completes and writes the file - the second half of what used to be one write.
        Runs after _append_incoming_entry under the same pm_lock, so the on-disk transcript
        ends up identical to the old single write: incoming entry then reply entry, same ts
        stamping and json.dumps(indent=2) format."""
        history = self.load_history()
        if final_event["type"] == "pm_reply":
            history.append({"role": "pm", "text": final_event["text"], "ts": time.time()})
        else:
            history.append({"role": "error", "text": final_event["message"], "ts": time.time()})
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(history, indent=2))

    def _save_attachments(self, attachments: list[dict]) -> list[Path]:
        """Decodes base64 image attachments onto disk under workspace/current/uploads
        so the PM can view them via its Read tool - see MAX_ATTACHMENTS_PER_TURN above
        for why this is the only way an image reaches a headless `claude -p` turn.
        Silently skips anything not a recognized image type or over the size cap,
        rather than failing the whole turn over one bad attachment."""
        saved: list[Path] = []
        for attachment in attachments[:MAX_ATTACHMENTS_PER_TURN]:
            mime = attachment.get("mime", "")
            ext = _EXTENSION_BY_MIME.get(mime)
            if ext is None:
                continue
            try:
                raw = base64.b64decode(attachment.get("data", ""), validate=True)
            except Exception:
                continue
            if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
                continue
            self.uploads_dir.mkdir(parents=True, exist_ok=True)
            path = self.uploads_dir / f"{uuid.uuid4().hex}{ext}"
            path.write_bytes(raw)
            saved.append(path)
        return saved

    def handle_user_message(
        self,
        text: str,
        attachments: list[dict] | None = None,
        other_sessions_context: str = "",
        roadmap_context: str = "",
    ) -> Iterator[dict]:
        """Runs one PM turn in response to a real stakeholder message. `attachments` are
        base64-encoded images from the chat UI (see _save_attachments) - written to disk
        and pointed at from the prompt text so the PM can Read them. `other_sessions_context`
        is server.py's live snapshot of what every other active session is doing - see
        _with_session_context. `roadmap_context` is server.py's per-turn roadmap snapshot
        (own product in full, others as a shallow digest) - see _with_roadmap_context."""
        self._auto_continue_streak = 0

        saved_paths = self._save_attachments(attachments or [])
        prompt_text = text
        if saved_paths:
            file_list = "\n".join(f"- {path}" for path in saved_paths)
            intro = (
                f"{text}\n\n" if text else ""
            ) + f"[The stakeholder attached {len(saved_paths)} image(s) with this message - use your Read tool to view them:]"
            prompt_text = f"{intro}\n{file_list}"
        prompt_text = self._with_session_context(prompt_text, other_sessions_context)
        prompt_text = self._with_roadmap_context(prompt_text, roadmap_context)

        yield from self._run_turn(
            prompt_text,
            history_text=text,
            history_role="user",
            history_attachments=[path.name for path in saved_paths],
            commit_message=f"PM turn: {(text or '[image]')[:72]}",
        )

    def handle_task_completion(
        self, task: dict, other_sessions_context: str = "", roadmap_context: str = ""
    ) -> Iterator[dict]:
        """Auto-triggered the instant a dev task this PM dispatched finishes (see
        server.py's task-registry subscription) - lets the PM react and keep the plan
        moving without the stakeholder having to come back and ask for a status check.
        Capped by MAX_AUTO_CONTINUE_STREAK so a stuck plan can't re-invoke itself
        unattended forever; at the cap the PM is told to stop dispatching and check in
        instead of being trusted to decide that on its own."""
        at_cap = self._auto_continue_streak >= MAX_AUTO_CONTINUE_STREAK
        self._auto_continue_streak += 1

        prompt = (
            f'[System: dev task {task["id"]} just finished with status "{task["status"]}" - '
            f'"{task["description"]}". Check its result via curl and tell the stakeholder what '
            "happened."
        )
        if at_cap:
            prompt += (
                " You've auto-continued several times in a row now with no stakeholder input - "
                "do NOT dispatch another task this turn. Summarize where things stand and ask the "
                "stakeholder a concrete question instead.]"
            )
        else:
            prompt += (
                " Then immediately continue the plan: if the goal isn't done yet, start the next "
                "task yourself - don't wait to be asked. Only stop to ask the stakeholder if it's "
                "genuinely their call.]"
            )
        prompt = self._with_session_context(prompt, other_sessions_context)
        prompt = self._with_roadmap_context(prompt, roadmap_context)

        label = f'Dev task {task["id"]} finished ({task["status"]}): {task["description"][:100]}'
        yield from self._run_turn(
            prompt,
            history_text=label,
            history_role="system",
            history_attachments=None,
            commit_message=f'PM auto-continue after dev task {task["id"]} ({task["status"]})',
        )

    @staticmethod
    def _with_session_context(prompt_text: str, other_sessions_context: str) -> str:
        """Prepends a live snapshot of other active sessions' work, when there is any -
        see sessions.py's describe_other_active_sessions. Kept out of the persisted chat
        history (only prompt_text, never history_text, carries it): it's per-turn plumbing
        the stakeholder doesn't need to read, not something they said or the PM decided."""
        if not other_sessions_context:
            return prompt_text
        return (
            "[Other PM sessions active right now, each its own git worktree/branch off "
            "this same repo, invisible to you unless told here - before dispatching any "
            "new dev task this turn, check whether it overlaps with something already "
            "running or just finished below. If it does, don't duplicate it: tell the "
            "stakeholder about the overlap and ask how to proceed instead of dispatching.\n"
            f"{other_sessions_context}\n]\n\n{prompt_text}"
        )

    @staticmethod
    def _with_roadmap_context(prompt_text: str, roadmap_context: str) -> str:
        """Prepends this turn's roadmap snapshot (own product in full depth, every
        other product as a shallow digest - see server.py's _roadmap_context_for and
        this class's ROADMAP_GUIDANCE_TEMPLATE for how to act on it). Kept out of
        persisted chat history for the same reason as _with_session_context: it's
        per-turn plumbing, not something the stakeholder said or the PM decided."""
        if not roadmap_context:
            return prompt_text
        return (
            "[Roadmap snapshot for this turn - see your system prompt for how to act on "
            f"it:\n{roadmap_context}\n]\n\n{prompt_text}"
        )

    def _run_turn(
        self,
        prompt_text: str,
        *,
        history_text: str,
        history_role: str,
        history_attachments: list[str] | None,
        commit_message: str,
    ) -> Iterator[dict]:
        """Runs one `claude -p` turn (which may itself dispatch a nested dev-agent call
        via Bash), persists it to the transcript, and yields UI events. Serialized by
        pm_lock so a manual message and an auto-continuation triggered by a task
        finishing can never run concurrently against the same resumable session."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        # Flip the live "thinking" signal on and broadcast before any work, so the
        # sessions list shows the indicator immediately; the finally guarantees it
        # clears even if the turn errors or the generator is closed early.
        self.turn_active = True
        self._notify_activity_change()
        try:
            yield {"type": "pm_working"}

            with self.pm_lock:
                # Persist the incoming entry BEFORE running the turn so a mid-turn reload
                # (GET /history) already shows the just-sent message; append only the reply
                # after the turn returns. Both writes stay serialized under pm_lock, exactly
                # as the single combined write was, so no history write can race.
                self._append_incoming_entry(
                    history_text, attachments=history_attachments, role=history_role
                )
                final_event = self._run_pm_turn(prompt_text)
                self._append_reply_entry(final_event)
                with self.git_lock:
                    gitsnapshot.snapshot(commit_message, self.repo_root)
            yield final_event
        finally:
            self.turn_active = False
            self._notify_activity_change()

    def _run_pm_turn(self, text: str) -> dict:
        command = [
            "claude",
            "-p", text,
            "--output-format", "json",
            "--system-prompt", self.system_prompt,
            "--allowedTools", self.allowed_tools,
            "--model", self.model,
        ]
        if self.claude_session_id:
            command += ["--resume", self.claude_session_id]

        try:
            proc = subprocess.run(
                command,
                # Repo root, not workspace_dir: product sources live at the root, so
                # relative paths in the PM's reads and in dev-task descriptions
                # resolve against the real source tree, not the PM's bookkeeping
                # folder.
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=PM_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"type": "pm_error", "message": f"PM agent timed out after {PM_TIMEOUT_SECONDS}s."}
        except FileNotFoundError:
            return {"type": "pm_error", "message": "The `claude` CLI is not installed or not on PATH."}

        stdout = proc.stdout.strip()
        if not stdout:
            return {
                "type": "pm_error",
                "message": f"PM agent produced no output (exit {proc.returncode}). "
                           f"stderr: {proc.stderr.strip()[:2000]}",
            }

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {"type": "pm_error", "message": stdout[:4000]}

        if data.get("session_id"):
            self.claude_session_id = data["session_id"]
            self._save_state()

        if data.get("is_error") or proc.returncode != 0:
            return {
                "type": "pm_error",
                "message": data.get("result") or f"exit code {proc.returncode}",
                # Carried even on failure: a turn that errored still cost tokens.
                "agent_usage": agent_usage(data),
            }

        return {
            "type": "pm_reply",
            "text": data.get("result", ""),
            # Measured spend for this turn, for cost attribution (see costing.py).
            # Previously discarded.
            "agent_usage": agent_usage(data),
        }
