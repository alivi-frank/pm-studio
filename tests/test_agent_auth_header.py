"""The PM agents reach their own server over `curl`, and their Bash allowlist matches
those commands as literal prefixes. Two things therefore have to stay true:

1. In personal mode the prompts are byte-identical to what they always were - no
   header, no behavior change for a deployment that never opts into enterprise mode.
2. In enterprise mode the auth header lands AFTER the URL, so `curl -s -X POST <url>`
   is still a matching prefix. A header inserted before the URL would silently break
   every dispatch (the call gets held for an approval a headless session can't grant).
"""

import dataclasses
import unittest

from pm_studio import agent as agent_module
from pm_studio.accounts import AGENT_HEADER_NAME, AGENT_TOKEN
from pm_studio.config import MODE_ENTERPRISE, MODE_PERSONAL


class AgentAuthHeaderTest(unittest.TestCase):
    def _with_mode(self, mode: str):
        return dataclasses.replace(agent_module.CONFIG, mode=mode)

    def test_personal_mode_adds_nothing(self) -> None:
        original = agent_module.CONFIG
        agent_module.CONFIG = self._with_mode(MODE_PERSONAL)
        try:
            self.assertEqual(agent_module.agent_auth_header(), "")
        finally:
            agent_module.CONFIG = original

    def test_enterprise_mode_emits_the_token_header(self) -> None:
        original = agent_module.CONFIG
        agent_module.CONFIG = self._with_mode(MODE_ENTERPRISE)
        try:
            header = agent_module.agent_auth_header()
        finally:
            agent_module.CONFIG = original
        self.assertEqual(header, f' -H "{AGENT_HEADER_NAME}: {AGENT_TOKEN}"')
        # Leading space, no trailing one: it is spliced directly after the URL.
        self.assertTrue(header.startswith(" "))
        self.assertFalse(header.endswith(" "))

    def test_header_placeholder_follows_every_url_in_the_templates(self) -> None:
        """Guards the allowlist contract at the template level: `{auth_header}` must
        never appear before the URL it belongs to."""
        for template in (
            agent_module.PM_SYSTEM_PROMPT_TEMPLATE,
            agent_module.ROADMAP_GUIDANCE_TEMPLATE,
            # A parent product's PM writes to its sub-products' boards with the same
            # curl calls, so its guidance carries the same contract.
            agent_module.PARENT_PRODUCT_GUIDANCE_TEMPLATE,
            agent_module.TRACKER_GUIDANCE_TEMPLATE,
        ):
            for line in template.splitlines():
                stripped = line.strip()
                if "{auth_header}" not in stripped or not stripped.startswith("curl"):
                    continue
                head = stripped.split("{auth_header}")[0]
                # The URL is itself a placeholder (`{tasks_base_url}`, `{roadmap_base_url}`,
                # `{session_meta_url}`), so "the URL came first" means one of those has
                # already been interpolated by this point in the line.
                self.assertTrue(
                    "url}" in head or "http" in head,
                    f"auth header must come after the URL, got: {stripped}",
                )

    def test_every_curl_example_is_authenticated(self) -> None:
        """If a curl example were missed, that one call would 401 in enterprise mode
        while everything around it kept working - the worst kind of partial break."""
        for template in (
            agent_module.PM_SYSTEM_PROMPT_TEMPLATE,
            agent_module.ROADMAP_GUIDANCE_TEMPLATE,
            # A parent product's PM writes to its sub-products' boards with the same
            # curl calls, so its guidance carries the same contract.
            agent_module.PARENT_PRODUCT_GUIDANCE_TEMPLATE,
            agent_module.TRACKER_GUIDANCE_TEMPLATE,
        ):
            for line in template.splitlines():
                stripped = line.strip()
                if stripped.startswith("curl"):
                    self.assertIn("{auth_header}", stripped, f"unauthenticated: {stripped}")


if __name__ == "__main__":
    unittest.main()
