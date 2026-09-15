"""Audit-ready time for capitalization and R&D credits - without timesheets.

The allocation already says how each person's active days split across projects and
which of those hours are capitalizable by rule. This module folds that into the two
shapes finance wants: a period statement (person x project, hours, treatment, amount)
and the evidence behind any cell (the signals that earned those hours). Rates come
from the costing roster when the person has one, else the blended rate, else the
row carries hours only and says so.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict


def statement(alloc_rows: list[dict], *, projects: dict[str, dict], initiatives: dict[str, dict], rate_for, currency: str = "USD") -> dict:
    """rate_for(person_id) -> (rate or None, basis label)."""
    cells: dict[tuple[str, str | None], dict] = {}
    for r in alloc_rows:
        key = (r["person_id"], r["project_id"])
        cell = cells.setdefault(key, {"person_id": r["person_id"], "person_name": r["person_name"], "person_external": r.get("person_external", False), "project_id": r["project_id"], "hours": 0.0, "logged_hours": 0.0, "inferred_hours": 0.0, "capex_hours": 0.0, "opex_hours": 0.0, "days": set(), "signals": 0, "nature": defaultdict(float), "via": defaultdict(float), "initiative_id": r.get("initiative_id"), "maintenance": r.get("maintenance", False)})
        cell["hours"] += r["hours"]
        cell["logged_hours"] += r["logged_hours"]
        cell["inferred_hours"] += r["inferred_hours"]
        if r.get("capex"):
            cell["capex_hours"] += r["hours"]
        else:
            cell["opex_hours"] += r["hours"]
        cell["days"].add(r["day"])
        cell["signals"] += r["signals"]
        cell["nature"][r.get("nature") or "other"] += r["hours"]
        cell["via"][r.get("via") or "none"] += r["hours"]
    rows = []
    totals = {"hours": 0.0, "capex_hours": 0.0, "opex_hours": 0.0, "capex_amount": 0.0, "opex_amount": 0.0, "priced_hours": 0.0, "unpriced_hours": 0.0}
    for cell in cells.values():
        rate, basis = rate_for(cell["person_id"])
        project = projects.get(cell["project_id"] or "") or {}
        initiative = initiatives.get(cell["initiative_id"] or "") or {}
        capex_amount = round(cell["capex_hours"] * rate, 2) if rate is not None else None
        opex_amount = round(cell["opex_hours"] * rate, 2) if rate is not None else None
        treatment = "capitalize" if cell["capex_hours"] >= cell["hours"] - 1e-6 else ("expense" if cell["capex_hours"] <= 1e-6 else "split")
        rows.append({
            "person_id": cell["person_id"], "person_name": cell["person_name"], "person_external": cell["person_external"],
            "project_id": cell["project_id"], "project_title": project.get("title") or ("Unattributed" if not cell["project_id"] else cell["project_id"]),
            "initiative_id": cell["initiative_id"], "initiative_title": initiative.get("title") or ("—" if not cell["initiative_id"] else cell["initiative_id"]),
            "maintenance": cell["maintenance"],
            "hours": round(cell["hours"], 2), "logged_hours": round(cell["logged_hours"], 2), "inferred_hours": round(cell["inferred_hours"], 2),
            "capex_hours": round(cell["capex_hours"], 2), "opex_hours": round(cell["opex_hours"], 2), "treatment": treatment,
            "rate": rate, "rate_basis": basis, "capex_amount": capex_amount, "opex_amount": opex_amount,
            "days": len(cell["days"]), "signals": cell["signals"],
            "nature_mix": {k: round(v, 1) for k, v in sorted(cell["nature"].items(), key=lambda kv: -kv[1])},
            "evidence_basis": {k: round(v, 1) for k, v in sorted(cell["via"].items(), key=lambda kv: -kv[1])},
        })
        totals["hours"] += cell["hours"]
        totals["capex_hours"] += cell["capex_hours"]
        totals["opex_hours"] += cell["opex_hours"]
        if rate is not None:
            totals["capex_amount"] += cell["capex_hours"] * rate
            totals["opex_amount"] += cell["opex_hours"] * rate
            totals["priced_hours"] += cell["hours"]
        else:
            totals["unpriced_hours"] += cell["hours"]
    rows.sort(key=lambda r: (-r["capex_hours"], -r["hours"]))
    by_initiative: dict = defaultdict(lambda: {"hours": 0.0, "capex_hours": 0.0, "capex_amount": 0.0, "people": set()})
    for r in rows:
        b = by_initiative[r["initiative_id"]]
        b["title"] = r["initiative_title"]
        b["hours"] += r["hours"]
        b["capex_hours"] += r["capex_hours"]
        b["capex_amount"] += r["capex_amount"] or 0.0
        b["people"].add(r["person_id"])
        b["maintenance"] = r["maintenance"]
    init_rows = [{"initiative_id": k, "title": v["title"], "hours": round(v["hours"], 1), "capex_hours": round(v["capex_hours"], 1), "capex_amount": round(v["capex_amount"], 2), "people": len(v["people"]), "maintenance": v["maintenance"], "capex_pct": round(100.0 * v["capex_hours"] / v["hours"], 0) if v["hours"] else 0} for k, v in by_initiative.items()]
    init_rows.sort(key=lambda r: -r["capex_hours"])
    return {
        "rows": rows, "by_initiative": init_rows, "currency": currency,
        # Money totals are None, not zero, when nothing in the window carried a rate -
        # "we do not know" must never read as "it cost nothing".
        "totals": {k: (None if k.endswith("_amount") and totals["priced_hours"] == 0 else round(v, 2)) for k, v in totals.items()},
        "capex_pct": round(100.0 * totals["capex_hours"] / totals["hours"], 1) if totals["hours"] else 0.0,
        "logged_pct": round(100.0 * sum(r["logged_hours"] for r in rows) / totals["hours"], 1) if totals["hours"] else 0.0,
        "methodology": (
            "Hours are activity-weighted: each person's active day is worth the configured capacity, "
            "explicit worklogs are booked where logged, and the remainder is split across the tickets and "
            "repositories they touched that day in proportion to signal weight. Capitalizable = development "
            "work on a live project of a non-maintenance initiative, excluding defects; per-initiative "
            "overrides are recorded in the tuning log. Every hour traces to the signals that earned it."
        ),
    }


def to_csv(report: dict, *, period_label: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["period", "person", "external_identity", "project", "initiative", "maintenance", "hours", "logged_hours", "inferred_hours", "capex_hours", "opex_hours", "treatment", "rate", "rate_basis", "capex_amount", "opex_amount", "active_days", "evidence_signals", "nature_mix", "evidence_basis"])
    for r in report["rows"]:
        writer.writerow([period_label, r["person_name"], "yes" if r["person_external"] else "no", r["project_title"], r["initiative_title"], "yes" if r["maintenance"] else "no", r["hours"], r["logged_hours"], r["inferred_hours"], r["capex_hours"], r["opex_hours"], r["treatment"], r["rate"] if r["rate"] is not None else "", r["rate_basis"], r["capex_amount"] if r["capex_amount"] is not None else "", r["opex_amount"] if r["opex_amount"] is not None else "", r["days"], r["signals"], "; ".join(f"{k}={v}" for k, v in r["nature_mix"].items()), "; ".join(f"{k}={v}" for k, v in r["evidence_basis"].items())])
    return buf.getvalue()


def trace(alloc_rows: list[dict], signals_by_id, *, person_id: str, project_id: str | None) -> dict:
    """The audit trail behind one statement cell: the day rows and their signals."""
    rows = [r for r in alloc_rows if r["person_id"] == person_id and (r["project_id"] or None) == (project_id or None)]
    ids: list[str] = []
    for r in rows:
        ids.extend(r["signal_ids"])
    signals = signals_by_id(ids)
    signals.sort(key=lambda s: s["at"])
    return {"days": [{k: v for k, v in r.items() if k != "signal_ids"} for r in rows], "signals": signals, "hours": round(sum(r["hours"] for r in rows), 2)}
