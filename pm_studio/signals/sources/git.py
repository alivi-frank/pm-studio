"""Commits from every nested git checkout the deployment declares.

A commit is the most honest signal there is: it names who, when, on which repository,
what changed, and - when the team writes them - which ticket. This adapter reads
`git log --all` on each repo under a [systems] path (plus `extra_repos`), so branches
that never merged still count as work done.

What it deliberately does NOT do: fetch. Reading is local and offline; the Sources
panel shows each repo's newest commit so a stale checkout is visible rather than
silently under-counting.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..model import (
    DEFAULT_WEIGHTS,
    KIND_COMMIT,
    KIND_MERGE,
    PR_NUMBER_RE,
    Signal,
    extract_ticket_refs,
    is_ai_assisted,
    parse_iso,
    signal_id,
)
from .base import CollectResult

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
# The record separator LEADS each record: --numstat prints a commit's file lines AFTER
# its format line, so a trailing separator would hand every commit's stats to the
# next record. Leading, each chunk is "fields, then that commit's own numstat".
LOG_FORMAT = RECORD_SEP + FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%P", "%s", "%b"])
GIT_TIMEOUT_SECONDS = 300
MAX_DISCOVERY_DEPTH = 3
# Authors that are automation, not people. Their commits still count as repository
# motion but never as anyone's effort.
BOT_MARKERS = ("snyk-bot", "dependabot", "renovate", "[bot]", "noreply@github.com/bot", "azure-pipelines", "bitbucket-pipelines")


def discover_repos(repo_root: Path, roots: list[str], max_depth: int = MAX_DISCOVERY_DEPTH) -> list[str]:
    """Repo-root-relative paths of every git checkout under the given roots. A root
    that is itself a checkout is returned as one repo; otherwise its children are
    walked to `max_depth` (CapAdmin is 34 repos under one system path)."""
    found: list[str] = []
    seen: set[str] = set()
    # Resolved once, so a symlinked root (macOS /var -> /private/var) compares equal
    # to the resolved children below.
    repo_root = repo_root.resolve()

    def is_repo(path: Path) -> bool:
        marker = path / ".git"
        return marker.is_dir() or marker.is_file()

    def walk(path: Path, depth: int) -> None:
        if not path.is_dir():
            return
        rel = str(path.relative_to(repo_root))
        if is_repo(path):
            if rel not in seen:
                seen.add(rel)
                found.append(rel)
            # A repo may nest another (snakesdk inside alivi-route); keep walking one
            # level so the nested clone's commits are read as their own repo.
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            if child.name in ("node_modules", "vendor", "build", "dist", "__pycache__", ".venv"):
                continue
            walk(child, depth + 1)

    for root in roots:
        walk((repo_root / root).resolve() if not Path(root).is_absolute() else Path(root), 0)
    return found


def _is_bot(name: str, email: str) -> bool:
    haystack = f"{name} {email}".lower()
    return any(marker in haystack for marker in BOT_MARKERS)


def _branch_from_subject(subject: str) -> str:
    """Best-effort branch name out of merge subjects: "Merge branch 'x'", "Merge
    remote-tracking branch 'origin/x' into y", "Merged in x (pull request #7)",
    "Merge pull request 13460 from x into y"."""
    text = subject.strip()
    for marker in ("branch '", "Merged in ", " from "):
        idx = text.find(marker)
        if idx >= 0:
            rest = text[idx + len(marker):]
            end = len(rest)
            for stop in ("'", " (", " into ", " "):
                j = rest.find(stop)
                if j > 0:
                    end = min(end, j)
            name = rest[:end].strip()
            if name.startswith("origin/"):
                name = name[7:]
            return name[:120]
    return ""


def parse_log(raw: str, repo: str, system: str | None, *, jira_projects: set[str] | None, ado_enabled: bool, weights: dict[str, float]) -> list[Signal]:
    """The git log records (LOG_FORMAT + --numstat) as signals."""
    signals: list[Signal] = []
    for record in raw.split(RECORD_SEP):
        if not record.strip():
            continue
        # The body field (%b) may contain newlines, so split on the field separator and
        # take this commit's numstat lines off the tail of the LAST field.
        parts = record.lstrip("\n").split(FIELD_SEP)
        if len(parts) < 7:
            continue
        sha, name, email, when, parents, subject = parts[:6]
        tail = FIELD_SEP.join(parts[6:])
        body_lines: list[str] = []
        files = ins = dels = 0
        dirs: dict[str, int] = {}
        for line in tail.split("\n"):
            cols = line.split("\t")
            if len(cols) == 3 and (cols[0].isdigit() or cols[0] == "-") and (cols[1].isdigit() or cols[1] == "-"):
                files += 1
                ins += int(cols[0]) if cols[0].isdigit() else 0
                dels += int(cols[1]) if cols[1].isdigit() else 0
                top = cols[2].split("/", 1)[0] if "/" in cols[2] else "(root)"
                dirs[top] = dirs.get(top, 0) + 1
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()
        at = parse_iso(when.strip())
        if at is None:
            continue
        is_merge = len(parents.split()) > 1
        jira, ado = extract_ticket_refs(f"{subject}\n{body}", jira_projects=jira_projects)
        refs = [f"jira:{k}" for k in jira] + ([f"ado:{i}" for i in ado] if ado_enabled else [])
        kind = KIND_MERGE if is_merge else KIND_COMMIT
        pr = PR_NUMBER_RE.search(subject)
        meta = {
            "sha": sha[:12],
            "subject": subject[:200],
            "files": files,
            "insertions": ins,
            "deletions": dels,
            "dirs": sorted(dirs, key=dirs.get, reverse=True)[:3],
            "ai": is_ai_assisted(body) or is_ai_assisted(subject),
            "bot": _is_bot(name, email),
            "branch": _branch_from_subject(subject) if is_merge else "",
            "pr": pr.group(1) if pr else "",
            "hour_local": datetime.fromisoformat(when.strip().replace("Z", "+00:00")).hour if when.strip() else None,
            "weekday_local": datetime.fromisoformat(when.strip().replace("Z", "+00:00")).weekday() if when.strip() else None,
        }
        signals.append(Signal(
            id=signal_id("git", repo, sha),
            at=at,
            source="git",
            kind=kind,
            actor=name.strip(),
            actor_email=email.strip().lower(),
            refs=refs,
            repo=repo,
            system=system,
            weight=0.0 if meta["bot"] else weights.get(kind, DEFAULT_WEIGHTS[kind]),
            minutes=None,
            meta=meta,
        ))
    return signals


class GitSource:
    id = "git"
    label = "Git repositories"
    category = "code"

    def __init__(self, repo_root: Path, system_paths: dict[str, str], extra_repos: tuple[str, ...] = (), *, jira_projects: set[str] | None = None, ado_enabled: bool = False, weights: dict[str, float] | None = None, runner=None) -> None:
        self.repo_root = repo_root
        # system id -> repo-root-relative path (only systems that declared one)
        self.system_paths = {k: v for k, v in system_paths.items() if v}
        self.extra_repos = extra_repos
        self.jira_projects = jira_projects
        self.ado_enabled = ado_enabled
        self.weights = weights or {}
        self._run = runner or self._default_runner

    @property
    def configured(self) -> bool:
        return bool(self.system_paths or self.extra_repos)

    def describe(self) -> dict:
        return {
            "id": self.id, "label": self.label, "category": self.category,
            "configured": self.configured,
            "detail": f"{len(self.system_paths)} system paths" + (f" + {len(self.extra_repos)} extra" if self.extra_repos else ""),
        }

    @staticmethod
    def _default_runner(args: list[str], cwd: Path) -> str:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:300] or f"git exited {proc.returncode}")
        return proc.stdout

    def system_for(self, repo_rel: str) -> str | None:
        best: tuple[int, str | None] = (-1, None)
        for system_id, path in self.system_paths.items():
            path = path.rstrip("/")
            if repo_rel == path or repo_rel.startswith(path + "/"):
                if len(path) > best[0]:
                    best = (len(path), system_id)
        return best[1]

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        roots = list(dict.fromkeys(list(self.system_paths.values()) + list(self.extra_repos)))
        repos = discover_repos(self.repo_root, roots)
        since_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo_facts: dict[str, dict] = {}
        for rel in repos:
            path = self.repo_root / rel
            system = self.system_for(rel)
            started = time.time()
            try:
                raw = self._run(["git", "log", "--all", "--no-color", f"--since={since_iso}", f"--format={LOG_FORMAT}", "--numstat"], path)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                result.notes.append(f"{rel}: {exc}")
                repo_facts[rel] = {"system": system, "error": str(exc)[:200], "commits": 0}
                continue
            signals = parse_log(raw, rel, system, jira_projects=self.jira_projects, ado_enabled=self.ado_enabled, weights=self.weights)
            result.signals.extend(signals)
            newest = max((s.at for s in signals), default=None)
            keyed = sum(1 for s in signals if s.refs and s.kind == KIND_COMMIT)
            plain = sum(1 for s in signals if s.kind == KIND_COMMIT and not s.meta.get("bot"))
            repo_facts[rel] = {
                "system": system,
                "commits": len(signals),
                "keyed_commits": keyed,
                "human_commits": plain,
                "newest_at": newest,
                "authors": len({s.actor_email for s in signals if not s.meta.get("bot")}),
                "scan_seconds": round(time.time() - started, 2),
            }
        result.facts = {"repos": repo_facts, "collected_at": time.time()}
        return result
