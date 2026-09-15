"""HTTP surface for the intelligence layer, mounted onto the main app.

Reads need `view` (open in personal mode); anything that teaches the system -
refresh, judge, feedback, tuning, aliases - needs `manage_roadmap`, the same grant
that lets a role change the board it describes. Money columns are stripped for roles
without `view_cost`, hours stay: effort is transparent, compensation is not.
"""

from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .service import FILTER_KEYS, IntelligenceService


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in FILTER_KEYS}


def _strip_money(report: dict) -> dict:
    finance = dict(report.get("finance") or {})
    finance["rows"] = [{k: v for k, v in r.items() if k not in ("rate", "rate_basis", "capex_amount", "opex_amount")} for r in finance.get("rows", [])]
    finance["by_initiative"] = [{k: v for k, v in r.items() if k != "capex_amount"} for r in finance.get("by_initiative", [])]
    finance["totals"] = {k: v for k, v in (finance.get("totals") or {}).items() if "amount" not in k}
    finance["money_hidden"] = True
    return {**report, "finance": finance}


def mount(app: FastAPI, service: IntelligenceService, *, require, can, audit, static_dir) -> None:
    """`require(request, capability)` -> user|None (raises 403), `can(request,
    capability)` -> bool, `audit(user, action, target, detail)`."""

    def actor_name(user) -> str:
        return getattr(user, "name", None) or getattr(user, "email", None) or "local"

    @app.get("/intelligence")
    def intelligence_page() -> FileResponse:
        return FileResponse(static_dir / "intelligence.html", headers={"Cache-Control": "no-cache"})

    @app.get("/intelligence/data")
    def intelligence_data(request: Request) -> dict:
        require(request, "view")
        params = request.query_params
        report = service.report(params.get("from"), params.get("to"), _filters(request), mode=params.get("mode"))
        return report if can(request, "view_cost") else _strip_money(report)

    @app.get("/intelligence/finance.csv")
    def intelligence_finance_csv(request: Request) -> PlainTextResponse:
        require(request, "view_cost")
        params = request.query_params
        text = service.finance_csv(params.get("from"), params.get("to"), _filters(request), mode=params.get("mode"))
        return PlainTextResponse(text, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=capitalization-{params.get('from') or 'window'}-{params.get('to') or 'today'}.csv"})

    @app.get("/intelligence/trace")
    def intelligence_trace(request: Request) -> dict:
        require(request, "view")
        params = request.query_params
        person_id = params.get("person_id") or ""
        if not person_id:
            raise HTTPException(status_code=400, detail="person_id is required")
        return service.trace(params.get("from"), params.get("to"), person_id=person_id, project_id=params.get("project_id") or None, filters=_filters(request), mode=params.get("mode"))

    @app.get("/intelligence/signals")
    def intelligence_signals(request: Request) -> dict:
        require(request, "view")
        params = request.query_params
        ref, project_id = params.get("ref") or None, params.get("project_id") or None
        if not ref and not project_id:
            raise HTTPException(status_code=400, detail="ref or project_id is required")
        return {"signals": service.entity_signals(params.get("from"), params.get("to"), ref=ref, project_id=project_id)}

    @app.get("/intelligence/sources")
    def intelligence_sources(request: Request) -> dict:
        require(request, "view")
        return {"sources": service.ledger.describe(), "refreshing": service.ledger.is_refreshing, "last_refresh_at": service.ledger.last_refresh_at}

    @app.post("/intelligence/refresh")
    def intelligence_refresh(request: Request, payload: dict = Body(default={})) -> dict:
        user = require(request, "manage_roadmap")
        sources = payload.get("sources") if isinstance(payload, dict) else None
        started = service.start_refresh([str(s) for s in sources] if isinstance(sources, list) and sources else None)
        audit(user, "intelligence.refresh", ",".join(sources or []) if isinstance(sources, list) else "all", "started" if started else "already running")
        return {"started": started, "sources": service.ledger.describe()}

    @app.post("/intelligence/judge")
    def intelligence_judge(request: Request) -> dict:
        user = require(request, "manage_roadmap")
        started = service.start_judge(by=actor_name(user))
        audit(user, "intelligence.judge", "", "started" if started else "already running")
        return {"started": started}

    @app.post("/intelligence/judge/suggestions/{index}/apply")
    def intelligence_apply_suggestion(request: Request, index: int) -> dict:
        user = require(request, "manage_roadmap")
        try:
            snapshot = service.apply_suggestion(index, by=actor_name(user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(user, "intelligence.tuning", f"suggestion:{index}", "applied")
        return {"tuning": snapshot}

    @app.post("/intelligence/findings/{finding_id}/feedback")
    def intelligence_feedback(request: Request, finding_id: str, payload: dict = Body(...)) -> dict:
        user = require(request, "manage_roadmap")
        state = str(payload.get("state") or "").strip()
        if state not in ("confirmed", "dismissed", "snoozed", "open"):
            raise HTTPException(status_code=400, detail="state must be confirmed, dismissed, snoozed or open")
        snooze = payload.get("snooze_days")
        try:
            snooze = float(snooze) if snooze is not None else (14.0 if state == "snoozed" else None)
        except (TypeError, ValueError):
            snooze = 14.0
        entry = service.give_feedback(finding_id, state=state, by=actor_name(user), note=str(payload.get("note") or "")[:500], snooze_days=snooze)
        audit(user, "intelligence.feedback", finding_id, state)
        return {"feedback": entry}

    @app.post("/intelligence/tuning")
    def intelligence_tuning(request: Request, payload: dict = Body(...)) -> dict:
        user = require(request, "manage_roadmap")
        try:
            snapshot = service.tune(str(payload.get("kind") or ""), str(payload.get("key") or ""), payload.get("value"), by=actor_name(user), reason=str(payload.get("reason") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(user, "intelligence.tuning", f"{payload.get('kind')}:{payload.get('key')}", str(payload.get("value")))
        return {"tuning": snapshot}

    @app.post("/intelligence/aliases")
    def intelligence_alias(request: Request, payload: dict = Body(...)) -> dict:
        user = require(request, "manage_roadmap")
        handle, person_id = str(payload.get("handle") or "").strip(), str(payload.get("person_id") or "").strip()
        if not handle or not person_id:
            raise HTTPException(status_code=400, detail="handle and person_id are required")
        service.add_alias(handle, person_id, by=actor_name(user))
        audit(user, "intelligence.alias", handle, person_id)
        return {"ok": True}
