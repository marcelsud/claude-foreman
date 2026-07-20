from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foreman.config import (
    active_non_subscription_credentials,
    codex_subscription_command,
    default_data_dir,
    subscription_environment,
)


class SubscriptionAuthTests(unittest.TestCase):
    def test_default_data_dir_preserves_pre_rename_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            legacy = home / ".local" / "share" / "claude-foreman"
            legacy.mkdir(parents=True)
            with patch("foreman.config.Path.home", return_value=home):
                self.assertEqual(legacy, default_data_dir())

                current = home / ".local" / "share" / "foreman"
                current.mkdir()
                self.assertEqual(current, default_data_dir())

    def test_codex_command_forces_builtin_chatgpt_subscription_provider(self) -> None:
        self.assertEqual(
            [
                "/usr/bin/codex",
                "-c",
                'model_provider="openai"',
                "-c",
                'forced_login_method="chatgpt"',
                "app-server",
            ],
            codex_subscription_command("/usr/bin/codex", "app-server"),
        )

    def test_worker_environment_removes_all_non_subscription_auth(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "secret",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "OPENAI_API_KEY": "openai-secret",
                "CODEX_ACCESS_TOKEN": "codex-token",
                "KEEP_ME": "yes",
            },
            clear=True,
        ):
            self.assertEqual(
                [
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY",
                ],
                active_non_subscription_credentials(),
            )
            env = subscription_environment()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertEqual("yes", env["KEEP_ME"])
            self.assertEqual("subscription", env["FOREMAN_AUTH_MODE"])
            self.assertEqual("/tmp", env["TMPDIR"])
            self.assertEqual("/tmp", env["TMP"])
            self.assertEqual("/tmp", env["TEMP"])


if __name__ == "__main__":
    unittest.main()
