import configparser
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import agent_env as agent_env_mod


AWS_CONFIG = """\
[sso-session corp]
sso_start_url = https://example.awsapps.com/start
sso_region = us-east-1

[profile app-dev]
sso_session = corp
sso_account_id = 111111111111
sso_role_name = Dev

[profile app-prod]
sso_session = corp
sso_account_id = 222222222222
sso_role_name = Admin

[profile chained]
source_profile = app-dev
role_arn = arn:aws:iam::111111111111:role/x
"""


class AgentEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".env").write_text("JIRA_API_TOKEN=t\nPLAIN_FROM_DOTENV=x\nEPICRIDE_SECRET_A_B={}\n", encoding="utf-8")
        (self.tmp / "aws-config").write_text(AWS_CONFIG, encoding="utf-8")
        agent_env_mod._build.cache_clear()
        self.addCleanup(agent_env_mod._build.cache_clear)
        self.config = mock.patch.object(agent_env_mod, "CONFIG", mock.Mock(repo_root=self.tmp))
        self.config.start()
        self.addCleanup(self.config.stop)

    def _env(self, **extra: str) -> dict:
        base = {
            "PATH": "/bin",
            "JIRA_API_TOKEN": "t",
            "PLAIN_FROM_DOTENV": "x",
            "EPICRIDE_SECRET_A_B": "{}",
            "GITHUB_TOKEN": "g",
            "AWS_ACCESS_KEY_ID": "k",
            "AWS_PROFILE": "app-prod",
            "AWS_CONFIG_FILE": str(self.tmp / "aws-config"),
            "AWS_REGION": "us-east-1",
        }
        base.update(extra)
        with mock.patch.dict(os.environ, base, clear=True):
            return agent_env_mod._build()

    def test_off_without_home(self) -> None:
        env, _ = self._env()
        self.assertIsNone(env)

    def test_isolates_claude_config_and_credentials(self) -> None:
        env, summary = self._env(PM_AGENT_HOME="agent-home", PM_AGENT_AWS_PROFILES="app-dev")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str((self.tmp / "agent-home" / "claude").resolve()))
        for gone in ("JIRA_API_TOKEN", "PLAIN_FROM_DOTENV", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "PM_AGENT_HOME"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["EPICRIDE_SECRET_A_B"], "{}")
        self.assertEqual(env["AWS_REGION"], "us-east-1")
        self.assertEqual(env["PATH"], "/bin")
        self.assertIn("JIRA_API_TOKEN", summary["dropped"])

    def test_aws_config_holds_only_listed_profiles(self) -> None:
        env, summary = self._env(PM_AGENT_HOME=str(self.tmp / "h"), PM_AGENT_AWS_PROFILES="chained,missing")
        self.assertEqual(env["AWS_PROFILE"], "chained")
        written = configparser.RawConfigParser()
        written.read(env["AWS_CONFIG_FILE"], encoding="utf-8")
        self.assertEqual(
            sorted(written.sections()),
            ["profile app-dev", "profile chained", "sso-session corp"],
        )
        self.assertEqual(summary["aws_missing"], ["missing"])
        self.assertEqual(Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text(), "")

    def test_no_profiles_means_no_aws(self) -> None:
        env, _ = self._env(PM_AGENT_HOME=str(self.tmp / "h"))
        self.assertNotIn("AWS_PROFILE", env)
        written = configparser.RawConfigParser()
        written.read(env["AWS_CONFIG_FILE"], encoding="utf-8")
        self.assertEqual(written.sections(), [])


class EnsureLoginTest(unittest.TestCase):
    """The launcher's check: signed in -> nothing; not signed in -> `claude auth login`;
    never an interactive REPL, never blocking the server for a broken CLI."""

    def _run(self, statuses, login_rc=0):
        calls = []
        replies = list(statuses)

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(stdout=replies.pop(0))

        def fake_call(cmd, **kwargs):
            calls.append(cmd)
            return login_rc

        with mock.patch.object(agent_env_mod.subprocess, "run", fake_run), \
                mock.patch.object(agent_env_mod.subprocess, "call", fake_call), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            rc = agent_env_mod.ensure_login({"CLAUDE_CONFIG_DIR": "x"})
        return rc, calls

    def test_signed_in_does_nothing_else(self) -> None:
        rc, calls = self._run(['{"loggedIn": true, "email": "a@b"}'])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["claude", "auth", "status", "--json"]])

    def test_not_signed_in_runs_the_browser_login_and_rechecks(self) -> None:
        rc, calls = self._run(['{"loggedIn": false}', '{"loggedIn": true}'])
        self.assertEqual(rc, 0)
        self.assertIn(["claude", "auth", "login"], calls)
        self.assertEqual(calls[-1], ["claude", "auth", "status", "--json"])

    def test_a_failed_login_is_reported_not_fatal(self) -> None:
        rc, _ = self._run(['{"loggedIn": false}', '{"loggedIn": false}'], login_rc=1)
        self.assertEqual(rc, 1)

    def test_a_missing_cli_is_reported(self) -> None:
        with mock.patch.object(agent_env_mod.subprocess, "run", side_effect=FileNotFoundError), \
                mock.patch("sys.stderr"):
            self.assertEqual(agent_env_mod.auth_status(None)["loggedIn"], False)


class AgentAuthTest(unittest.TestCase):
    """GET /agents/auth and POST /agents/auth/login, below the HTTP layer."""

    def setUp(self) -> None:
        agent_env_mod._auth_cache = None
        agent_env_mod._login_proc = None
        agent_env_mod._login_lines.clear()

    def test_status_is_cached_and_refreshable(self) -> None:
        calls = []
        status = {"loggedIn": True, "email": "a@b", "authMethod": "claude.ai", "subscriptionType": "team"}
        with mock.patch.object(agent_env_mod, "auth_status", side_effect=lambda env: calls.append(1) or status), \
                mock.patch.object(agent_env_mod, "agent_env", return_value={"CLAUDE_CONFIG_DIR": "/h/claude"}):
            first = agent_env_mod.agent_auth()
            agent_env_mod.agent_auth()
            agent_env_mod.agent_auth(refresh=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {k: first[k] for k in ("isolated", "config_dir", "logged_in", "email", "subscription")},
            {"isolated": True, "config_dir": "/h/claude", "logged_in": True, "email": "a@b", "subscription": "team"},
        )

    def test_login_runs_once_at_a_time_in_the_background(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None  # still running
        proc.stdout = iter([])
        with mock.patch.object(agent_env_mod.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(agent_env_mod, "auth_status", return_value={"loggedIn": False}), \
                mock.patch.object(agent_env_mod, "agent_env", return_value=None):
            first = agent_env_mod.start_login()
            agent_env_mod.start_login()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["claude", "auth", "login"])
        self.assertTrue(first["login_running"])

    def test_a_missing_cli_is_reported_without_raising(self) -> None:
        with mock.patch.object(agent_env_mod.subprocess, "Popen", side_effect=FileNotFoundError), \
                mock.patch.object(agent_env_mod, "auth_status", return_value={"loggedIn": False, "error": "x"}), \
                mock.patch.object(agent_env_mod, "agent_env", return_value=None):
            result = agent_env_mod.start_login()
        self.assertFalse(result["login_running"])
        self.assertIn("claude", list(agent_env_mod._login_lines)[0])


if __name__ == "__main__":
    unittest.main()
