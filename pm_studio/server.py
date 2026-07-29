import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse

from .models import list_models
from .portfolio import (
    DEFAULT_CATCH_ALL_PROJECT,
    DEFAULT_MAINTENANCE_GOAL,
    DEFAULT_MAINTENANCE_INITIATIVE,
    PortfolioError,
    PortfolioStore,
)
from .roadmap import PRODUCTS, RoadmapStore
from .sessions import DEFAULT_SESSION_ID, SessionManager, SessionRuntime

STATIC_DIR = Path(__file__).parent / "static"

# Maps each open chat connection to a lock guarding its sends. Two producers can push
# to the same connection - the normal request/response turn in _run_chat_ws, and an
# auto-continuation broadcast in _auto_continue_pm firing from a dev-task completion at
# an arbitrary time - and concurrent `send_text` calls on one websocket aren't safe, so
# every send (both paths) goes through this lock.
chat_ws_clients: dict[str, dict[WebSocket, asyncio.Lock]] = {}
task_ws_clients: dict[str, set[WebSocket]] = {}
session_ws_clients: set[WebSocket] = set()
roadmap_ws_clients: set[WebSocket] = set()
_main_loop: asyncio.AbstractEventLoop | None = None

sessions = SessionManager()
roadmap_store = RoadmapStore()
portfolio_store = PortfolioStore()


def _roadmap_context_for(session_id: str) -> str:
    """Builds the roadmap block injected into a PM's turn: full depth on its own
    product, a one-line digest of every other product for general awareness only. A
    session with no pinned product (e.g. the default session) gets the shallow digest
    of every product and nothing deep - see sessions.py's Session.product."""
    session = sessions.get(session_id)
    product = session.product if session is not None else None
    if product:
        own = roadmap_store.describe_own_product(product)
        others = roadmap_store.describe_other_products(product)
        if others:
            return f"{own}\n\nOther products (brief, for awareness only):\n{others}"
        return own
    return roadmap_store.describe_other_products("")


def _public_session(session) -> dict:
    """The session payload the sessions list page consumes: the persisted Session plus
    a derived `activity` key (live working/waiting/idle - see SessionManager.activity_of).
    The single place that shape is built, so GET /sessions and every session-websocket
    broadcast the page reads stay consistent. `activity` is never persisted."""
    data = session.to_dict()
    data["activity"] = sessions.activity_of(session.id)
    return data


def _enrich_session_event(event: dict) -> dict:
    """Adds live `activity` to a broadcast event's session payload, matching
    _public_session's shape, so the sessions page's websocket events carry the same
    activity signal as its initial GET /sessions fetch."""
    session = event.get("session")
    if not isinstance(session, dict) or "id" not in session:
        return event
    return {**event, "session": {**session, "activity": sessions.activity_of(session["id"])}}


def _get_runtime(session_id: str) -> SessionRuntime:
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Unknown or inactive session: {session_id}")
    return runtime


def _wire_task_broadcast(session_id: str) -> None:
    """Subscribes a session's TaskRegistry to the websocket fan-out the moment
    it exists - at startup for every session already in the registry, and
    on-the-fly for sessions created afterwards via the session-update broadcast."""
    runtime = sessions.get_runtime(session_id)
    if runtime is not None:
        runtime.task_registry.subscribe(lambda task: _on_task_update(session_id, task))


def _on_session_update(event: dict) -> None:
    if event.get("type") == "session_created":
        _wire_task_broadcast(event["session"]["id"])
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_session_update(event))


async def _broadcast_session_update(event: dict) -> None:
    message = json.dumps(_enrich_session_event(event))
    dead = []
    for ws in session_ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session_ws_clients.discard(ws)


def _on_task_update(session_id: str, task: dict) -> None:
    # Called from a dev-agent's background thread - hop onto the event loop thread.
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_task_update(session_id, task))
        # A dev task starting/finishing changes the session's live activity, so push an
        # enriched session event to the sessions page too (it flips the card between
        # "Running dev task" and idle/waiting without a manual reload).
        session = sessions.get(session_id)
        if session is not None:
            _main_loop.call_soon_threadsafe(
                asyncio.create_task,
                _broadcast_session_update({"type": "session_updated", "session": session.to_dict()}),
            )
    # Only ordinary dev tasks re-engage the PM - a merge-conflict resolution isn't part
    # of its own plan (see tasks.py's "kind" field) and finishing one shouldn't make it
    # start narrating an unrelated merge to the stakeholder.
    if task.get("kind") == "dev" and task.get("status") in ("done", "error"):
        threading.Thread(target=_auto_continue_pm, args=(session_id, task), daemon=True).start()


