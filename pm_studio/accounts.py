"""Enterprise accounts: users, email invites, and login sessions.

Only reachable when `[enterprise] mode = "enterprise"` (see config.py). In personal
mode nothing here is consulted and the server behaves exactly as it always has - a
single trusted user, no login - which is what keeps upgrading the package a no-op for
existing deployments.

Design notes:

- **Stdlib only.** Password hashing is PBKDF2-HMAC-SHA256 from `hashlib`; tokens come
  from `secrets`. Adding a crypto or ORM dependency to a tool people install with a
  pinned git ref is not worth it for four roles and a cookie.
- **Server-owned JSON**, one file under `workspace/`, written only by the single
  always-running process - deliberately the same shape as sessions.json and the
  roadmap boards (see roadmap.RoadmapStore's docstring for why that matters when
  every PM session is its own git worktree). It is not git-tracked, which is also
  what keeps password hashes out of the repo.
- **Secrets are stored hashed, never in the clear.** A login cookie and an invite
  link both hold a token whose SHA-256 is what lands on disk; the plaintext exists
  only in the response that mints it. An invite link therefore cannot be re-read
  later - revoke and re-invite instead.
- **The roster is the deployment's own data**, like the roadmap: emails and names
  live in the consumer's workspace, never in this package.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .config import CONFIG

ACCOUNTS_PATH = CONFIG.workspace_dir / "accounts.json"

Role = Literal["admin", "pm", "reviewer", "viewer"]
UserStatus = Literal["invited", "active", "disabled"]

ROLES: tuple[Role, ...] = ("admin", "pm", "reviewer", "viewer")

# Human labels for the UI, in privilege order. The package defines the role set;
# a deployment restricts *who* holds which role, not what the roles are.
ROLE_LABELS: dict[str, str] = {
    "admin": "Admin / owner",
    "pm": "PM builder",
    "reviewer": "Reviewer",
    "viewer": "Viewer",
}

INVITE_TTL_SECONDS = 7 * 24 * 3600
LOGIN_TTL_SECONDS = 30 * 24 * 3600

SESSION_COOKIE_NAME = "pm_studio_auth"

# Header and per-process secret the PM agents use to reach their own server.
#
# PM agents talk to this server over `curl` (see agent.py) and have no browser cookie.
# In enterprise mode those calls would otherwise be rejected and the core loop would
# stop working, so the process mints one token at startup and injects it into the
# prompts' curl examples. It is never persisted and dies with the process.
#
# The token is not an escalation path: an agent's Bash allowlist already matches its
# curl commands literally, so it can only reach the handful of endpoints for its own
# session and its own product's board - exactly as in personal mode. What the token
# adds is that *other* people on the network cannot reach those endpoints at all.
AGENT_HEADER_NAME = "X-PM-Studio-Agent"
AGENT_TOKEN = secrets.token_urlsafe(32)

# PBKDF2 iterations. High enough to be a real cost per guess, low enough that a login
# on a laptop stays imperceptible.
_PBKDF2_ITERATIONS = 240_000
_MIN_PASSWORD_LENGTH = 10


class AccountError(Exception):
    """Any rejected account operation. The message is safe to show a user."""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Returns (hash_hex, salt_hex). A fresh salt is generated when none is given."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    # Constant-time: a timing side channel on a local tool is not a realistic threat,
    # but comparing digests correctly costs nothing.
    return secrets.compare_digest(candidate, password_hash)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_role(role: str) -> Role:
    if role not in ROLES:
        raise AccountError(f"Unknown role: {role!r}. Must be one of: {', '.join(ROLES)}")
    return role  # type: ignore[return-value]


def validate_password(password: str) -> str:
    if len(password or "") < _MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
        )
    return password


@dataclass
class User:
    id: str
    email: str
    name: str
    role: Role
    status: UserStatus
    created_at: float
    updated_at: float
    # None until the user sets a password by accepting their invite.
    password_hash: str | None = None
    password_salt: str | None = None
    last_login_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        """Everything except the credential material. Every API response and websocket
        broadcast uses this - `to_dict` is for persistence only, so a hash cannot be
        leaked by adding a new endpoint that forgets to strip it."""
        data = self.to_dict()
        data.pop("password_hash", None)
        data.pop("password_salt", None)
        data["role_label"] = ROLE_LABELS.get(self.role, self.role)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        return cls(**data)


@dataclass
class Invite:
    id: str
    email: str
    role: Role
    token_hash: str
    invited_by: str
    created_at: float
    expires_at: float
    accepted_at: float | None = None
    revoked_at: float | None = None

    @property
    def is_open(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > time.time()
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        data = self.to_dict()
        data.pop("token_hash", None)
        data["is_open"] = self.is_open
        data["role_label"] = ROLE_LABELS.get(self.role, self.role)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Invite":
        return cls(**data)


@dataclass
class LoginSession:
    token_hash: str
    user_id: str
    created_at: float
    expires_at: float

    @property
    def is_valid(self) -> bool:
        return self.expires_at > time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LoginSession":
        return cls(**data)


@dataclass
class NewInvite:
    """An invite plus the one-time plaintext token, returned only from `invite()`."""

    invite: Invite
    token: str

    def accept_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/accept-invite?token={self.token}"


def agent_principal() -> User:
    """The synthetic identity a PM agent's own curl calls act under (see AGENT_TOKEN).

    Given the `pm` role because that is exactly what an agent does - dispatch dev work
    and keep its product's board current - and deliberately never `admin`: an agent has
    no business touching the roster or cost data. It is not a row in accounts.json and
    cannot log in.
    """
    return User(
        id="agent",
        email="agent@localhost",
        name="PM agent",
        role="pm",
        status="active",
        created_at=0.0,
        updated_at=0.0,
    )


def is_agent_token(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, AGENT_TOKEN)


class AccountStore:
    """The user roster, open invites and live login sessions for one deployment.

    Login sessions are persisted (as token hashes) rather than kept in memory so a
    server restart - which happens every time the operator reinstalls a pinned tag -
    doesn't sign everybody out mid-session.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or ACCOUNTS_PATH
        self._users: dict[str, User] = {}
        self._invites: dict[str, Invite] = {}
        self._logins: dict[str, LoginSession] = {}
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._users = {u["id"]: User.from_dict(u) for u in raw.get("users", [])}
        self._invites = {i["id"]: Invite.from_dict(i) for i in raw.get("invites", [])}
        self._logins = {
            s["token_hash"]: LoginSession.from_dict(s) for s in raw.get("logins", [])
        }
        # Drop anything already expired rather than carrying dead rows forever.
        self._logins = {h: s for h, s in self._logins.items() if s.is_valid}

    def _save(self) -> None:
        """Caller holds the lock. Written 0600: this file holds password hashes and
        live session tokens, so it should not be world-readable even on a single-user
        machine."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "users": [u.to_dict() for u in self._users.values()],
            "invites": [i.to_dict() for i in self._invites.values()],
            "logins": [s.to_dict() for s in self._logins.values() if s.is_valid],
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    # ---- reads ----

    @property
    def is_empty(self) -> bool:
        return not self._users

    @property
    def needs_setup(self) -> bool:
        """True when enterprise mode is on but nobody can administer it yet - the
        first-run state that the setup page exists to resolve."""
        return not any(u.role == "admin" and u.status == "active" for u in self._users.values())

    def list_users(self) -> list[dict]:
        return [
            u.to_public_dict()
            for u in sorted(self._users.values(), key=lambda u: (u.created_at, u.email))
        ]

    def list_invites(self, include_closed: bool = False) -> list[dict]:
        invites = [
            i for i in self._invites.values() if include_closed or i.is_open
        ]
        return [i.to_public_dict() for i in sorted(invites, key=lambda i: i.created_at)]

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def get_by_email(self, email: str) -> User | None:
        target = normalize_email(email)
        for user in self._users.values():
            if user.email == target:
                return user
        return None

    # ---- user lifecycle ----

    def create_owner(self, email: str, name: str, password: str) -> User:
        """First-run only: mints the admin/owner account. Refuses once any admin
        exists, so the setup page cannot be replayed by whoever reaches it next."""
        with self._lock:
            if not self.needs_setup:
                raise AccountError("This instance is already set up.")
            return self._create_user_locked(
                email=email, name=name, role="admin", status="active", password=password
            )

    def _create_user_locked(
        self,
        email: str,
        name: str,
        role: str,
        status: UserStatus,
        password: str | None = None,
    ) -> User:
        normalized = normalize_email(email)
        if not normalized or "@" not in normalized:
            raise AccountError("A valid email address is required.")
        if any(u.email == normalized for u in self._users.values()):
            raise AccountError(f"{normalized} is already a user here.")
        password_hash = password_salt = None
        if password is not None:
            password_hash, password_salt = hash_password(validate_password(password))
        now = time.time()
        user = User(
            id=uuid.uuid4().hex[:12],
            email=normalized,
            name=(name or "").strip() or normalized.split("@")[0],
            role=validate_role(role),
            status=status,
            created_at=now,
            updated_at=now,
            password_hash=password_hash,
            password_salt=password_salt,
        )
        self._users[user.id] = user
        self._save()
        return user

    def set_role(self, user_id: str, role: str) -> User:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                raise AccountError("Unknown user.")
            new_role = validate_role(role)
            if user.role == "admin" and new_role != "admin" and self._sole_admin(user_id):
                # Demoting the last admin would leave nobody able to manage the
                # roster, with no way back short of hand-editing accounts.json.
                raise AccountError("This is the only admin - promote someone else first.")
            user.role = new_role
            user.updated_at = time.time()
            self._save()
            return user

    def set_status(self, user_id: str, status: UserStatus) -> User:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                raise AccountError("Unknown user.")
            if status == "disabled" and user.role == "admin" and self._sole_admin(user_id):
                raise AccountError("This is the only admin - promote someone else first.")
            user.status = status
            user.updated_at = time.time()
            if status == "disabled":
                # Disabling must take effect immediately, not whenever the cookie
                # happens to expire.
                self._revoke_user_logins_locked(user_id)
            self._save()
            return user

    def _sole_admin(self, user_id: str) -> bool:
        admins = [
            u.id for u in self._users.values() if u.role == "admin" and u.status == "active"
        ]
        return admins == [user_id]

    # ---- invites ----

    def invite(self, email: str, role: str, invited_by: str) -> NewInvite:
        """Creates an invited (password-less) user plus a one-time invite token. The
        plaintext token is returned once, for the mail body or a copyable link."""
        with self._lock:
            normalized = normalize_email(email)
            existing = self.get_by_email(normalized)
            if existing is not None and existing.status != "invited":
                raise AccountError(f"{normalized} is already a user here.")
            if existing is None:
                self._create_user_locked(
                    email=normalized, name="", role=role, status="invited"
                )
            else:
                # Re-inviting somebody who never accepted: keep the row, refresh the
                # role, and supersede the old token below.
                existing.role = validate_role(role)
                existing.updated_at = time.time()
            for invite in self._invites.values():
                if invite.email == normalized and invite.is_open:
                    invite.revoked_at = time.time()
            token = secrets.token_urlsafe(32)
            now = time.time()
            invite = Invite(
                id=uuid.uuid4().hex[:12],
                email=normalized,
                role=validate_role(role),
                token_hash=hash_token(token),
                invited_by=invited_by,
                created_at=now,
                expires_at=now + INVITE_TTL_SECONDS,
            )
            self._invites[invite.id] = invite
            self._save()
            return NewInvite(invite=invite, token=token)

    def revoke_invite(self, invite_id: str) -> Invite:
        with self._lock:
            invite = self._invites.get(invite_id)
            if invite is None:
                raise AccountError("Unknown invite.")
            invite.revoked_at = time.time()
            user = self.get_by_email(invite.email)
            # An invited user who never set a password has no reason to linger.
            if user is not None and user.status == "invited":
                self._users.pop(user.id, None)
            self._save()
            return invite

    def peek_invite(self, token: str) -> Invite:
        """Resolves a token for the accept page, without consuming it."""
        digest = hash_token(token)
        for invite in self._invites.values():
            if secrets.compare_digest(invite.token_hash, digest):
                if not invite.is_open:
                    raise AccountError("This invite has expired or already been used.")
                return invite
        raise AccountError("This invite link is not valid.")

    def accept_invite(self, token: str, name: str, password: str) -> User:
        """Consumes an invite: sets the user's name and password and activates them."""
        with self._lock:
            invite = self.peek_invite(token)
            user = self.get_by_email(invite.email)
            if user is None:
                user = self._create_user_locked(
                    email=invite.email, name=name, role=invite.role, status="invited"
                )
            password_hash, password_salt = hash_password(validate_password(password))
            user.name = (name or "").strip() or user.name
            user.password_hash = password_hash
            user.password_salt = password_salt
            user.status = "active"
            user.role = invite.role
            user.updated_at = time.time()
            invite.accepted_at = time.time()
            self._save()
            return user

    # ---- authentication ----

    def authenticate(self, email: str, password: str) -> User:
        """Verifies credentials. Every failure returns the same message: whether an
        address is registered here is not something a login form should confirm."""
        generic = AccountError("Incorrect email or password.")
        user = self.get_by_email(email)
        if user is None or not user.password_hash or not user.password_salt:
            raise generic
        if user.status != "active":
            raise AccountError("This account is not active. Ask an admin.")
        if not verify_password(password, user.password_hash, user.password_salt):
            raise generic
        with self._lock:
            user.last_login_at = time.time()
            self._save()
        return user

    def start_login(self, user_id: str) -> str:
        """Mints a login token. The plaintext is returned for the cookie; only its
        hash is stored."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._logins[hash_token(token)] = LoginSession(
                token_hash=hash_token(token),
                user_id=user_id,
                created_at=now,
                expires_at=now + LOGIN_TTL_SECONDS,
            )
            self._save()
        return token

    def resolve_login(self, token: str | None) -> User | None:
        """Cookie token -> active user, or None. Used on every authenticated request,
        so it stays read-only and lock-free on the hot path."""
        if not token:
            return None
        login = self._logins.get(hash_token(token))
        if login is None or not login.is_valid:
            return None
        user = self._users.get(login.user_id)
        if user is None or user.status != "active":
            return None
        return user

    def end_login(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._logins.pop(hash_token(token), None) is not None:
                self._save()

    def _revoke_user_logins_locked(self, user_id: str) -> None:
        self._logins = {
            h: s for h, s in self._logins.items() if s.user_id != user_id
        }
