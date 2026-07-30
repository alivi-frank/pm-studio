"""Tests for the enterprise account store: first-run owner setup, the invite ->
accept flow, login sessions, and the guards that stop an instance from being locked
out of its own admin role.

Deliberately store-level (no HTTP): the package's existing suite runs without
fastapi installed, so the logic worth protecting lives here rather than behind a
TestClient.
"""

import tempfile
import time
import unittest
from pathlib import Path

from pm_studio.accounts import (
    INVITE_TTL_SECONDS,
    AccountError,
    AccountStore,
    agent_principal,
    hash_password,
    is_agent_token,
    normalize_email,
    verify_password,
)


class AccountStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "accounts.json"
        self.store = AccountStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _owner(self) -> object:
        return self.store.create_owner("Owner@Example.com ", "Owner", "correct horse battery")

    # ---- first-run setup ----

    def test_needs_setup_until_an_active_admin_exists(self) -> None:
        self.assertTrue(self.store.needs_setup)
        owner = self._owner()
        self.assertFalse(self.store.needs_setup)
        self.assertEqual(owner.role, "admin")
        self.assertEqual(owner.status, "active")
        # Email is normalized so a later login isn't case-sensitive.
        self.assertEqual(owner.email, "owner@example.com")

    def test_setup_cannot_be_replayed(self) -> None:
        self._owner()
        with self.assertRaises(AccountError):
            self.store.create_owner("second@example.com", "Second", "another good password")

    def test_short_password_rejected(self) -> None:
        with self.assertRaises(AccountError):
            self.store.create_owner("owner@example.com", "Owner", "short")

    def test_invalid_email_rejected(self) -> None:
        with self.assertRaises(AccountError):
            self.store.create_owner("not-an-email", "Owner", "correct horse battery")

    # ---- invites ----

    def test_invite_then_accept_activates_the_user(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        invited = self.store.get_by_email("dev@example.com")
        self.assertEqual(invited.status, "invited")
        self.assertIsNone(invited.password_hash)

        user = self.store.accept_invite(new_invite.token, "Dev", "another good password")
        self.assertEqual(user.status, "active")
        self.assertEqual(user.role, "pm")
        self.assertEqual(user.name, "Dev")
        self.assertIsNotNone(user.password_hash)

    def test_invite_token_is_single_use(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        self.store.accept_invite(new_invite.token, "Dev", "another good password")
        with self.assertRaises(AccountError):
            self.store.accept_invite(new_invite.token, "Dev", "yet another password")

    def test_expired_invite_rejected(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        new_invite.invite.expires_at = time.time() - 1
        with self.assertRaises(AccountError):
            self.store.peek_invite(new_invite.token)

    def test_reinviting_supersedes_the_previous_token(self) -> None:
        """An admin re-inviting someone who never accepted must invalidate the old
        link, or a leaked first link would stay usable forever."""
        owner = self._owner()
        first = self.store.invite("dev@example.com", "viewer", owner.id)
        second = self.store.invite("dev@example.com", "pm", owner.id)
        with self.assertRaises(AccountError):
            self.store.peek_invite(first.token)
        # The role from the newest invite is what lands.
        user = self.store.accept_invite(second.token, "Dev", "another good password")
        self.assertEqual(user.role, "pm")

    def test_inviting_an_active_user_is_rejected(self) -> None:
        owner = self._owner()
        with self.assertRaises(AccountError):
            self.store.invite(owner.email, "pm", owner.id)

    def test_revoking_an_invite_drops_the_pending_user(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        self.store.revoke_invite(new_invite.invite.id)
        self.assertIsNone(self.store.get_by_email("dev@example.com"))
        with self.assertRaises(AccountError):
            self.store.peek_invite(new_invite.token)

    def test_open_invites_listing_hides_accepted_ones(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        self.assertEqual(len(self.store.list_invites()), 1)
        self.store.accept_invite(new_invite.token, "Dev", "another good password")
        self.assertEqual(self.store.list_invites(), [])
        self.assertEqual(len(self.store.list_invites(include_closed=True)), 1)

    def test_accept_url_uses_the_deployment_base_url(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        url = new_invite.accept_url("http://127.0.0.1:8000/")
        self.assertTrue(url.startswith("http://127.0.0.1:8000/accept-invite?token="))
        self.assertIn(new_invite.token, url)

    def test_invite_ttl_is_a_week(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        span = new_invite.invite.expires_at - new_invite.invite.created_at
        self.assertAlmostEqual(span, INVITE_TTL_SECONDS, places=3)

    # ---- authentication ----

    def test_login_roundtrip(self) -> None:
        owner = self._owner()
        authenticated = self.store.authenticate("OWNER@example.com", "correct horse battery")
        self.assertEqual(authenticated.id, owner.id)
        token = self.store.start_login(owner.id)
        self.assertEqual(self.store.resolve_login(token).id, owner.id)
        self.store.end_login(token)
        self.assertIsNone(self.store.resolve_login(token))

    def test_wrong_password_and_unknown_email_are_indistinguishable(self) -> None:
        self._owner()
        with self.assertRaises(AccountError) as wrong:
            self.store.authenticate("owner@example.com", "not the password")
        with self.assertRaises(AccountError) as unknown:
            self.store.authenticate("nobody@example.com", "not the password")
        self.assertEqual(str(wrong.exception), str(unknown.exception))

    def test_invited_user_cannot_log_in_before_accepting(self) -> None:
        owner = self._owner()
        self.store.invite("dev@example.com", "pm", owner.id)
        with self.assertRaises(AccountError):
            self.store.authenticate("dev@example.com", "anything at all")

    def test_resolve_login_rejects_garbage_and_none(self) -> None:
        self._owner()
        self.assertIsNone(self.store.resolve_login(None))
        self.assertIsNone(self.store.resolve_login(""))
        self.assertIsNone(self.store.resolve_login("not-a-real-token"))

    def test_disabling_a_user_revokes_their_live_logins(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        user = self.store.accept_invite(new_invite.token, "Dev", "another good password")
        token = self.store.start_login(user.id)
        self.assertIsNotNone(self.store.resolve_login(token))
        self.store.set_status(user.id, "disabled")
        # Immediate, not "whenever the cookie expires".
        self.assertIsNone(self.store.resolve_login(token))

    # ---- roles ----

    def test_last_admin_cannot_be_demoted_or_disabled(self) -> None:
        owner = self._owner()
        with self.assertRaises(AccountError):
            self.store.set_role(owner.id, "pm")
        with self.assertRaises(AccountError):
            self.store.set_status(owner.id, "disabled")

    def test_admin_can_be_demoted_once_another_admin_exists(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("second@example.com", "admin", owner.id)
        self.store.accept_invite(new_invite.token, "Second", "another good password")
        demoted = self.store.set_role(owner.id, "pm")
        self.assertEqual(demoted.role, "pm")

    def test_unknown_role_rejected(self) -> None:
        owner = self._owner()
        with self.assertRaises(AccountError):
            self.store.set_role(owner.id, "superuser")

    # ---- persistence + secret hygiene ----

    def test_state_survives_a_restart(self) -> None:
        owner = self._owner()
        token = self.store.start_login(owner.id)
        reloaded = AccountStore(self.path)
        self.assertFalse(reloaded.needs_setup)
        # Login sessions are persisted as hashes so a server restart - which happens on
        # every reinstall of a pinned tag - doesn't sign everyone out.
        self.assertIsNotNone(reloaded.resolve_login(token))

    def test_public_dict_never_carries_credentials(self) -> None:
        owner = self._owner()
        public = owner.to_public_dict()
        self.assertNotIn("password_hash", public)
        self.assertNotIn("password_salt", public)
        self.assertEqual(public["role_label"], "Admin / owner")
        # And the same for the listing the roster page consumes.
        for row in self.store.list_users():
            self.assertNotIn("password_hash", row)

    def test_stored_file_holds_no_plaintext_secret(self) -> None:
        owner = self._owner()
        new_invite = self.store.invite("dev@example.com", "pm", owner.id)
        token = self.store.start_login(owner.id)
        contents = self.path.read_text()
        self.assertNotIn("correct horse battery", contents)
        self.assertNotIn(new_invite.token, contents)
        self.assertNotIn(token, contents)

    def test_accounts_file_is_not_world_readable(self) -> None:
        self._owner()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class PasswordHashingTest(unittest.TestCase):
    def test_verify_roundtrip(self) -> None:
        digest, salt = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", digest, salt))
        self.assertFalse(verify_password("wrong", digest, salt))

    def test_same_password_gets_a_distinct_salt(self) -> None:
        first, first_salt = hash_password("correct horse battery")
        second, second_salt = hash_password("correct horse battery")
        self.assertNotEqual(first_salt, second_salt)
        self.assertNotEqual(first, second)

    def test_normalize_email(self) -> None:
        self.assertEqual(normalize_email("  Owner@Example.COM "), "owner@example.com")
        self.assertEqual(normalize_email(None), "")


class AgentPrincipalTest(unittest.TestCase):
    def test_agent_is_a_pm_never_an_admin(self) -> None:
        """The agent token must not be a back door to the roster or cost data."""
        agent = agent_principal()
        self.assertEqual(agent.role, "pm")
        self.assertEqual(agent.status, "active")

    def test_agent_token_comparison(self) -> None:
        from pm_studio.accounts import AGENT_TOKEN

        self.assertTrue(is_agent_token(AGENT_TOKEN))
        self.assertFalse(is_agent_token("nope"))
        self.assertFalse(is_agent_token(None))
        self.assertFalse(is_agent_token(""))


if __name__ == "__main__":
    unittest.main()
