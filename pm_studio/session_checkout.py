"""On-demand product-code checkouts inside session worktrees.

A non-default session is a `git worktree` of the deployment repo. When product code
lives in git SUBMODULES, that worktree materializes each gitlink as an empty
directory - the session cannot see or change product code. This module fills those
directories on demand, without any network clone:

- `ensure_system_checkout` turns every submodule under a system's path into a LINKED
  WORKTREE of the primary checkout's submodule repository (`git -C <primary>/<P>
  worktree add --detach <session>/<P> <sha>`), at exactly the commit the session's
  branch pins in its gitlink. Objects and refs are shared with the primary clone, so it
  is cheap and the session's commits/branches are immediately visible there. Heavy
  dependency directories (`node_modules`, ...) are shared from the primary checkout via
  directory links (Windows junctions, POSIX symlinks) instead of being reinstalled.
- `release_system_checkouts` undoes it before the session worktree is removed - which
  git itself requires (`git worktree remove` refuses a worktree containing populated
  submodules). Uncommitted work and commits reachable from no branch/remote are never
  discarded silently: such a checkout is kept and named in a note starting with
  `KEPT_PREFIX`, unless `force=True`.

The links created for `link_paths` are recorded in a small manifest inside the session
worktree's private git dir (`<git-dir>/pm-studio-links.json`), so release removes
exactly what ensure created and never anything else - and the manifest disappears with
the worktree.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

__all__ = [
    "CheckoutError",
    "KEPT_PREFIX",
    "ensure_system_checkout",
    "release_system_checkouts",
]

KEPT_PREFIX = "kept (has unsaved work): "
"""Every release note for a checkout that was NOT removed because it holds work that
would be lost starts with this - callers detect skipped checkouts with `startswith`."""

_MANIFEST_NAME = "pm-studio-links.json"
_PRUNED_DIRS = {"node_modules", ".git"}
_GIT_TIMEOUT = 300
_FETCH_TIMEOUT = 600
_IS_WINDOWS = sys.platform == "win32"
_lock = threading.Lock()  # one checkout/release at a time: they share primary repos' worktree registries


class CheckoutError(Exception):
    """No checkout under the requested system could be prepared at all."""


# ---------------------------------------------------------------- git helpers

def _git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if _IS_WINDOWS:
        cmd += ["-c", "core.longpaths=true"]
    cmd += ["-C", str(cwd), *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _git_out(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _err(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {proc.returncode}"


def _submodule_paths(repo: Path) -> list[str]:
    """Paths declared in `repo/.gitmodules` (relative to `repo`), in file order."""
    if not (repo / ".gitmodules").is_file():
        return []
    out = _git_out(repo, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    paths = []
    for line in out.splitlines():
        _, _, value = line.partition(" ")
        value = value.strip().strip("/")
        if value:
            paths.append(value)
    return paths


def _is_initialized(path: Path) -> bool:
    return (path / ".git").exists()


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_under(path: str | Path, root: str | Path) -> bool:
    p, r = _norm(path), _norm(root).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def _all_submodules(repo_root: Path, system_path: str | None = None) -> list[str]:
    """Repo-root-relative paths of the submodules to manage, parents before children.

    Top level: the superproject's `.gitmodules` entries equal to or under `system_path`
    (all of them when `system_path` is None). Nested: a submodule's own `.gitmodules`
    entries, recursively - but only below submodules initialized in the primary
    checkout, and only nested ones that are themselves initialized there."""
    prefix = (system_path or "").replace("\\", "/").strip("/")
    result: list[str] = []

    def walk(rel: str) -> None:
        result.append(rel)
        primary = repo_root / rel
        if not _is_initialized(primary):
            return
        for child in _submodule_paths(primary):
            child_rel = f"{rel}/{child}"
            if _is_initialized(repo_root / child_rel):
                walk(child_rel)

    for rel in _submodule_paths(repo_root):
        if not prefix or rel == prefix or rel.startswith(prefix + "/"):
            walk(rel)
    return result


def _pinned_commit(container: Path, rel_in_container: str) -> str | None:
    """The gitlink commit `container`'s HEAD records for `rel_in_container`."""
    out = _git_out(container, "ls-tree", "HEAD", "--", rel_in_container)
    for line in out.splitlines():
        meta, _, _ = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "commit":
            return parts[2]
    return None


