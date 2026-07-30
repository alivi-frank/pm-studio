import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import mailer
from .accounts import (
    AGENT_HEADER_NAME,
    LOGIN_TTL_SECONDS,
    ROLE_LABELS,
    SESSION_COOKIE_NAME,
    AccountError,
    AccountStore,
    User,
    agent_principal,
    is_agent_token,
)
from .authz import (
    CAPABILITY_LABELS,
    AuditLog,
    Capability,
    capabilities_of,
    describe_matrix,
    role_has,
)
from .config import CONFIG
from .costing import (
    KIND_DEV_TASK,
    KIND_PM_TURN,
    CostingError,
    CostingStore,
    current_week,
)
from .models import list_models
from .portfolio import (
    DEFAULT_CATCH_ALL_PROJECT,
    DEFAULT_MAINTENANCE_GOAL,
    DEFAULT_MAINTENANCE_INITIATIVE,
    PortfolioError,
    PortfolioStore,
)
from .roadmap import PRODUCTS, RoadmapItem, RoadmapStore, TicketAlreadyLinked
from .sessions import DEFAULT_SESSION_ID, SessionManager, SessionRuntime
from .trackers import TrackerError, TrackerStore, normalize_key

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
# The synced Jira/ADO ticket catalog. Constructed unconditionally; with no [[trackers]]
# configured it holds nothing and every ticket join below resolves to None, so the board
# behaves exactly as it did before the feature existed.
tracker_store = TrackerStore()
# Constructed in both modes (it is empty and untouched in personal mode) so nothing
# has to branch on mode just to reach the store.
account_store = AccountStore()
audit_log = AuditLog()
costing_store = CostingStore(
    blended_rate=CONFIG.costing.blended_rate,
    default_capacity_hours=CONFIG.costing.default_capacity_hours,
    weights=CONFIG.costing.weights,
    currency=CONFIG.costing.currency,
)

# task_id -> the user who dispatched it. A dev task's token spend is only known when it
# finishes, by which time the request that started it is long gone, so the dispatcher is
# remembered here. In-process is enough: a task outliving the process is marked errored
# on restart anyway (see TaskRegistry's startup sweep).
_dev_task_dispatchers: dict[str, str] = {}

# Reachable without a session cookie. Everything else needs one when enterprise mode
# is on; in personal mode this set is irrelevant because auth is skipped entirely.
_PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/setup",
        "/accept-invite",
        "/auth/login",
        "/auth/setup",
        "/auth/invite",
        "/auth/accept-invite",
        "/auth/me",
        "/favicon.ico",
    }
)


def _lookup_ticket(tracker_id: str, key: str) -> dict | None:
    ticket = tracker_store.lookup(tracker_id, key)
    return ticket.to_dict() if ticket is not None else None


def _with_ticket(item: dict) -> dict:
    """Joins a roadmap change to its linked ticket for the wire.

    The item stores only `tracker_id` + `ticket_key` (see roadmap.py); everything the card
    renders - canonical type for the colour, the tracker's own type name for the label,
    title, state, URL - comes from the synced catalog here. A link whose ticket is not in
    the catalog yet returns `unresolved: true` rather than being dropped, so the UI can say
    "linked, not seen in the last sync" instead of silently showing no badge at all.
    """
    tracker_id, key = item.get("tracker_id"), item.get("ticket_key")
    if not tracker_id or not key:
        return {**item, "ticket": None}
    ticket = tracker_store.lookup(tracker_id, key)
    if ticket is None:
        config = CONFIG.tracker(tracker_id)
        return {
            **item,
            "ticket": {
                "tracker_id": tracker_id,
                "tracker_label": config.label if config else tracker_id,
                "provider": config.provider if config else "",
                "key": key,
                "type": "other",
                "raw_type": "",
                "title": "",
                "state": "",
                "url": "",
                "unresolved": True,
            },
        }
    config = CONFIG.tracker(tracker_id)
    return {
        **item,
        "ticket": {
            **ticket.to_dict(),
            "tracker_label": config.label if config else tracker_id,
            "unresolved": False,
        },
    }


