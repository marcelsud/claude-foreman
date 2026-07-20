from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_foreman.config import ForemanConfig
from claude_foreman.database import ForemanDB
from claude_foreman.verification import run_verification_gates


class FakeCommandServer:
    requests: list[tuple[str, dict]] = []

    def __init__(self, _config):
        pass

    async def __aenter__(self):
        self.requests.clear()
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, params):
        self.requests.append((method, params))
        return {"exitCode": 0, "stdout": "31 tests passed\n", "stderr": ""}


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_gate_runs_in_codex_workspace_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            data = root / "data"
            config = ForemanConfig(
                data_dir=data, db_path=data / "state.db",
                worktrees_dir=data / "worktrees", logs_dir=data / "logs",
                pid_path=data / "foremand.pid",
            )
            db = ForemanDB(config.db_path)
            db.initialize()
            task = db.create_task(
                repo_path=str(root), prompt="work",
                verification_commands=["python3 -m unittest"],
            )
            run_id = db.create_run(task.id)

            with patch(
                "claude_foreman.verification._AppServerCommands", FakeCommandServer
            ):
                summary = await run_verification_gates(
                    config, db, task, run_id, root
                )

            self.assertTrue(summary["required_ok"])
            result = summary["gates"][0]["result"]
            self.assertEqual("passed", result["status"])
            self.assertEqual(0, result["exit_code"])
            self.assertTrue(result["snapshot_sha"].startswith("sha256:"))
            method, params = FakeCommandServer.requests[0]
            self.assertEqual("command/exec", method)
            self.assertEqual(["python3", "-m", "unittest"], params["command"])
            self.assertEqual("workspaceWrite", params["sandboxPolicy"]["type"])
            self.assertFalse(params["sandboxPolicy"]["networkAccess"])


if __name__ == "__main__":
    unittest.main()
