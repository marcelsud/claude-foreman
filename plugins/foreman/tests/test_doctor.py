from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foreman.config import ForemanConfig
from foreman.doctor import run_doctor


class DoctorTests(unittest.TestCase):
    def test_codex_check_uses_same_subscription_provider_as_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ForemanConfig(
                data_dir=root,
                db_path=root / "state.db",
                worktrees_dir=root / "worktrees",
                logs_dir=root / "logs",
                pid_path=root / "foremand.pid",
            )

            def which(command: str) -> str | None:
                return {
                    "codex": "/usr/bin/codex",
                    "git": "/usr/bin/git",
                }.get(command)

            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="Logged in using ChatGPT\n",
                stderr="",
            )
            with (
                patch("foreman.doctor.importlib.util.find_spec", return_value=None),
                patch("foreman.doctor.shutil.which", side_effect=which),
                patch("foreman.doctor.subprocess.run", return_value=completed) as run,
            ):
                result = run_doctor(config)

            codex = result["checks"]["providers"]["codex"]
            self.assertTrue(codex["ok"])
            self.assertEqual("openai", codex["model_provider"])
            self.assertEqual("foreman_process_override", codex["model_provider_source"])
            self.assertEqual("chatgpt", codex["forced_login_method"])
            self.assertEqual(
                [
                    "/usr/bin/codex",
                    "-c",
                    'model_provider="openai"',
                    "-c",
                    'forced_login_method="chatgpt"',
                    "login",
                    "status",
                ],
                run.call_args.args[0],
            )


if __name__ == "__main__":
    unittest.main()