def _has_commit(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _is_empty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not _is_link(path) and not any(os.scandir(path))
    except OSError:
        return False


# ---------------------------------------------------------------- directory links

def _is_link(path: Path) -> bool:
    try:
        return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))
    except OSError:
        return False


def _make_dir_link(link: Path, target: Path) -> None:
    if _IS_WINDOWS:
        # A junction needs no admin rights / Developer Mode (a directory symlink does).
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, AttributeError):
            pass
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise OSError(f"mklink /J failed: {(proc.stderr or proc.stdout).strip()}")
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def _remove_dir_link(link: Path) -> None:
    """Removes the link itself, never its target. On Windows a junction is removed with
    rmdir (shutil.rmtree would follow it into the primary checkout)."""
    if _IS_WINDOWS:
        os.rmdir(link)
    else:
        os.unlink(link)


def _has_magic(part: str) -> bool:
    return any(ch in part for ch in "*?[")


def _expand(repo_root: Path, pattern: str) -> list[str]:
    """Repo-root-relative directories matching `pattern` (`*`, `?`, `[...]` per segment,
    `**` = zero or more directories). `**` never descends into node_modules or .git, and
    never follows links, so expansion stays fast on dependency-heavy trees."""
    parts = [p for p in pattern.replace("\\", "/").strip("/").split("/") if p and p != "."]
    found: list[str] = []

    def walk(base: Path, rel: str, rest: list[str]) -> None:
        if not rest:
            if base.is_dir():
                found.append(rel)
            return
        head, tail = rest[0], rest[1:]
        if head == "**":
            walk(base, rel, tail)
            for entry in _subdirs(base):
                if entry.name not in _PRUNED_DIRS:
                    walk(Path(entry.path), _join(rel, entry.name), rest)
        elif _has_magic(head):
            for entry in _subdirs(base):
                if fnmatch.fnmatchcase(entry.name, head):
                    walk(Path(entry.path), _join(rel, entry.name), tail)
        else:
            nxt = base / head
            if nxt.is_dir():
                walk(nxt, _join(rel, head), tail)

    walk(repo_root, "", parts)
    return sorted(set(r for r in found if r))


def _subdirs(base: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(base) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False) and not _is_link(Path(e.path))]
    except OSError:
        return []


def _join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _manifest_path(worktree: Path) -> Path | None:
    git_dir = _git_out(worktree, "rev-parse", "--absolute-git-dir")
    return Path(git_dir) / _MANIFEST_NAME if git_dir else None


def _read_manifest(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data.get("links", [])]
    except (OSError, ValueError, AttributeError):
        return []