async def _broadcast_task_update(session_id: str, task: dict) -> None:
    message = json.dumps({"type": "task_update", "task": task})
    clients = task_ws_clients.get(session_id, set())
    dead = []
    for ws in clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def _on_roadmap_update(event: dict) -> None:
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_roadmap_update(event))


async def _broadcast_roadmap_update(event: dict) -> None:
    message = json.dumps(event)
    dead = []
    for ws in roadmap_ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        roadmap_ws_clients.discard(ws)


def _auto_continue_pm(session_id: str, task: dict) -> None:
    """Runs on its own thread (spawned from the dev task's own completion callback,
    already off the event loop) so the PM reacts to a finished task without the
    stakeholder having to send anything. Unlike a normal turn, nothing is waiting on a
    request/response websocket loop for this, so results are pushed to every open chat
    connection for the session instead of a single one."""
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        return
    try:
        other_ctx = sessions.describe_other_active_sessions(session_id)
        roadmap_ctx = _roadmap_context_for(session_id)
        for event in runtime.pm_agent.handle_task_completion(task, other_ctx, roadmap_ctx):
            _broadcast_chat_event_threadsafe(session_id, {**event, "auto": True})
    except Exception as exc:
        _broadcast_chat_event_threadsafe(
            session_id, {"type": "pm_error", "message": str(exc), "auto": True}
        )


def _broadcast_chat_event_threadsafe(session_id: str, event: dict) -> None:
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_chat_event(session_id, event))


async def _broadcast_chat_event(session_id: str, event: dict) -> None:
    message = json.dumps(event)
    clients = chat_ws_clients.get(session_id, {})
    dead = []
    for ws, lock in list(clients.items()):
        try:
            async with lock:
                await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    for session_id in list(sessions.runtimes):
        _wire_task_broadcast(session_id)
    sessions.subscribe(_on_session_update)
    roadmap_store.subscribe(_on_roadmap_update)
    # Portfolio edits ride the roadmap socket: anything watching the board already
    # cares when a project is re-parented or an initiative closes.
    portfolio_store.subscribe(_on_roadmap_update)
    yield


app = FastAPI(lifespan=lifespan)


# ---- session picker / lifecycle ----

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "sessions.html")


@app.get("/sessions")
def list_sessions() -> list[dict]:
    return [_public_session(s) for s in sessions.list_sessions()]