def _roadmap_context_for(session_id: str) -> str:
    """Builds the roadmap block injected into a PM's turn: full depth on its own
    product, a one-line digest of every other product for general awareness only. A
    session with no pinned product (e.g. the default session) gets the shallow digest
    of every product and nothing deep - see sessions.py's Session.product."""
    session = sessions.get(session_id)
    product = session.product if session is not None else None
    if product:
        own = roadmap_store.describe_own_product(product, ticket_lookup=_lookup_ticket)
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
    if task.get("status") in ("done", "error"):
        usage = task.get("agent_usage") or {}
        if usage.get("cost_usd") or usage.get("input_tokens"):
            # Measured spend, recorded against the dispatcher but carrying no extra
            # labour weight of its own (the dispatch signal above already did that).
            _record_signal(
                None,
                KIND_DEV_TASK,
                session_id,
                usage={**usage, "cost_usd": usage.get("cost_usd", 0.0)},
                user_id="",
            )
        _dev_task_dispatchers.pop(task.get("id", ""), None)
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
        # Enrich here rather than in the store: the store deliberately knows nothing about
        # the tracker catalog, and the board needs the same joined shape from a websocket
        # event as it gets from GET /roadmap/data - otherwise a live edit would blank out
        # a card's ticket badge until the next full reload.
        if isinstance(event.get("item"), dict):
            event = {**event, "item": _with_ticket(event["item"])}
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
            # user_id="" - nobody was at the keyboard for an auto-continuation, so its
            # tokens count and its labour weight must not.
            _record_signal(
                None, KIND_PM_TURN, session_id, usage=event.get("agent_usage"), user_id=""
            )
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
    # Only started when a tracker is actually configured, so an unconfigured deployment
    # runs no extra thread at all.
    if tracker_store.is_configured:
        threading.Thread(target=_tracker_sync_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


# ---- authentication (enterprise mode only) ----
#
# Personal mode short-circuits every check below, so a deployment that never sets
# `[enterprise] mode` keeps the exact request path it has always had: no cookie, no
# login page, no roster. Enterprise mode is opt-in precisely because turning it on
# changes who may reach the dev agents.


def _wants_html(request: Request) -> bool:
    """A browser navigating to a page should be redirected to the login screen; an
    XHR or a curl call from a PM agent should get a 401 it can act on."""
    return "text/html" in request.headers.get("accept", "")


def _current_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def _require_user(request: Request) -> User:
    """In personal mode there is no user object at all - callers that need an identity
    (invites, the roster) are enterprise-only endpoints and say so."""
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _require(request: Request, capability: Capability) -> User | None:
    """The single authorization gate for every mutating endpoint.

    Returns None in personal mode - there is no identity to authorize, and the request
    proceeds exactly as it did before accounts existed. In enterprise mode it returns
    the acting user, so callers can hand it straight to the audit log.

    The 403 says what the capability *was*, not just "forbidden": a viewer who pokes at
    an endpoint should learn what role would have been needed.
    """
    if not CONFIG.is_enterprise:
        return None
    user = _require_user(request)
    if not role_has(user.role, capability):
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({user.role}) is not allowed to {CAPABILITY_LABELS[capability]}.",
        )
    return user


def _audit(actor: User | None, action: str, target: str = "", detail: str = "") -> None:
    """No-op in personal mode: a single trusted user acting alone is what the git
    snapshot history already records."""
    if actor is not None:
        audit_log.record(actor, action, target, detail)


def _require_enterprise() -> None:
    if not CONFIG.is_enterprise:
        raise HTTPException(
            status_code=404,
            detail="This instance runs in personal mode; enterprise features are off.",
        )


def _set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=LOGIN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if not CONFIG.is_enterprise:
        return await call_next(request)

    path = request.url.path
    # A PM agent's curl calls carry the process's agent token instead of a cookie -
    # without this the core loop (dispatch a dev task, update the board) would 401 the
    # moment enterprise mode was switched on.
    if is_agent_token(request.headers.get(AGENT_HEADER_NAME)):
        request.state.user = agent_principal()
    else:
        request.state.user = account_store.resolve_login(
            request.cookies.get(SESSION_COOKIE_NAME)
        )

    # First run: an enterprise instance with no admin yet can only go to setup.
    # Without this an operator who flips the mode flag would be locked out of their
    # own server with no way in.
    if account_store.needs_setup and path not in ("/setup", "/auth/setup", "/auth/me"):
        if _wants_html(request):
            return RedirectResponse("/setup")
        return JSONResponse({"detail": "This instance has not been set up yet."}, status_code=503)

    if path in _PUBLIC_PATHS or request.state.user is not None:
        return await call_next(request)

    if _wants_html(request):
        return RedirectResponse("/login")
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


async def _ws_reject(websocket: WebSocket, capability: Capability = "view") -> bool:
    """HTTP middleware never sees a websocket handshake, so every socket checks the
    cookie and its own capability itself. Returns True when the connection was refused.

    1008 is the policy-violation close code; the page treats it as "reload and you'll
    get the login screen". This matters most for the chat socket: sending a turn to a
    PM is `run_session`, so a viewer must not be able to skip the HTTP layer and drive
    an agent through the websocket instead.
    """
    if not CONFIG.is_enterprise:
        return False
    user = account_store.resolve_login(websocket.cookies.get(SESSION_COOKIE_NAME))
    if user is not None and role_has(user.role, capability):
        return False
    await websocket.close(code=1008)
    return True