def _write_manifest(path: Path | None, links: list[str]) -> None:
    if path is None:
        return
    if links:
        path.write_text(json.dumps({"links": sorted(set(links))}, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()


# ---------------------------------------------------------------- public API

def ensure_system_checkout(
    repo_root: Path, worktree: Path, system_path: str, link_paths: tuple[str, ...] = (),
) -> list[str]:
    """Idempotent. Makes every submodule under `system_path` (repo-root-relative, e.g.
    "src/epicride") usable inside `worktree`, and links heavy dependency dirs from the
    primary checkout. Returns notes.

    No submodules under `system_path` (or an empty `system_path` in a repo without any)
    is not an error: the system's code is then plain files of the superproject, which
    the session worktree already has - there is simply nothing to check out, and the
    links are still made. It raises only when submodules exist and NONE of them could
    be prepared."""
    repo_root, worktree = Path(repo_root), Path(worktree)
    if _norm(repo_root) == _norm(worktree):
        return ["the primary checkout already has every initialized submodule; nothing to do"]
    if not worktree.is_dir():
        raise CheckoutError(f"session worktree {worktree} does not exist")
    notes: list[str] = []
    with _lock:
        submodules = _all_submodules(repo_root, system_path)
        if not submodules:
            notes.append(
                f"no submodules under '{system_path or '.'}'; using the session worktree as is"
            )
            notes.extend(_ensure_links(repo_root, worktree, link_paths))
            return notes
        ready = 0
        for rel in submodules:
            ok, note = _ensure_one(repo_root, worktree, rel)
            ready += ok
            if note:
                notes.append(note)
        if ready == 0:
            raise CheckoutError(
                f"no checkout under '{system_path}' could be prepared: " + "; ".join(notes)
            )
        notes.extend(_ensure_links(repo_root, worktree, link_paths))
    return notes


def _ensure_one(repo_root: Path, worktree: Path, rel: str) -> tuple[bool, str]:
    primary = repo_root / rel
    target = worktree / rel
    if (target / ".git").exists():
        return True, ""  # already a checkout - idempotent, leave it exactly as it is
    if not _is_initialized(primary):
        return False, f"{rel}: not initialized in the primary checkout (run `git submodule update --init {rel}` there); skipped"
    if target.exists() and not _is_empty_dir(target):
        return False, f"{rel}: {target} exists and is not empty; left untouched"

    # The gitlink is recorded by the repository that directly contains `rel` - the
    # session superproject, or (nested) the parent submodule's session checkout.
    container, rel_in = worktree, rel
    for parent in _parents(rel):
        if rel in _nested_of(repo_root, parent):
            if not (worktree / parent / ".git").exists():
                return False, f"{rel}: its parent submodule {parent} has no session checkout; skipped"
            container, rel_in = worktree / parent, rel[len(parent) + 1:]
            break
    sha = _pinned_commit(container, rel_in)
    note = ""
    if sha is None:
        return False, f"{rel}: the session branch records no gitlink for it (sync main into the session first); skipped"
    if not _has_commit(primary, sha):
        fetch = _git(primary, "fetch", "--quiet", "origin", sha, timeout=_FETCH_TIMEOUT)
        if fetch.returncode != 0 or not _has_commit(primary, sha):
            head = _git_out(primary, "rev-parse", "HEAD")
            if not head:
                return False, f"{rel}: pinned commit {sha[:12]} unavailable and primary HEAD unreadable; skipped"
            note = (f"{rel}: pinned commit {sha[:12]} is not available locally and could not be "
                    f"fetched; checked out the primary submodule's HEAD {head[:12]} instead "
                    f"(the superproject will show this submodule as modified)")
            sha = head

    target.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(primary, "worktree", "add", "--detach", str(target), sha)
    if proc.returncode != 0:
        return False, f"{rel}: git worktree add failed: {_err(proc)}"
    return True, note or f"{rel}: checked out at {sha[:12]} (detached; create a feature branch before committing)"


def _parents(rel: str) -> list[str]:
    """Ancestor paths of `rel`, deepest first."""
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def _nested_of(repo_root: Path, parent: str) -> set[str]:
    return {f"{parent}/{p}" for p in _submodule_paths(repo_root / parent)}


def _ensure_links(repo_root: Path, worktree: Path, link_paths: tuple[str, ...]) -> list[str]:
    if not link_paths:
        return []
    notes: list[str] = []
    manifest = _manifest_path(worktree)
    created = _read_manifest(manifest)
    for pattern in link_paths:
        matches = _expand(repo_root, pattern)
        if not matches:
            notes.append(f"link {pattern}: nothing matches in the primary checkout")
            continue
        for rel in matches:
            link = worktree / rel
            if os.path.lexists(link):
                continue  # never overwrite: an existing link (idempotent) or a real directory
            if not link.parent.is_dir():
                notes.append(f"link {rel}: parent directory missing in the session (checkout not prepared?); skipped")
                continue
            try:
                _make_dir_link(link, repo_root / rel)
            except OSError as exc:
                notes.append(f"link {rel}: could not create ({exc})")
                continue
            created.append(rel)
            notes.append(f"link {rel} -> primary checkout")
    _write_manifest(manifest, created)
    return notes


def release_system_checkouts(repo_root: Path, worktree: Path, force: bool = False) -> list[str]:
    """Removes the submodule worktrees created inside `worktree` (called before the
    session worktree is removed). A checkout with uncommitted changes, or with commits
    reachable from no branch or remote, is kept and reported with a note starting with
    `KEPT_PREFIX` - unless `force` is True, which discards that work."""
    repo_root, worktree = Path(repo_root), Path(worktree)
    if not worktree.is_dir() or _norm(repo_root) == _norm(worktree):
        return []
    notes: list[str] = []
    with _lock:
        # 1. links first, so a link inside a submodule checkout never looks like work,
        #    and `git worktree remove` never walks into a junction's target.
        manifest = _manifest_path(worktree)
        remaining: list[str] = []
        for rel in _read_manifest(manifest):
            link = worktree / rel
            if not os.path.lexists(link):
                continue
            if not _is_link(link):
                notes.append(f"link {rel}: is no longer a link; left untouched")
                continue
            try:
                _remove_dir_link(link)
            except OSError as exc:
                remaining.append(rel)
                notes.append(f"link {rel}: could not remove ({exc})")
        _write_manifest(manifest, remaining)

        # 2. submodule worktrees, deepest first (a nested checkout lives inside its parent's).
        kept: list[str] = []
        touched: list[Path] = []
        for rel in sorted(_all_submodules(repo_root), key=lambda r: r.count("/"), reverse=True):
            primary = repo_root / rel
            target = worktree / rel
            if not _is_initialized(primary) or not (target / ".git").is_file():
                continue
            if not _listed_worktree(primary, target):
                continue  # a checkout we did not create (e.g. a real clone) - not ours to remove
            if not force:
                reason = _unsaved_work(target)
                if not reason and any(_is_under(k, target) for k in kept):
                    reason = "contains a kept nested checkout"
                if reason:
                    kept.append(str(target))
                    notes.append(f"{KEPT_PREFIX}{rel} ({reason}) at {target}")
                    continue
            proc = _git(primary, "worktree", "remove", "--force", str(target))
            if proc.returncode != 0:
                kept.append(str(target))
                notes.append(f"{KEPT_PREFIX}{rel} (git worktree remove failed: {_err(proc)})")
                continue
            target.mkdir(parents=True, exist_ok=True)  # back to the plain empty gitlink dir
            touched.append(primary)
            notes.append(f"{rel}: removed session checkout")
        for primary in touched:
            _git(primary, "worktree", "prune")
    return notes


def _listed_worktree(primary: Path, target: Path) -> bool:
    out = _git_out(primary, "worktree", "list", "--porcelain")
    want = _norm(target)
    return any(
        line.startswith("worktree ") and _norm(line[len("worktree "):]) == want
        for line in out.splitlines()
    )


def _unsaved_work(checkout: Path) -> str:
    """Why removing `checkout` would lose work, or "" when it would not."""
    status = _git(checkout, "status", "--porcelain", "--ignore-submodules=none")
    if status.returncode != 0:
        return f"git status failed: {_err(status)}"
    if status.stdout.strip():
        return "uncommitted changes"
    orphans = _git(checkout, "rev-list", "--max-count=1", "HEAD", "--not", "--branches", "--remotes")
    if orphans.returncode != 0:
        return f"git rev-list failed: {_err(orphans)}"
    if orphans.stdout.strip():
        return "commits on no branch or remote"
    return ""
