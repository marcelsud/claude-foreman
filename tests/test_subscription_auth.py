from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from claude_foreman.config import active_non_subscription_credentials, subscription_environment


class SubscriptionAuthTests(unittest.TestCase):
    def test_worker_environment_removes_all_non_subscription_auth(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "secret",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "KEEP_ME": "yes",
            },
            clear=True,
        ):
            self.assertEqual(
                ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"],
                active_non_subscription_credentials(),
            )
            env = subscription_environment()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertEqual("yes", env["KEEP_ME"])
            self.assertEqual("subscription", env["CLAUDE_FOREMAN_AUTH_MODE"])
            self.assertEqual("/tmp", env["TMPDIR"])
            self.assertEqual("/tmp", env["TMP"])
            self.assertEqual("/tmp", env["TEMP"])


if __name__ == "__main__":
    unittest.main()
