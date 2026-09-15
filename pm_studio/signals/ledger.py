"""The ledger: runs the sources, keeps their results, and hands out attributed slices.

Server-owned, like every other store: one process reads and writes the caches under
`<workspace>/signals/cache/`. A refresh runs off the request thread (the sources talk
to Jira/ADO and read tens of thousands of commits); readers keep seeing the previous
result until the new one is in.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .attribution import AttributionContext, Attributor, coverage
from .identity import IdentityResolver
from .model import Signal
from .sources.base import CollectResult


class SignalLedger:
    def __init__(self, data_dir: Path, *, since: str, sources: list, context_builder: Callable[[], tuple[AttributionContext, IdentityResolver]]) -> None:
        self.data_dir = data_dir
        self.cache_dir = data_dir / "cache"
        self.since = since
        self.sources = {s.id: s for s in sources}
        self._context_builder = context_builder
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.results: dict[str, CollectResult] = {}
        self.status: dict[str, dict] = {}
        self.generation = 0
        self._slices_cache: tuple[str, list[dict], dict, IdentityResolver] | None = None
        self.refreshing: set[str] = set()
        self.last_refresh_at: float | None = None
        self.load()

    # ---- persistence ----

    @property
    def since_epoch(self) -> float:
        y, m, d = (int(p) for p in self.since.split("-"))
        return datetime(y, m, d, tzinfo=timezone.utc).timestamp()

    def _cache_path(self, source_id: str) -> Path:
        return self.cache_dir / f"{source_id}.json.gz"

    def _read_cache(self, source_id: str) -> dict | None:
        """The gzipped cache, or the plain-JSON one an earlier build wrote."""
        gz = self._cache_path(source_id)
        plain = self.cache_dir / f"{source_id}.json"
        if gz.is_file():
            with gzip.open(gz, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        if plain.is_file():
            return json.loads(plain.read_text())
        return None

    def load(self) -> None:
        for source_id in self.sources:
            try:
                data = self._read_cache(source_id)
            except (OSError, json.JSONDecodeError, EOFError) as exc:
                self.status[source_id] = {"state": "error", "error": f"cache unreadable: {exc}", "signals": 0}
                continue
            if data is None:
                self.status[source_id] = {"state": "never", "signals": 0}
                continue
            result = CollectResult(signals=[Signal.from_dict(s) for s in data.get("signals", [])], facts=data.get("facts") or {}, notes=list(data.get("notes") or []), truncated=bool(data.get("truncated")))
            self.results[source_id] = result
            self.status[source_id] = {"state": "cached", "signals": len(result.signals), "collected_at": data.get("collected_at"), "seconds": data.get("seconds"), "notes": result.notes[:10], "truncated": result.truncated, "summary": data.get("summary") or {}}
            self.last_refresh_at = max(self.last_refresh_at or 0, float(data.get("collected_at") or 0)) or None
        self.generation += 1

    def _save(self, source_id: str, result: CollectResult, seconds: float, summary: dict) -> None:
        payload = {"signals": [s.to_dict() for s in result.signals], "facts": result.facts, "notes": result.notes[:50], "truncated": result.truncated, "collected_at": time.time(), "seconds": round(seconds, 1), "summary": summary}
        path = self._cache_path(source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
        plain = self.cache_dir / f"{source_id}.json"
        if plain.is_file():
            plain.unlink()

    # ---- refresh ----

    def refresh(self, source_ids: list[str] | None = None) -> dict:
        """Runs the named sources (default: all configured) and swaps their results
        in. Serialized: a second caller while one runs gets {"running": True}."""
        if not self._refresh_lock.acquire(blocking=False):
            return {"running": True, "status": self.describe()}
        try:
            for source_id, source in self.sources.items():
                if not source.configured:
                    self.status[source_id] = {**self.status.get(source_id, {}), "state": "unconfigured", "signals": 0}
            ids = source_ids or [sid for sid, s in self.sources.items() if s.configured]
            for source_id in ids:
                source = self.sources.get(source_id)
                if source is None or not source.configured:
                    continue
                self.refreshing.add(source_id)
                started = time.time()
                try:
                    result = source.collect(self.since_epoch, self.results.get(source_id))
                    seconds = time.time() - started
                    summary = summarize_facts(source_id, result)
                    with self._lock:
                        self.results[source_id] = result
                        self.status[source_id] = {"state": "ok" if not result.truncated else "partial", "signals": len(result.signals), "collected_at": time.time(), "seconds": round(seconds, 1), "notes": result.notes[:10], "truncated": result.truncated, "summary": summary}
                        self.generation += 1
                        self.last_refresh_at = time.time()
                    self._save(source_id, result, seconds, summary)
                except Exception as exc:  # noqa: BLE001 - one source failing must not stop the others
                    self.status[source_id] = {**self.status.get(source_id, {}), "state": "error", "error": str(exc)[:400], "collected_at": time.time()}
                finally:
                    self.refreshing.discard(source_id)
            return {"running": False, "status": self.describe()}
        finally:
            self._refresh_lock.release()

    @property
    def is_refreshing(self) -> bool:
        return bool(self.refreshing)

    def describe(self) -> list[dict]:
        out = []
        for source_id, source in self.sources.items():
            info = source.describe()
            out.append({**info, **self.status.get(source_id, {}), "refreshing": source_id in self.refreshing})
        return out

    # ---- reads ----

    def signals(self) -> list[Signal]:
        with self._lock:
            out: list[Signal] = []
            for result in self.results.values():
                out.extend(result.signals)
        return out

    def facts(self, source_id: str) -> dict:
        result = self.results.get(source_id)
        return result.facts if result else {}

    def issue_facts(self) -> dict[str, dict]:
        """Per-ticket timeline facts from the tracker sources, keyed "tracker:KEY"."""
        out: dict[str, dict] = {}
        for source_id, result in self.results.items():
            tracker_id = getattr(self.sources.get(source_id), "tracker_id", None)
            if not tracker_id:
                continue
            for key, fact in (result.facts.get("issues") or {}).items():
                out[f"{tracker_id}:{key}"] = fact
        return out

    def slices(self) -> tuple[list[dict], dict, IdentityResolver]:
        """Attributed slices over every signal, cached until the stores or the ledger
        change. Returns (slices, coverage, resolver)."""
        ctx, resolver = self._context_builder()
        key = f"{ctx.version}|{self.generation}"
        cached = self._slices_cache
        if cached and cached[0] == key:
            return cached[1], cached[2], cached[3]
        attributor = Attributor(ctx, resolver)
        out: list[dict] = []
        for signal in self.signals():
            out.extend(attributor.slices(signal))
        out.sort(key=lambda s: s["at"])
        cov = coverage(out)
        self._slices_cache = (key, out, cov, resolver)
        return out, cov, resolver

    def signals_by_id(self, ids: list[str]) -> list[dict]:
        wanted = set(ids)
        return [s.to_dict() for s in self.signals() if s.id in wanted]


def summarize_facts(source_id: str, result: CollectResult) -> dict:
    facts = result.facts or {}
    if source_id == "git":
        repos = facts.get("repos") or {}
        return {"repos": len(repos), "newest_at": max((r.get("newest_at") or 0) for r in repos.values()) if repos else None,
                "repo_rows": sorted(({"repo": k, **{kk: vv for kk, vv in v.items() if kk != "error"}, "error": v.get("error")} for k, v in repos.items()), key=lambda r: -(r.get("commits") or 0))}
    if source_id in ("jira", "ado"):
        return {"issues": len(facts.get("issues") or {}), "refetched": facts.get("refetched", facts.get("touched"))}
    if source_id == "pm":
        return {k: facts.get(k) for k in ("turns", "dev_tasks", "agent_cost_usd")}
    if source_id == "ado-prs":
        return {"pull_requests": facts.get("pull_requests")}
    if source_id == "inbox":
        return {"records": facts.get("records")}
    return {}
