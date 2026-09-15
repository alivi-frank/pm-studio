"""Small, human-owned state beside the ledger: feedback on findings, tuning of
thresholds and weights, the judge's verdict log, and identity aliases.

All of it is JSON under `<workspace>/signals/` and small enough to commit; the caches
the sources write live under `signals/cache/` and are disposable."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def _read(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


class FeedbackStore:
    """What humans (and the judge) said about each finding. `state` is one of
    confirmed / dismissed / snoozed / open; a dismissed finding stays out of the
    default list but is never deleted - the rule that produced it is being taught."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = _read(path, {})

    def get(self, finding_id: str) -> dict | None:
        return self.data.get(finding_id)

    def set(self, finding_id: str, *, state: str, by: str, note: str = "", until: float | None = None, source: str = "human") -> dict:
        with self._lock:
            entry = {"state": state, "by": by, "note": note, "at": time.time(), "source": source}
            if until:
                entry["until"] = until
            self.data[finding_id] = entry
            write_json(self.path, self.data)
            return entry

    def set_judged(self, finding_id: str, verdict: str, reason: str, at: float) -> None:
        with self._lock:
            entry = self.data.setdefault(finding_id, {"state": "open", "by": "", "note": "", "at": at, "source": "judge"})
            entry["judged"] = {"verdict": verdict, "reason": reason, "at": at}
            write_json(self.path, self.data)

    def all(self) -> dict:
        return dict(self.data)


class TuningStore:
    """Effective thresholds/weights = package defaults <- config <- this file. The
    file is what the judge's accepted suggestions and the UI's edits write to, so a
    deployment's calibration survives restarts and is visible as a diff."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = _read(path, {})
        self.data.setdefault("thresholds", {})
        self.data.setdefault("weights", {})
        self.data.setdefault("capex_overrides", {})
        self.data.setdefault("muted_rules", [])
        self.data.setdefault("history", [])

    def thresholds(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.data["thresholds"].items()}

    def weights(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.data["weights"].items()}

    def capex_overrides(self) -> dict[str, bool]:
        return {k: bool(v) for k, v in self.data["capex_overrides"].items()}

    def muted_rules(self) -> set[str]:
        return set(self.data["muted_rules"])

    def set(self, kind: str, key: str, value, *, by: str, reason: str = "") -> None:
        with self._lock:
            if kind == "muted_rules":
                muted = set(self.data["muted_rules"])
                if value:
                    muted.add(key)
                else:
                    muted.discard(key)
                self.data["muted_rules"] = sorted(muted)
            else:
                bucket = self.data.setdefault(kind, {})
                if value is None:
                    bucket.pop(key, None)
                else:
                    bucket[key] = value
            self.data["history"].append({"at": time.time(), "kind": kind, "key": key, "value": value, "by": by, "reason": reason[:300]})
            self.data["history"] = self.data["history"][-200:]
            write_json(self.path, self.data)

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))


class JudgmentLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, judgment: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(json.dumps(judgment) + "\n")

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        with self.path.open() as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def latest(self) -> dict | None:
        rows = self.all()
        return rows[-1] if rows else None