@app.post("/sessions")
def create_session(payload: dict = Body(default={})) -> dict:
    name = (payload.get("name") or "").strip() or None
    product = (payload.get("product") or "").strip() or None
    model = (payload.get("model") or "").strip() or None
    try:
        session = sessions.create(name, product, model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    # _public_session so the response carries live `activity` (working/waiting/idle),
    # matching GET /sessions and the websocket - the chat page reads activity.reason
    # here to tell whether the PM is mid-turn ("thinking") on a mid-turn reload.
    return _public_session(session)


@app.post("/sessions/{session_id}/model")
def set_session_model(session_id: str, payload: dict = Body(...)) -> dict:
    """Changes which Claude model this session's PM turns and dev tasks run on,
    live - see SessionManager.set_model."""
    model = (payload.get("model") or "").strip()
    try:
        session = sessions.set_model(session_id, model)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/meta")
def set_session_meta(session_id: str, payload: dict = Body(...)) -> dict:
    """Sets the PM-maintained title and/or goal for this session - the self-updating
    identity shown in the sessions list (see SessionManager.set_meta). Reuses
    _public_session so the response carries live activity + the new title/goal, the
    same shape the sessions page consumes from GET /sessions and the websocket."""
    title = payload.get("title")
    goal = payload.get("goal")
    try:
        session = sessions.set_meta(session_id, title=title, goal=goal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _public_session(session)


@app.get("/models")
def get_models() -> list[dict]:
    return list_models()


@app.post("/sessions/{session_id}/merge")
def merge_session(session_id: str) -> dict:
    try:
        session = sessions.merge(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/cleanup")
def cleanup_session(session_id: str) -> dict:
    try:
        session = sessions.cleanup(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    try:
        session = sessions.delete(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/archive")
def archive_session(session_id: str) -> dict:
    """Hides the session from the default session list. Purely a visibility flag -
    doesn't touch the session's lifecycle status, worktree, or branch."""
    try:
        session = sessions.archive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/unarchive")
def unarchive_session(session_id: str) -> dict:
    try:
        session = sessions.unarchive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/sync")
def sync_session(session_id: str) -> dict:
    """Merges main into this session's own worktree, so it doesn't drift far enough
    from main to make its eventual terminate-time merge painful."""
    try:
        session = sessions.sync(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/terminate")
def terminate_session(session_id: str) -> dict:
    """Merges the session into main, archives its final spec/chat, then removes the
    worktree - the only way a non-default session ends. Runs in the background;
    poll /sessions/{id} or watch /ws/sessions for the outcome."""
    try:
        session = sessions.terminate(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.websocket("/ws/sessions")
async def sessions_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session_ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        session_ws_clients.discard(websocket)


# ---- session-scoped pages ----

@app.get("/chat/{session_id}")
def chat_page(session_id: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/dashboard/{session_id}")
def dashboard_page(session_id: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


# ---- roadmap board (cross-session, cross-product - not scoped to one session) ----

def _validated_project_id(raw) -> str | None:
    """Passes through None ("no change") and "" ("detach"), but rejects an unknown id -
    a change pointing at a project that doesn't exist would silently fall out of every
    rollup."""
    if raw is None:
        return None
    project_id = str(raw).strip()
    if project_id and not portfolio_store.project_exists(project_id):
        raise HTTPException(status_code=400, detail=f"Unknown project: {project_id}")
    return project_id


def _resolve_project_id(payload: dict) -> str | None:
    """The parent project for a new change.

    When the caller names one, it is validated. When it doesn't, the change lands in
    the catch-all project if the deployment has declared one - that is what keeps
    unplanned work aligned without making every creation path ask about strategy
    first. A deployment not using the work model has no catch-all, and changes simply
    carry no project, exactly as before.
    """
    explicit = _validated_project_id(payload.get("project_id"))
    if explicit:
        return explicit
    return portfolio_store.catch_all_project_id


# ---- work model: goals, initiatives, projects ----


@app.get("/portfolio")
def portfolio_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/portfolio/data")
def portfolio_data() -> dict:
    """Goals, initiatives, projects, the catch-all id, and the unaligned report."""
    return {
        **portfolio_store.snapshot(),
        "unassigned_changes": roadmap_store.unassigned_items(),
    }


@app.post("/portfolio/bootstrap")
def portfolio_bootstrap(payload: dict = Body(default={})) -> dict:
    """Declares the maintenance goal + always-open initiative + catch-all project.

    Goals and initiatives are never auto-created, but the catch-all project needs a
    parent - so this trio is declared once, explicitly, by whoever sets the instance
    up. Idempotent."""
    return portfolio_store.ensure_maintenance_scaffold(
        goal_title=(payload.get("goal_title") or "").strip() or DEFAULT_MAINTENANCE_GOAL,
        initiative_title=(payload.get("initiative_title") or "").strip()
        or DEFAULT_MAINTENANCE_INITIATIVE,
        project_title=(payload.get("project_title") or "").strip()
        or DEFAULT_CATCH_ALL_PROJECT,
    )


@app.post("/portfolio/goals")
def create_goal(payload: dict = Body(...)) -> dict:
    try:
        goal = portfolio_store.create_goal(
            title=payload.get("title") or "", description=payload.get("description") or ""
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return goal.to_dict()


@app.patch("/portfolio/goals/{goal_id}")
def update_goal(goal_id: str, payload: dict = Body(default={})) -> dict:
    try:
        goal = portfolio_store.update_goal(
            goal_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return goal.to_dict()


@app.delete("/portfolio/goals/{goal_id}")
def delete_goal(goal_id: str) -> dict:
    try:
        portfolio_store.delete_goal(goal_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/initiatives")
def create_initiative(payload: dict = Body(...)) -> dict:
    try:
        initiative = portfolio_store.create_initiative(
            title=payload.get("title") or "",
            description=payload.get("description") or "",
            goal_ids=payload.get("goal_ids"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return initiative.to_public_dict()


@app.patch("/portfolio/initiatives/{initiative_id}")
def update_initiative(initiative_id: str, payload: dict = Body(default={})) -> dict:
    try:
        initiative = portfolio_store.update_initiative(
            initiative_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
            goal_ids=payload.get("goal_ids"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return initiative.to_public_dict()


@app.delete("/portfolio/initiatives/{initiative_id}")
def delete_initiative(initiative_id: str) -> dict:
    try:
        portfolio_store.delete_initiative(initiative_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/projects")
def create_project(payload: dict = Body(...)) -> dict:
    try:
        project = portfolio_store.create_project(
            title=payload.get("title") or "",
            description=payload.get("description") or "",
            initiative_id=(payload.get("initiative_id") or "").strip() or None,
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return project.to_public_dict()


@app.patch("/portfolio/projects/{project_id}")
def update_project(project_id: str, payload: dict = Body(default={})) -> dict:
    """`initiative_id: ""` deliberately makes the project unaligned; omitting the key
    leaves its parent alone."""
    raw_initiative = payload.get("initiative_id")
    try:
        project = portfolio_store.update_project(
            project_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
            initiative_id=(raw_initiative or "").strip() or None,
            clear_initiative=raw_initiative is not None and not str(raw_initiative).strip(),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return project.to_public_dict()


@app.delete("/portfolio/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    try:
        # The roadmap store owns changes, so the count is passed in rather than having
        # portfolio.py reach across into it.
        portfolio_store.delete_project(
            project_id, change_count=roadmap_store.count_by_project(project_id)
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.get("/roadmap")
def roadmap_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "roadmap.html")


@app.get("/roadmap/data")
def roadmap_data() -> dict:
    """The board's dataset, grouped by product (the original shape, unchanged).

    `portfolio` is additive: the board ships it so the page can re-group into the
    initiative lens locally as websocket events arrive, without refetching the whole
    board on every keystroke elsewhere. Existing consumers can ignore it.
    """
    return {
        "products": PRODUCTS,
        "items": roadmap_store.list_all(),
        "portfolio": {
            "initiatives": portfolio_store.list_initiatives(),
            "projects": portfolio_store.list_projects(),
            "catch_all_project_id": portfolio_store.catch_all_project_id,
        },
    }


@app.get("/roadmap/by-initiative")
def roadmap_by_initiative() -> dict:
    """The same changes under the other lens: Initiative -> Project -> Change.

    One dataset, two pivots. Nothing is dropped when you switch - projects with no
    initiative, and changes with no project, come back in a trailing group with
    `initiative: null` rather than vanishing.
    """
    changes = [item for items in roadmap_store.list_all().values() for item in items]
    return {
        "pivot": "initiative",
        "products": PRODUCTS,
        "groups": portfolio_store.group_changes_by_initiative(changes),
    }


@app.get("/roadmap/{product}")
def list_roadmap_items(product: str) -> list[dict]:
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    return roadmap_store.list_product(product)


@app.post("/roadmap/{product}/items")
def create_roadmap_item(product: str, payload: dict = Body(...)) -> dict:
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    try:
        item = roadmap_store.create(
            product=product,
            title=title,
            description=(payload.get("description") or "").strip(),
            bucket=payload.get("bucket") or "later",
            status=payload.get("status") or "pending",
            origin_product=(payload.get("origin_product") or "").strip() or None,
            owner=(payload.get("owner") or "").strip() or None,
            project_id=_resolve_project_id(payload),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return item.to_dict()


@app.patch("/roadmap/{product}/items/{item_id}")
def update_roadmap_item(product: str, item_id: str, payload: dict = Body(default={})) -> dict:
    existing = roadmap_store.get(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    # Enforced on the SOURCE product's path segment, same as any other PATCH here -
    # this is what makes a move safe to grant through a PM's existing own-product-only
    # PATCH allowlist (see agent.py): it can only move items it currently owns, never
    # reach into another product's board to pull one out.
    if existing.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{existing.product}', not '{product}'",
        )

    move_to = (payload.get("move_to_product") or "").strip() or None
    if move_to:
        if move_to not in PRODUCTS:
            raise HTTPException(status_code=400, detail=f"Unknown product: {move_to}")
        try:
            item = roadmap_store.move(
                item_id,
                to_product=move_to,
                triaged=bool(payload.get("triaged", False)),
                bucket=payload.get("bucket"),
                status=payload.get("status"),
                title=payload.get("title"),
                description=payload.get("description"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return item.to_dict()

    item = roadmap_store.update(
        item_id,
        bucket=payload.get("bucket"),
        status=payload.get("status"),
        triaged=payload.get("triaged"),
        title=payload.get("title"),
        description=payload.get("description"),
        # None = no change; "" = clear external ownership (see RoadmapStore.update).
        owner=payload.get("owner"),
        # Same convention: "" detaches the change from its project.
        project_id=_validated_project_id(payload.get("project_id")),
    )
    return item.to_dict()


@app.delete("/roadmap/{product}/items/{item_id}")
def delete_roadmap_item(product: str, item_id: str) -> dict:
    existing = roadmap_store.get(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    if existing.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{existing.product}', not '{product}'",
        )
    roadmap_store.delete(item_id)
    return {"status": "deleted"}


@app.websocket("/ws/roadmap")
async def roadmap_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    roadmap_ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        roadmap_ws_clients.discard(websocket)


# ---- session-scoped data ----

@app.get("/history/{session_id}")
def history(session_id: str) -> list[dict]:
    return _get_runtime(session_id).pm_agent.load_history()


@app.get("/chat/{session_id}/uploads/{filename}")
def get_attachment(session_id: str, filename: str) -> FileResponse:
    path = _get_runtime(session_id).pm_agent.uploads_dir / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(path)


@app.post("/chat/{session_id}/reset")
def reset_chat(session_id: str) -> dict:
    """Archives the current spec/chat, then clears them and drops the Claude session
    pointer so the next turn starts a brand-new conversation. PROJECT_STATUS.md,
    PROJECT_INDEX.md, and docs/ are untouched."""
    _get_runtime(session_id).pm_agent.reset()
    return {"status": "reset"}


@app.post("/tasks/{session_id}")
def create_task(session_id: str, payload: dict = Body(...)) -> dict:
    description = (payload.get("task") or "").strip()
    if not description:
        return {"error": "task description required"}
    return _get_runtime(session_id).task_registry.start_task(description)


@app.get("/tasks/{session_id}")
def list_tasks(session_id: str) -> list[dict]:
    return _get_runtime(session_id).task_registry.list_tasks()


@app.get("/tasks/{session_id}/{task_id}")
def get_task(session_id: str, task_id: str) -> dict:
    task = _get_runtime(session_id).task_registry.get_task(task_id)
    return task if task is not None else {"error": "not found"}


async def _run_tasks_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    task_ws_clients.setdefault(session_id, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        task_ws_clients.get(session_id, set()).discard(websocket)


@app.websocket("/ws/tasks/{session_id}")
async def tasks_ws(websocket: WebSocket, session_id: str) -> None:
    await _run_tasks_ws(websocket, session_id)


async def _run_chat_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Unknown or inactive session: {session_id}"})
        )
        await websocket.close()
        return

    loop = asyncio.get_event_loop()
    # Registered so a dev-task completion firing while this connection is just sitting
    # idle on receive_text() can still push the PM's auto-continuation reply here - see
    # _broadcast_chat_event. The lock is shared with that path so the two never send
    # concurrently on the same websocket.
    send_lock = asyncio.Lock()
    chat_ws_clients.setdefault(session_id, {})[websocket] = send_lock
    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            text = (payload.get("text") or "").strip()
            attachments = payload.get("attachments") or []
            if not text and not attachments:
                continue

            queue: asyncio.Queue = asyncio.Queue()

            def produce(user_text: str = text, user_attachments: list = attachments) -> None:
                try:
                    other_ctx = sessions.describe_other_active_sessions(session_id)
                    roadmap_ctx = _roadmap_context_for(session_id)
                    for event in runtime.pm_agent.handle_user_message(
                        user_text, user_attachments, other_ctx, roadmap_ctx
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception as exc:  # surface dev/PM errors to the chat instead of dying silently
                    loop.call_soon_threadsafe(
                        queue.put_nowait, {"type": "error", "message": str(exc)}
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            future = loop.run_in_executor(None, produce)
            while True:
                event = await queue.get()
                if event is None:
                    break
                async with send_lock:
                    await websocket.send_text(json.dumps(event))
            await future
    except WebSocketDisconnect:
        pass
    finally:
        chat_ws_clients.get(session_id, {}).pop(websocket, None)


@app.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    await _run_chat_ws(websocket, session_id)


# ---- backward-compat aliases to the default session ----
#
# `/tasks/{task_id}` (single lookup) is deliberately NOT aliased - it would be
# indistinguishable from the new `/tasks/{session_id}` (list) route at the same
# URL shape. Everything else here has a zero-segment legacy form that doesn't
# collide with the new one-segment session-scoped routes.

@app.get("/dashboard")
def dashboard_alias() -> RedirectResponse:
    return RedirectResponse(f"/dashboard/{DEFAULT_SESSION_ID}")


@app.get("/history")
def history_alias() -> list[dict]:
    return history(DEFAULT_SESSION_ID)


@app.post("/tasks")
def create_task_alias(payload: dict = Body(...)) -> dict:
    return create_task(DEFAULT_SESSION_ID, payload)


@app.get("/tasks")
def list_tasks_alias() -> list[dict]:
    return list_tasks(DEFAULT_SESSION_ID)


@app.websocket("/ws/chat")
async def chat_ws_alias(websocket: WebSocket) -> None:
    await _run_chat_ws(websocket, DEFAULT_SESSION_ID)


@app.websocket("/ws/tasks")
async def tasks_ws_alias(websocket: WebSocket) -> None:
    await _run_tasks_ws(websocket, DEFAULT_SESSION_ID)