# The shared navigation chrome, the only asset more than one page pulls in. Named
# routes rather than a StaticFiles mount so that the set of files this server will hand
# out stays an explicit list and no path can be traversed into.
@app.get("/static/nav.css")
def nav_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "nav.css", media_type="text/css")


@app.get("/static/nav.js")
def nav_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "nav.js", media_type="application/javascript")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/setup")
def setup_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/accept-invite")
def accept_invite_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "accept-invite.html")


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    """The one endpoint every page calls to learn what mode it is in and who it is.
    Deliberately public: in personal mode it answers `{"mode": "personal"}` and the
    pages hide all account UI."""
    user = _current_user(request)
    return {
        "mode": CONFIG.mode,
        "enterprise": CONFIG.is_enterprise,
        "needs_setup": CONFIG.is_enterprise and account_store.needs_setup,
        "user": user.to_public_dict() if user is not None else None,
        "roles": ROLE_LABELS,
        "smtp_configured": bool(CONFIG.smtp and CONFIG.smtp.is_usable),
        # So a page can hide controls that would only 403. Every one of these is
        # still enforced server-side, independently of what the UI chooses to show.
        "capabilities": capabilities_of(user.role) if user is not None else [],
        "capability_matrix": describe_matrix(),
    }


@app.post("/auth/setup")
def auth_setup(response: Response, payload: dict = Body(...)) -> dict:
    """First-run owner creation. The account that converts a personal instance to
    enterprise becomes its admin - see the `[enterprise]` docs."""
    _require_enterprise()
    try:
        user = account_store.create_owner(
            email=(payload.get("email") or ""),
            name=(payload.get("name") or ""),
            password=(payload.get("password") or ""),
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


@app.post("/auth/login")
def auth_login(response: Response, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    try:
        user = account_store.authenticate(
            email=(payload.get("email") or ""), password=(payload.get("password") or "")
        )
    except AccountError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    account_store.end_login(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@app.get("/auth/invite")
def auth_peek_invite(token: str) -> dict:
    """Lets the accept-invite page show who the invite is for before a password is
    typed. Consumes nothing."""
    _require_enterprise()
    try:
        invite = account_store.peek_invite(token)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"email": invite.email, "role": invite.role, "role_label": ROLE_LABELS.get(invite.role, invite.role)}


@app.post("/auth/accept-invite")
def auth_accept_invite(response: Response, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    try:
        user = account_store.accept_invite(
            token=(payload.get("token") or ""),
            name=(payload.get("name") or ""),
            password=(payload.get("password") or ""),
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


# ---- roster + invites (admin) ----


@app.get("/users")
def list_users(request: Request) -> list[dict]:
    _require_enterprise()
    _require(request, "manage_users")
    return account_store.list_users()


@app.post("/users/{user_id}/role")
def set_user_role(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    try:
        updated = account_store.set_role(user_id, (payload.get("role") or ""))
        _audit(actor, "user.role_changed", updated.email, f"now {updated.role}")
        return updated.to_public_dict()
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/users/{user_id}/status")
def set_user_status(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    status = (payload.get("status") or "").strip()
    if status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'disabled'")
    try:
        updated = account_store.set_status(user_id, status)
        _audit(actor, "user.status_changed", updated.email, status)
        return updated.to_public_dict()
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/invites")
def list_invites(request: Request) -> list[dict]:
    _require_enterprise()
    _require(request, "manage_users")
    return account_store.list_invites()


@app.post("/invites")
def create_invite(request: Request, payload: dict = Body(...)) -> dict:
    """Invites one address at a given role. Always returns the accept URL - if SMTP is
    configured we also mail it, but the link is what makes the flow work on a machine
    with no mail server (see mailer.send_invite)."""
    _require_enterprise()
    admin = _require(request, "manage_users")
    try:
        new_invite = account_store.invite(
            email=(payload.get("email") or ""),
            role=(payload.get("role") or "viewer"),
            invited_by=admin.id,
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accept_url = new_invite.accept_url(CONFIG.base_url)
    emailed = mailer.send_invite(
        CONFIG, new_invite.invite.email, admin.name, accept_url
    )
    return {
        "invite": new_invite.invite.to_public_dict(),
        "accept_url": accept_url,
        "emailed": emailed,
    }


@app.delete("/invites/{invite_id}")
def revoke_invite(invite_id: str, request: Request) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    try:
        invite = account_store.revoke_invite(invite_id)
        _audit(actor, "invite.revoked", invite.email)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "revoked"}


@app.get("/audit")
def read_audit(request: Request, limit: int = 200) -> list[dict]:
    """Who did what. Admin-only: it names people and quotes dispatched task text."""
    _require_enterprise()
    _require(request, "manage_users")
    return audit_log.tail(limit=limit)


@app.get("/people")
def people_page() -> FileResponse:
    """The admin roster screen: users, roles, invites."""
    return FileResponse(STATIC_DIR / "people.html")


# ---- session picker / lifecycle ----

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "sessions.html")


@app.get("/sessions")
def list_sessions() -> list[dict]:
    return [_public_session(s) for s in sessions.list_sessions()]


@app.post("/sessions")
def create_session(request: Request, payload: dict = Body(default={})) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    name = (payload.get("name") or "").strip() or None
    product = (payload.get("product") or "").strip() or None
    model = (payload.get("model") or "").strip() or None
    project_id = _validated_project_id(payload.get("project_id"))
    try:
        session = sessions.create(name, product, model, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.created", session.id, session.name)
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
def set_session_model(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Changes which Claude model this session's PM turns and dev tasks run on,
    live - see SessionManager.set_model."""
    _require(request, "run_session")
    model = (payload.get("model") or "").strip()
    try:
        session = sessions.set_model(session_id, model)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/meta")
def set_session_meta(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Sets the PM-maintained title and/or goal for this session - the self-updating
    identity shown in the sessions list (see SessionManager.set_meta). Reuses
    _public_session so the response carries live activity + the new title/goal, the
    same shape the sessions page consumes from GET /sessions and the websocket."""
    _require(request, "run_session")
    title = payload.get("title")
    goal = payload.get("goal")
    try:
        session = sessions.set_meta(session_id, title=title, goal=goal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _public_session(session)


@app.post("/sessions/{session_id}/project")
def set_session_project(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Points a session's work at a Project in the work model. `project_id: ""` clears
    it, after which the session's activity falls back to the catch-all project."""
    actor = _require(request, "run_session")
    project_id = _validated_project_id(payload.get("project_id"))
    try:
        session = sessions.set_project(session_id, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(actor, "session.project_set", session_id, project_id or "(cleared)")
    return _public_session(session)


# ---- time & cost attribution (admin only) ----


@app.get("/costing")
def costing_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "costing.html")


@app.get("/costing/roster")
def costing_roster(request: Request) -> dict:
    """Rates and capacities, joined to the user roster so the screen can show names.

    Admin-only via `view_cost`: this is compensation data, and it stays the narrowest
    grant in the matrix even though the roadmap it hangs off is visible to everyone.
    """
    _require(request, "view_cost")
    configured = {row["user_id"]: row for row in costing_store.list_roster()}
    people = account_store.list_users() if CONFIG.is_enterprise else [
        {"id": "local", "name": "This machine", "email": "", "role": "admin"}
    ]
    rows = []
    for person in people:
        entry = costing_store.entry_for(person["id"])
        rows.append(
            {
                "user_id": person["id"],
                "name": person.get("name"),
                "email": person.get("email"),
                "role": person.get("role"),
                "rate_per_hour": entry.rate_per_hour,
                "capacity_hours_per_week": entry.capacity_hours_per_week,
                "configured": person["id"] in configured,
            }
        )
    return {
        "currency": CONFIG.costing.currency,
        "blended_rate": CONFIG.costing.blended_rate,
        "default_capacity_hours": CONFIG.costing.default_capacity_hours,
        "rows": rows,
    }


@app.post("/costing/roster/{user_id}")
def set_costing_entry(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """`rate_per_hour: ""` (or null with clear_rate) drops back to the blended rate."""
    actor = _require(request, "view_cost")
    raw_rate = payload.get("rate_per_hour")
    clear_rate = raw_rate is not None and str(raw_rate).strip() == ""
    try:
        entry = costing_store.set_entry(
            user_id,
            rate_per_hour=None if clear_rate or raw_rate is None else float(raw_rate),
            capacity_hours_per_week=(
                float(payload["capacity_hours_per_week"])
                if payload.get("capacity_hours_per_week") not in (None, "")
                else None
            ),
            clear_rate=clear_rate,
        )
    except (CostingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Deliberately no rate in the audit detail - the log is readable by every admin.
    _audit(actor, "costing.entry_updated", user_id)
    return entry.to_dict()


@app.get("/costing/report")
def costing_report(request: Request, week: str | None = None) -> dict:
    """A week's distribution, plus the initiative rollup.

    Hours are an approximation by construction: a declared capacity split by signal
    share. What makes them usable is that they reconcile - they always sum to a real
    week - not that they are precise.
    """
    _require(request, "view_cost")
    target = (week or "").strip() or current_week()
    known = (
        [u["id"] for u in account_store.list_users()] if CONFIG.is_enterprise else ["local"]
    )
    try:
        report = costing_store.distribute_week(target, user_ids=known)
    except CostingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report["rollup"] = costing_store.rollup_to_initiatives(
        report["by_project"], portfolio_store
    )
    report["portfolio"] = {
        "projects": portfolio_store.list_projects(),
        "initiatives": portfolio_store.list_initiatives(),
    }
    if CONFIG.is_enterprise:
        report["user_names"] = {
            u["id"]: u.get("name") or u.get("email") for u in account_store.list_users()
        }
    else:
        report["user_names"] = {"local": "This machine"}
    return report


@app.post("/costing/override")
def set_costing_override(request: Request, payload: dict = Body(...)) -> dict:
    """Replaces one person's whole week.

    Whole-week rather than per-project on purpose: the distribution's useful property is
    that it sums to a real week, and overriding a single project in isolation would
    silently break that. The derived figures are kept alongside, so a report always
    shows what the system thought as well as what a human decided.
    """
    actor = _require(request, "view_cost")
    week = (payload.get("week") or "").strip() or current_week()
    user_id = (payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    hours = payload.get("hours_by_project")
    try:
        if hours is None:
            costing_store.clear_override(week, user_id)
        else:
            costing_store.set_override(week, user_id, hours)
    except (CostingError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "costing.override" if hours is not None else "costing.override_cleared",
           f"{week}/{user_id}")
    return {"status": "ok", "week": week, "user_id": user_id}


@app.get("/models")
def get_models() -> list[dict]:
    return list_models()


@app.post("/sessions/{session_id}/merge")
def merge_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.merge(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.merged", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/cleanup")
def cleanup_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.cleanup(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.cleaned_up", session_id)
    return session.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.delete(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.deleted", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/archive")
def archive_session(session_id: str, request: Request) -> dict:
    """Hides the session from the default session list. Purely a visibility flag -
    doesn't touch the session's lifecycle status, worktree, or branch."""
    _require(request, "manage_session_lifecycle")
    try:
        session = sessions.archive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/unarchive")
def unarchive_session(session_id: str, request: Request) -> dict:
    _require(request, "manage_session_lifecycle")
    try:
        session = sessions.unarchive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/sync")
def sync_session(session_id: str, request: Request) -> dict:
    """Merges main into this session's own worktree, so it doesn't drift far enough
    from main to make its eventual terminate-time merge painful."""
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.sync(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.synced", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/terminate")
def terminate_session(session_id: str, request: Request) -> dict:
    """Merges the session into main, archives its final spec/chat, then removes the
    worktree - the only way a non-default session ends. Runs in the background;
    poll /sessions/{id} or watch /ws/sessions for the outcome."""
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.terminate(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.terminated", session_id)
    return session.to_dict()


@app.websocket("/ws/sessions")
async def sessions_ws(websocket: WebSocket) -> None:
    if await _ws_reject(websocket):
        return
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

def _actor_id(actor: User | None) -> str:
    """Who to attribute activity to. In personal mode there is no identity, so a single
    stable id is used - a solo user still gets a useful token-cost report, and nothing
    has to branch on mode at every call site."""
    return actor.id if actor is not None else "local"


def _session_project_id(session_id: str) -> str | None:
    """Which Project a session's activity counts towards: its own if it has one, else
    the catch-all if the deployment declared one, else nothing. This mirrors how a
    change with no project lands in the catch-all - unplanned work is attributed rather
    than silently dropped."""
    session = sessions.get(session_id)
    if session is not None and session.project_id:
        return session.project_id
    return portfolio_store.catch_all_project_id


def _record_signal(
    actor: User | None,
    kind: str,
    session_id: str,
    usage: dict | None = None,
    user_id: str | None = None,
) -> None:
    """Records one activity signal. Failures are swallowed on purpose: cost
    bookkeeping must never be able to break a PM turn or a dispatch."""
    usage = usage or {}
    try:
        costing_store.record(
            user_id=user_id if user_id is not None else _actor_id(actor),
            kind=kind,
            project_id=_session_project_id(session_id),
            session_id=session_id,
            agent_cost_usd=usage.get("cost_usd", 0.0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
    except Exception:
        pass


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
def portfolio_bootstrap(request: Request, payload: dict = Body(default={})) -> dict:
    """Declares the maintenance goal + always-open initiative + catch-all project.

    Goals and initiatives are never auto-created, but the catch-all project needs a
    parent - so this trio is declared once, explicitly, by whoever sets the instance
    up. Idempotent."""
    actor = _require(request, "manage_roadmap")
    result = portfolio_store.ensure_maintenance_scaffold(
        goal_title=(payload.get("goal_title") or "").strip() or DEFAULT_MAINTENANCE_GOAL,
        initiative_title=(payload.get("initiative_title") or "").strip()
        or DEFAULT_MAINTENANCE_INITIATIVE,
        project_title=(payload.get("project_title") or "").strip()
        or DEFAULT_CATCH_ALL_PROJECT,
    )
    if result.get("created"):
        _audit(actor, "portfolio.bootstrapped", result["project_id"])
    return result


@app.post("/portfolio/goals")
def create_goal(request: Request, payload: dict = Body(...)) -> dict:
    _require(request, "manage_roadmap")
    try:
        goal = portfolio_store.create_goal(
            title=payload.get("title") or "", description=payload.get("description") or ""
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return goal.to_dict()


@app.patch("/portfolio/goals/{goal_id}")
def update_goal(goal_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    _require(request, "manage_roadmap")
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
def delete_goal(goal_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
    try:
        portfolio_store.delete_goal(goal_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/initiatives")
def create_initiative(request: Request, payload: dict = Body(...)) -> dict:
    _require(request, "manage_roadmap")
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
def update_initiative(initiative_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    _require(request, "manage_roadmap")
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
def delete_initiative(initiative_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
    try:
        portfolio_store.delete_initiative(initiative_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/projects")
def create_project(request: Request, payload: dict = Body(...)) -> dict:
    _require(request, "manage_roadmap")
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
def update_project(project_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    """`initiative_id: ""` deliberately makes the project unaligned; omitting the key
    leaves its parent alone."""
    _require(request, "manage_roadmap")
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
def delete_project(project_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
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


# ---- external trackers (Jira / Azure DevOps) ----
#
# Reads are open like every other read in this system (see authz.py's transparency note);
# triggering a sync is a `manage_roadmap` write because it costs an outbound API call
# against someone else's rate limit.


@app.get("/trackers")
def get_trackers() -> dict:
    """Configured trackers plus each one's last sync outcome.

    Never includes a token: the payload is assembled field by field in
    TrackerStore.describe() precisely so that adding a config field later cannot leak one
    by accident.
    """
    return tracker_store.describe()


@app.get("/trackers/tickets")
def search_tickets(q: str = "", tracker_id: str = "", limit: int = 50) -> dict:
    """Candidate tickets for the link picker, from the synced catalog only - this endpoint
    never calls out to a tracker, so typing in the picker cannot generate API traffic."""
    tickets = tracker_store.search(q, tracker_id, max(1, min(limit, 200)))
    # Which candidates are already taken, so the picker can grey them out instead of
    # letting someone choose one and only then be refused by the 1:1 check. A set of
    # tuples rather than a joined string: how a catalog key is spelled is trackers.py's
    # business, and duplicating that format here is what invites the two to drift apart.
    taken = {
        (tid, normalize_key(tid, key))
        for tid, key in roadmap_store.linked_ticket_refs()
    }
    for ticket in tickets:
        tid = ticket["tracker_id"]
        ticket["linked"] = (tid, normalize_key(tid, ticket["key"])) in taken
    return {"tickets": tickets}


@app.post("/trackers/sync")
def sync_trackers(request: Request, payload: dict = Body(default={})) -> dict:
    """Kicks off a sync and returns immediately.

    Runs on a daemon thread, like merges and dev tasks (see sessions.py) - a Jira with
    thousands of issues would otherwise block the event loop for the whole pull. The board
    learns it finished from the roadmap websocket.
    """
    _require(request, "manage_roadmap")
    if not tracker_store.is_configured:
        raise HTTPException(
            status_code=400,
            detail="No trackers are configured. Add a [[trackers]] block to "
            "pm_studio_local/config.toml.",
        )
    if tracker_store.is_syncing:
        # Not an error: the caller wanted a fresh catalog and one is already being built.
        return {"status": "already_syncing", **tracker_store.describe()}
    tracker_id = (payload.get("tracker_id") or "").strip() or None
    if tracker_id and CONFIG.tracker(tracker_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tracker: {tracker_id}")
    threading.Thread(target=_run_tracker_sync, args=(tracker_id,), daemon=True).start()
    return {"status": "syncing", **tracker_store.describe()}


def _run_tracker_sync(tracker_id: str | None) -> None:
    """One sync pass, off the event loop. Never raises - TrackerStore.sync records each
    tracker's failure in its own status entry, and this wrapper is the last backstop so a
    bug here cannot kill the thread silently."""
    try:
        tracker_store.sync(tracker_id)
        # Linked tickets outside the configured projects are not in the catalog pull, so
        # fetch those individually - otherwise they would render as unresolved forever.
        tracker_store.refresh_missing(roadmap_store.linked_ticket_refs())
    except Exception as exc:  # noqa: BLE001
        print(f"[trackers] sync failed: {exc}")
    _on_roadmap_update({"type": "trackers_synced", "trackers": tracker_store.describe()})


def _tracker_sync_loop() -> None:
    """Background poller: wakes once a minute and syncs whichever trackers are due on
    their own interval (see TrackerConfig.sync_interval_minutes)."""
    while True:
        try:
            for tracker_id in tracker_store.due_tracker_ids():
                _run_tracker_sync(tracker_id)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any single error
            print(f"[trackers] sync loop error: {exc}")
        time.sleep(60)


@app.get("/roadmap/data")
def roadmap_data() -> dict:
    """The board's dataset, grouped by product (the original shape, unchanged).

    `portfolio` is additive: the board ships it so the page can re-group into the
    initiative lens locally as websocket events arrive, without refetching the whole
    board on every keystroke elsewhere. Existing consumers can ignore it.
    """
    return {
        "products": PRODUCTS,
        "items": {
            product: [_with_ticket(item) for item in items]
            for product, items in roadmap_store.list_all().items()
        },
        "portfolio": {
            "initiatives": portfolio_store.list_initiatives(),
            "projects": portfolio_store.list_projects(),
            "catch_all_project_id": portfolio_store.catch_all_project_id,
        },
        # So the board can render the badge palette and the picker without a second
        # round trip. Empty/`configured: false` when no [[trackers]] are declared.
        "trackers": tracker_store.describe(),
    }


@app.get("/roadmap/by-initiative")
def roadmap_by_initiative() -> dict:
    """The same changes under the other lens: Initiative -> Project -> Change.

    One dataset, two pivots. Nothing is dropped when you switch - projects with no
    initiative, and changes with no project, come back in a trailing group with
    `initiative: null` rather than vanishing.
    """
    changes = [
        _with_ticket(item)
        for items in roadmap_store.list_all().values()
        for item in items
    ]
    return {
        "pivot": "initiative",
        "products": PRODUCTS,
        "groups": portfolio_store.group_changes_by_initiative(changes),
        "trackers": tracker_store.describe(),
    }


@app.get("/roadmap/{product}")
def list_roadmap_items(product: str) -> list[dict]:
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    return roadmap_store.list_product(product)


@app.post("/roadmap/{product}/items")
def create_roadmap_item(product: str, request: Request, payload: dict = Body(...)) -> dict:
    actor = _require(request, "manage_roadmap")
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
    _audit(actor, "roadmap.item_created", f"{product}/{item.id}", item.title)
    # Linking at creation is one call instead of create-then-PATCH. A failed link raises,
    # which leaves the change created but unlinked - deliberately not rolled back: the
    # change is the thing worth keeping, and the error says exactly what to retry.
    if payload.get("ticket") or payload.get("ticket_key"):
        item = _apply_ticket_link(actor, item.id, payload)
    return _with_ticket(item.to_dict())


@app.patch("/roadmap/{product}/items/{item_id}")
def update_roadmap_item(product: str, item_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    actor = _require(request, "manage_roadmap")
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
        _audit(actor, "roadmap.item_moved", item_id, f"{product} -> {move_to}")
        # Joined like every other item response - a move must not hand back a shape whose
        # `ticket` key is missing, or the board would drop the badge until a reload.
        return _with_ticket(item.to_dict())

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
    # Ticket linking is applied after the ordinary field update so one PATCH can do both,
    # and it is keyed off `ticket` being PRESENT rather than truthy - `"ticket": ""` is how
    # a caller unlinks, matching the `owner`/`project_id` convention above.
    if "ticket" in payload or "ticket_key" in payload:
        item = _apply_ticket_link(actor, item_id, payload)
    _audit(actor, "roadmap.item_updated", f"{product}/{item_id}")
    return _with_ticket(item.to_dict())


def _apply_ticket_link(actor: User | None, item_id: str, payload: dict) -> RoadmapItem:
    """Links or unlinks a change's ticket from a roadmap PATCH.

    Accepts either a single `ticket` value - a full URL or a bare key, resolved against the
    configured trackers - or an explicit `tracker_id` + `ticket_key` pair for a caller that
    already knows both. An empty value unlinks.

    A link is refused unless the ticket actually EXISTS in its tracker: without that, a
    typo would sit on the card forever as an unresolved badge, which looks identical to a
    tracker being down. The existence check reuses the synced catalog and only calls out
    for a ticket the last sync did not cover.
    """
    reference = payload.get("ticket")
    explicit_key = (payload.get("ticket_key") or "").strip()
    explicit_tracker = (payload.get("tracker_id") or "").strip()

    # Unlink: `"ticket": ""` or `"ticket_key": ""`.
    if (reference is not None and not str(reference).strip()) or (
        "ticket_key" in payload and not explicit_key
    ):
        _audit(actor, "roadmap.ticket_unlinked", item_id)
        return roadmap_store.unlink_ticket(item_id)

    if not tracker_store.is_configured:
        raise HTTPException(
            status_code=400,
            detail="No trackers are configured, so a ticket cannot be linked. Add a "
            "[[trackers]] block to pm_studio_local/config.toml.",
        )

    if explicit_key and explicit_tracker:
        tracker_id, key = explicit_tracker, explicit_key
    else:
        resolved = tracker_store.resolve(str(reference or explicit_key))
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Could not tell which tracker {str(reference or explicit_key)!r} "
                "belongs to. Paste the ticket's URL, or send tracker_id and ticket_key "
                "explicitly. Configured trackers: "
                + (", ".join(t.id for t in CONFIG.trackers) or "none"),
            )
        tracker_id, key = resolved

    if CONFIG.tracker(tracker_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tracker: {tracker_id}")

    try:
        ticket = tracker_store.ensure_ticket(tracker_id, key)
    except TrackerError as exc:
        # 502: the tracker, not this request, is what failed - and the message is already
        # scrubbed of the token by trackers._scrub.
        raise HTTPException(
            status_code=502, detail=f"Could not reach {tracker_id}: {exc}"
        ) from exc
    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail=f"{key} was not found in {tracker_id}. Check the key, or that the "
            "configured credential can see that project.",
        )

    try:
        item = roadmap_store.link_ticket(item_id, tracker_id, key)
    except TicketAlreadyLinked as exc:
        # 409, not 400: the request was well-formed, it collided with existing state. The
        # detail names the change already holding the ticket.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "roadmap.ticket_linked", item_id, f"{tracker_id}/{ticket.key}")
    return item


@app.delete("/roadmap/{product}/items/{item_id}")
def delete_roadmap_item(product: str, item_id: str, request: Request) -> dict:
    actor = _require(request, "manage_roadmap")
    existing = roadmap_store.get(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    if existing.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{existing.product}', not '{product}'",
        )
    roadmap_store.delete(item_id)
    _audit(actor, "roadmap.item_deleted", f"{product}/{item_id}", existing.title)
    return {"status": "deleted"}


@app.websocket("/ws/roadmap")
async def roadmap_ws(websocket: WebSocket) -> None:
    if await _ws_reject(websocket):
        return
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
def reset_chat(session_id: str, request: Request) -> dict:
    """Archives the current spec/chat, then clears them and drops the Claude session
    pointer so the next turn starts a brand-new conversation. PROJECT_STATUS.md,
    PROJECT_INDEX.md, and docs/ are untouched."""
    actor = _require(request, "run_session")
    _get_runtime(session_id).pm_agent.reset()
    _audit(actor, "chat.reset", session_id)
    return {"status": "reset"}


@app.post("/tasks/{session_id}")
def create_task(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Dispatching a dev agent is the one endpoint that is equivalent to running
    arbitrary code on the host - agents run with bypassed permissions inside the repo.
    It is gated here, on the HTTP path, and audited by actor."""
    actor = _require(request, "dispatch_dev_task")
    description = (payload.get("task") or "").strip()
    if not description:
        return {"error": "task description required"}
    task = _get_runtime(session_id).task_registry.start_task(description)
    _audit(actor, "dev_task.dispatched", session_id, description[:300])
    # Dispatching is the strongest signal of human intent in the system - it is a
    # decision to build something - so it carries the most weight in the split.
    _record_signal(actor, KIND_DEV_TASK, session_id)
    _dev_task_dispatchers[task["id"]] = _actor_id(actor)
    return task


@app.get("/tasks/{session_id}")
def list_tasks(session_id: str) -> list[dict]:
    return _get_runtime(session_id).task_registry.list_tasks()


@app.get("/tasks/{session_id}/{task_id}")
def get_task(session_id: str, task_id: str) -> dict:
    task = _get_runtime(session_id).task_registry.get_task(task_id)
    return task if task is not None else {"error": "not found"}


async def _run_tasks_ws(websocket: WebSocket, session_id: str) -> None:
    if await _ws_reject(websocket):
        return
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
    # Driving a PM conversation is `run_session`, not `view`.
    if await _ws_reject(websocket, "run_session"):
        return
    await websocket.accept()
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Unknown or inactive session: {session_id}"})
        )
        await websocket.close()
        return

    # Who is driving this socket, for activity attribution. Resolved once at connect:
    # the cookie cannot change mid-connection.
    ws_user = (
        account_store.resolve_login(websocket.cookies.get(SESSION_COOKIE_NAME))
        if CONFIG.is_enterprise
        else None
    )

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
                # One signal per completed turn, carrying that turn's measured token
                # spend. A turn the stakeholder actually took, so it counts toward
                # their week - unlike an auto-continuation (see _auto_continue_pm).
                if event.get("agent_usage") is not None:
                    _record_signal(
                        ws_user, KIND_PM_TURN, session_id, usage=event["agent_usage"]
                    )
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
def create_task_alias(request: Request, payload: dict = Body(...)) -> dict:
    return create_task(DEFAULT_SESSION_ID, request, payload)


@app.get("/tasks")
def list_tasks_alias() -> list[dict]:
    return list_tasks(DEFAULT_SESSION_ID)


@app.websocket("/ws/chat")
async def chat_ws_alias(websocket: WebSocket) -> None:
    await _run_chat_ws(websocket, DEFAULT_SESSION_ID)


@app.websocket("/ws/tasks")
async def tasks_ws_alias(websocket: WebSocket) -> None:
    await _run_tasks_ws(websocket, DEFAULT_SESSION_ID)
