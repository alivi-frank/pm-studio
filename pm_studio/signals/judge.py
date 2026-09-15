"""The independent intelligence judge, and the feedback cycle around it.

The detector, the allocator and the metrics are deterministic and cheap; what they
cannot do is tell a real problem from an artefact of the data. So after each refresh
(or on demand) a second, read-only agent gets a dossier - coverage, the allocation,
the findings with their evidence, what humans said about earlier findings, and what
it said itself last time - and returns a structured judgment:

- scores per dimension, so the picture's own quality is a metric that trends;
- a verdict per finding (confirm / noise / needs data) with a reason;
- threshold suggestions, applied only when a human accepts them (the loop closes
  through people, on purpose);
- data fixes (an alias to add, a repo to map, a ticket to link) and next metrics
  worth building.

Like the gitflow judge, it cannot write: a read-only tool allowlist enforced by the
CLI, a prompt composed here from data only, and an inconclusive verdict - visibly -
on any failure. A deterministic self-assessment runs beside it every time, so the
scores exist even when no model is reachable.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ..costing import agent_usage
from ..models import MODELS

JUDGE_TIMEOUT_SECONDS = 900
JUDGE_ALLOWED_TOOLS = "Read Grep Glob Bash(git log:*) Bash(git show:*) Bash(git shortlog:*)"
VERDICTS = ("confirm", "noise", "needs_data")

PROMPT = """\
You are an independent judge of an engineering-intelligence report. A system has read \
every commit, ticket transition, worklog and comment across a company's repositories and \
trackers, attributed them to projects and initiatives, allocated people's hours by that \
activity, and raised findings about stale work, misattribution and unhealthy patterns. \
Your ONLY job is to judge that picture: is the attribution trustworthy, are the findings \
real problems or artefacts of the data, and what should change next. You are read-only. \
Never take the system's own summary as proof - the dossier file and the repositories are \
the evidence; check a few findings against them where you can.

The full dossier is at {dossier_path} (JSON). A compact excerpt follows:
{excerpt}

