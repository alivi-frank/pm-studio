"""Who is who, across git, Jira, ADO and PM Studio accounts.

The people directory (people.py) already merges tracker identities into persons. This
resolver reads that directory and adds the two things it lacks: git author identities
(email + name, no tracker key) and an alias table a human or the judge can extend.
Nothing here writes to the directory - a suggested merge is a finding, not an edit.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from .model import signal_id

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
GENERIC_LOCALPARTS = {"noreply", "no-reply", "dev", "admin", "root", "git", "user", "build", "ci"}


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def name_tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in normalize_name(name).split() if len(t) > 1)


class IdentityResolver:
    def __init__(self, people: list[dict], *, aliases_path: Path | None = None, accounts: list[dict] | None = None) -> None:
        self._lock = threading.Lock()
        self.aliases_path = aliases_path
        self.by_email: dict[str, dict] = {}
        self.by_identity: dict[tuple[str, str], dict] = {}
        self.by_name: dict[str, dict] = {}
        self.by_tokens: list[tuple[tuple[str, ...], dict]] = []
        self.by_account: dict[str, dict] = {}
        self.persons: dict[str, dict] = {}
        self.unresolved: dict[str, dict] = {}
        self.aliases: dict[str, str] = self._load_aliases()
        for person in people:
            self._index_person(person)
        for user in accounts or []:
            # An account with no directory person still needs a name on the ledger.
            uid = str(user.get("id") or "")
            if uid and uid not in self.by_account:
                pid = None
                email = str(user.get("email") or "").lower()
                if email and email in self.by_email:
                    pid = self.by_email[email]["id"]
                    self.by_account[uid] = self.persons[pid]
                else:
                    synthetic = {"id": f"acct:{uid}", "name": user.get("name") or email or uid, "email": email, "external": False, "source": "account"}
                    self.persons[synthetic["id"]] = synthetic
                    self.by_account[uid] = synthetic
                    if email:
                        self.by_email[email] = synthetic

    def _load_aliases(self) -> dict[str, str]:
        if not self.aliases_path or not self.aliases_path.is_file():
            return {}
        try:
            data = json.loads(self.aliases_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k).lower(): str(v) for k, v in (data.get("aliases") or {}).items()} if isinstance(data, dict) else {}

    def save_alias(self, handle: str, person_id: str) -> None:
        """handle is an email or a normalized name; person_id a directory id."""
        with self._lock:
            self.aliases[handle.lower()] = person_id
            if self.aliases_path:
                self.aliases_path.parent.mkdir(parents=True, exist_ok=True)
                self.aliases_path.write_text(json.dumps({"aliases": self.aliases, "updated_at": time.time()}, indent=2))
            self.unresolved.pop(handle.lower(), None)

    def _index_person(self, person: dict) -> None:
        pid = str(person.get("id") or "")
        if not pid:
            return
        record = {"id": pid, "name": person.get("name") or "", "email": str(person.get("email") or "").lower(), "external": False, "source": person.get("source") or "tracker", "status": person.get("status") or "active"}
        self.persons[pid] = record
        if record["email"]:
            self.by_email[record["email"]] = record
        if person.get("account_id"):
            self.by_account[str(person["account_id"])] = record
        norm = normalize_name(record["name"])
        if norm:
            self.by_name.setdefault(norm, record)
            self.by_tokens.append((name_tokens(record["name"]), record))
        for ident in person.get("identities") or []:
            key = (str(ident.get("tracker_id") or ""), str(ident.get("key") or ""))
            self.by_identity[key] = record
            email = str(ident.get("email") or "").lower()
            if email:
                self.by_email.setdefault(email, record)
            display = normalize_name(str(ident.get("display") or ""))
            if display:
                self.by_name.setdefault(display, record)

    def _fuzzy_name(self, name: str) -> dict | None:
        tokens = name_tokens(name)
        if len(tokens) < 2:
            return None
        best = None
        for candidate_tokens, record in self.by_tokens:
            if len(candidate_tokens) < 2:
                continue
            short, long_ = (tokens, candidate_tokens) if len(tokens) <= len(candidate_tokens) else (candidate_tokens, tokens)
            # First name + last name both present in the other - "Eduardo De la Cruz"
            # vs "Eduardo de la Cruz Rojas"; never a lone surname.
            if short[0] == long_[0] and set(short) <= set(long_):
                if best is not None and best is not record:
                    return None  # ambiguous
                best = record
        return best

    def resolve(self, name: str = "", email: str = "", *, tracker_id: str | None = None, key: str | None = None, account_id: str | None = None) -> dict:
        """The person behind an actor. Match order: alias, tracker identity, account,
        email, exact name, fuzzy name. Unknown actors get a stable synthetic person so
        their effort is still counted - and listed under `unresolved` for the
        directory to adopt."""
        email = (email or "").lower().strip()
        name = (name or "").strip()
        alias_key = email or normalize_name(name)
        if alias_key and alias_key in self.aliases:
            target = self.persons.get(self.aliases[alias_key])
            if target:
                return {**target, "matched_by": "alias"}
        if tracker_id and key and (tracker_id, key) in self.by_identity:
            return {**self.by_identity[(tracker_id, key)], "matched_by": "identity"}
        if account_id and account_id in self.by_account:
            return {**self.by_account[account_id], "matched_by": "account"}
        if email and email in self.by_email:
            return {**self.by_email[email], "matched_by": "email"}
        norm = normalize_name(name)
        if norm and norm in self.by_name:
            record = self.by_name[norm]
            if email and email not in self.by_email:
                self.by_email[email] = record  # learn the git email for this run
            return {**record, "matched_by": "name"}
        fuzzy = self._fuzzy_name(name) if name else None
        if fuzzy:
            if email:
                self.by_email[email] = fuzzy
            return {**fuzzy, "matched_by": "fuzzy"}
        handle = email or norm or "unknown"
        pid = f"ext:{signal_id(handle)}"
        with self._lock:
            entry = self.unresolved.setdefault(handle, {"id": pid, "name": name or email or "unknown", "email": email, "external": True, "source": "unresolved", "signals": 0, "hints": []})
            entry["signals"] += 1
            if len(entry["hints"]) < 3 and name and name not in entry["hints"]:
                entry["hints"].append(name)
            self.persons.setdefault(pid, {k: v for k, v in entry.items() if k not in ("signals", "hints")})
        return {**self.persons[pid], "matched_by": "none"}

    def suggestions(self) -> list[dict]:
        """Unresolved handles that LOOK like a known person (same first name, or the
        email's local part inside a known name), for the judge and the alias UI."""
        out = []
        for handle, entry in sorted(self.unresolved.items(), key=lambda kv: -kv[1]["signals"]):
            candidates = []
            local = handle.split("@", 1)[0] if "@" in handle else ""
            local_norm = normalize_name(local.replace(".", " ").replace("_", " ").replace("-", " "))
            for record in self.persons.values():
                if record.get("external"):
                    continue
                tokens = name_tokens(record["name"])
                if not tokens:
                    continue
                if local_norm and local_norm not in GENERIC_LOCALPARTS and all(tok in normalize_name(record["name"]) for tok in local_norm.split() if len(tok) > 2):
                    candidates.append(record["id"])
                elif entry["name"] and name_tokens(entry["name"])[:1] == tokens[:1] and len(name_tokens(entry["name"])) >= 2:
                    candidates.append(record["id"])
            out.append({"handle": handle, "name": entry["name"], "email": entry["email"], "signals": entry["signals"], "candidates": candidates[:3], "person_id": entry["id"]})
        return out