Judge with these lenses:
1. ATTRIBUTION - how much effort lands on a real project, and whether the unplaced \
remainder is a data problem (unkeyed commits, unlinked tickets, unresolved authors) or \
genuinely unplanned work.
2. ALLOCATION PLAUSIBILITY - do the hours per initiative look like the commit and \
ticket volume behind them, or is something inflating (bots, merges, one field-edit \
counted as a day)?
3. FINDING PRECISION - for each listed finding, is it a real problem a manager should \
act on ("confirm"), an artefact ("noise" - say what artefact), or undecidable without \
more data ("needs_data")? Prior human feedback in the dossier is ground truth about \
this team's judgment: learn from it.
4. CALIBRATION - if a rule produces mostly noise, suggest a threshold change (name the \
threshold key exactly as in the dossier's `thresholds`).
5. DATA FIXES - concrete, checkable fixes: an alias (handle -> person), a repository \
that maps to no system, a ticket that should be linked to a change.
6. NEXT METRICS - what this team should measure next, given what the data can already \
support, and what it would need to support more.

Reply with ONLY a JSON object, no prose before or after, in exactly this shape:
{{"scores": {{"attribution": 0-100, "allocation_plausibility": 0-100, "finding_precision": 0-100, "data_quality": 0-100, "overall": 0-100}},
  "findings_review": [{{"id": "<finding id from the dossier>", "verdict": "confirm" | "noise" | "needs_data", "reason": "<one sentence>"}}],
  "threshold_suggestions": [{{"key": "<threshold key>", "current": <number>, "suggested": <number>, "reason": "<one sentence>"}}],
  "data_fixes": [{{"kind": "alias" | "repo_mapping" | "ticket_link" | "other", "detail": "<what exactly>", "handle": "<email or name, for alias>", "person_id": "<directory id, for alias>"}}],
  "next_metrics": [{{"title": "...", "why": "...", "needs": "..."}}],
  "summary": "<three or four sentences a VP of Engineering would read>"}}
"""


def judge_model(preferred: str, fallback: str) -> str:
    if preferred and preferred in MODELS:
        return preferred
    for model_id in MODELS:
        if "opus" in model_id:
            return model_id
    return fallback


def self_assessment(dossier: dict) -> dict:
    """Deterministic scores from the dossier alone - the floor the model judges over."""
    cov = dossier.get("coverage") or {}
    placed = float(cov.get("placed_pct") or 0.0)
    wf = dossier.get("workflow") or {}
    keyed = wf.get("keyed_pct")
    keyed = float(keyed) if keyed is not None else placed
    unresolved = int(cov.get("unresolved_people") or 0)
    people = max(1, int(cov.get("people") or 1))
    fin = dossier.get("finance") or {}
    logged_pct = float(fin.get("logged_pct") or 0.0)
    sources = dossier.get("sources") or []
    fresh = [s for s in sources if s.get("configured") and s.get("collected_at") and time.time() - float(s["collected_at"]) < 2 * 86400]
    configured = [s for s in sources if s.get("configured")]
    freshness = 100.0 * len(fresh) / len(configured) if configured else 0.0
    counts = dossier.get("finding_counts") or {}
    visible = max(1, int(counts.get("visible") or 1))
    noise = int(counts.get("judged_noise") or 0) + int(counts.get("dismissed") or 0)
    precision = max(0.0, 100.0 - 100.0 * noise / (visible + noise))
    data_quality = max(0.0, 100.0 - 100.0 * unresolved / (people + unresolved)) * 0.5 + min(100.0, keyed) * 0.5
    plausibility = 40.0 + 0.6 * min(100.0, logged_pct * 2) if logged_pct else 55.0
    scores = {"attribution": round(placed), "allocation_plausibility": round(min(100.0, plausibility)), "finding_precision": round(precision), "data_quality": round(data_quality), "freshness": round(freshness)}
    scores["overall"] = round(sum(scores.values()) / len(scores))
    notes = []
    if placed < 70:
        notes.append(f"Only {placed:.0f}% of effort lands on a project; fix tagging and links before trusting per-initiative hours.")
    if unresolved:
        notes.append(f"{unresolved} author identities are not in the people directory.")
    if keyed is not None and keyed < 70:
        notes.append(f"{keyed:.0f}% of commits carry a ticket key.")
    if not logged_pct:
        notes.append("No explicit worklogs in the window - every hour is inferred from activity.")
    return {"scores": scores, "notes": notes, "kind": "self_assessment", "at": time.time()}


def build_dossier(report: dict, *, feedback: dict, previous: dict | None, thresholds: dict, max_findings: int = 60) -> dict:
    """Everything the judge is allowed to know, as data. Findings carry their evidence
    and any earlier human/judge verdict; nothing here is the system's opinion of itself
    beyond the numbers."""
    findings = [
        {k: f.get(k) for k in ("id", "rule", "severity", "title", "detail", "evidence", "value", "unit", "links", "state", "judged")}
        for f in report.get("findings", [])[:max_findings]
    ]
    human = {fid: {"state": fb.get("state"), "note": fb.get("note")} for fid, fb in feedback.items() if fb.get("source", "human") == "human" and fb.get("state") in ("confirmed", "dismissed")}
    return {
        "window": report.get("window"),
        "coverage": report.get("coverage"),
        "kpis": report.get("kpis"),
        "allocation": {"by_initiative": report.get("allocation", {}).get("by_initiative", [])[:20], "by_via": report.get("coverage", {}).get("by_via_pct")},
        "impact": report.get("impact", {}).get("initiatives", [])[:20],
        "flow": report.get("flow"),
        "workflow": {k: v for k, v in (report.get("workflow") or {}).items() if k not in ("heatmap",)},
        "finance": {k: v for k, v in (report.get("finance") or {}).items() if k in ("capex_pct", "logged_pct", "totals")},
        "findings": findings,
        "finding_counts": report.get("finding_counts"),
        "human_feedback": human,
        "unresolved_authors": report.get("identity", {}).get("suggestions", [])[:15],
        "thresholds": thresholds,
        "sources": report.get("sources"),
        "previous_judgment": {k: previous.get(k) for k in ("at", "scores", "summary", "threshold_suggestions")} if previous else None,
    }


def excerpt(dossier: dict, limit: int = 14000) -> str:
    slim = dict(dossier)
    slim["findings"] = [{k: f.get(k) for k in ("id", "rule", "severity", "title", "detail", "state", "judged")} for f in dossier.get("findings", [])[:40]]
    slim.pop("sources", None)
    text = json.dumps(slim, indent=1, default=str)
    return text if len(text) <= limit else text[:limit] + "\n... (truncated; read the dossier file for the rest)"


def parse_judgment(result_text: str) -> dict | None:
    text = result_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scores"), dict):
        return None
    scores = {}
    for k, v in data["scores"].items():
        try:
            scores[str(k)] = max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            continue
    reviews = []
    for row in data.get("findings_review") or []:
        if isinstance(row, dict) and row.get("id") and row.get("verdict") in VERDICTS:
            reviews.append({"id": str(row["id"]), "verdict": row["verdict"], "reason": str(row.get("reason") or "")[:300]})
    suggestions = []
    for row in data.get("threshold_suggestions") or []:
        if isinstance(row, dict) and row.get("key"):
            try:
                suggestions.append({"key": str(row["key"]), "current": float(row.get("current")) if row.get("current") is not None else None, "suggested": float(row["suggested"]), "reason": str(row.get("reason") or "")[:300]})
            except (TypeError, ValueError):
                continue
    fixes = [{"kind": str(r.get("kind") or "other"), "detail": str(r.get("detail") or "")[:300], "handle": str(r.get("handle") or ""), "person_id": str(r.get("person_id") or "")} for r in (data.get("data_fixes") or []) if isinstance(r, dict)]
    nexts = [{"title": str(r.get("title") or "")[:120], "why": str(r.get("why") or "")[:300], "needs": str(r.get("needs") or "")[:300]} for r in (data.get("next_metrics") or []) if isinstance(r, dict)]
    return {"scores": scores, "findings_review": reviews, "threshold_suggestions": suggestions, "data_fixes": fixes, "next_metrics": nexts, "summary": str(data.get("summary") or "")[:2000]}


def inconclusive(reason: str, model: str, usage: dict | None = None) -> dict:
    return {"kind": "judgment", "verdict": "inconclusive", "reason": reason, "model": model, "agent_usage": usage or {}, "scores": {}, "findings_review": [], "threshold_suggestions": [], "data_fixes": [], "next_metrics": [], "summary": ""}


def run_judge(dossier: dict, *, dossier_path: Path, repo_root: Path, model: str, runner=None) -> dict:
    """One judge turn. Always returns a judgment dict; failure modes are inconclusive."""
    dossier_path.parent.mkdir(parents=True, exist_ok=True)
    dossier_path.write_text(json.dumps(dossier, indent=1, default=str))
    prompt = PROMPT.format(dossier_path=str(dossier_path), excerpt=excerpt(dossier))
    command = ["claude", "-p", prompt, "--output-format", "json", "--allowedTools", JUDGE_ALLOWED_TOOLS, "--model", model]
    run = runner or (lambda cmd: subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=JUDGE_TIMEOUT_SECONDS))
    started = time.time()
    try:
        proc = run(command)
    except subprocess.TimeoutExpired:
        return inconclusive(f"Judge timed out after {JUDGE_TIMEOUT_SECONDS}s.", model)
    except FileNotFoundError:
        return inconclusive("The `claude` CLI is not installed or not on PATH.", model)
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return inconclusive(f"Judge produced no output (exit {proc.returncode}). stderr: {(proc.stderr or '').strip()[:500]}", model)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return inconclusive(f"Judge output was not the CLI's JSON: {stdout[:500]}", model)
    usage = agent_usage(data)
    if data.get("is_error") or proc.returncode != 0:
        return inconclusive(f"Judge run errored: {str(data.get('result', ''))[:500]}", model, usage)
    parsed = parse_judgment(str(data.get("result", "")))
    if parsed is None:
        return inconclusive(f"Judge did not answer in the required JSON shape: {str(data.get('result', ''))[:500]}", model, usage)
    return {**parsed, "kind": "judgment", "verdict": "ok", "model": model, "agent_usage": usage, "seconds": round(time.time() - started, 1)}
