from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_foreman.approval_policy import request_hash
from claude_foreman.config import ForemanConfig
from claude_foreman.mcp_server import ForemanTools, MCPServer


class MCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.data = data
        config = ForemanConfig(
            data_dir=data,
            db_path=data / "state.db",
            worktrees_dir=data / "worktrees",
            logs_dir=data / "logs",
            pid_path=data / "foremand.pid",
        )
        self.toolset = ForemanTools(config)
        self.server = MCPServer(self.toolset)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, name: str, arguments: dict) -> dict:
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        self.assertFalse(response["result"].get("isError"), response)
        return json.loads(response["result"]["content"][0]["text"])

    def test_initialize_and_tool_list(self) -> None:
        initialized = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual("claude-foreman", initialized["result"]["serverInfo"]["name"])
        self.assertEqual("0.4.0", initialized["result"]["serverInfo"]["version"])
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertIn("task_create", names)
        self.assertIn("approval_decide", names)
        self.assertIn("workflow_run", names)
        self.assertIn("task_usage", names)
        self.assertIn("goal_usage", names)
        create = next(item for item in listed["result"]["tools"] if item["name"] == "task_create")
        properties = create["inputSchema"]["properties"]
        self.assertEqual(["claude", "codex"], properties["provider"]["enum"])
        self.assertIn("ultra", properties["effort"]["enum"])
        self.assertEqual(
            "array", properties["verification_commands"]["type"]
        )

    def test_goal_and_task_roundtrip_without_autostart(self) -> None:
        goal = self.call("goal_create", {"title": "Test goal", "description": "demo"})
        task = self.call(
            "task_create",
            {
                "repo_path": self.temp.name,
                "prompt": "Do something",
                "goal_id": goal["id"],
                "autostart": False,
            },
        )
        fetched = self.call("task_get", {"task_id": task["id"]})
        self.assertEqual("queued", fetched["status"])
        self.assertEqual(goal["id"], fetched["goal_id"])
        compact = self.call("task_list", {})
        self.assertNotIn("prompt", compact[0])
        full = self.call("task_list", {"compact": False})
        self.assertIn("prompt", full[0])

    def test_codex_task_roundtrip(self) -> None:
        task = self.call(
            "task_create",
            {
                "repo_path": self.temp.name,
                "prompt": "Do something",
                "provider": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "autostart": False,
            },
        )
        self.assertEqual("codex", task["provider"])
        self.assertEqual("gpt-5.6-terra", task["model"])

    def test_task_diff_includes_untracked_files_and_is_bounded(self) -> None:
        repo = self.data / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
        task = self.toolset.db.create_task(repo_path=str(repo), prompt="write a large file")
        worktree = self.toolset.worktrees.ensure(task)
        self.toolset.db.update_task(
            task.id,
            worktree_path=str(worktree.path),
            branch_name=worktree.branch,
        )
        (worktree.path / "big.txt").write_text("x" * 5000, encoding="utf-8")

        result = self.toolset._task_diff({"task_id": task.id, "max_chars": 1000})

        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"]), 1000)
        self.assertIn("big.txt", result["diff"])

    def test_clarifying_question_decision_persists_structured_answers(self) -> None:
        task = self.toolset.db.create_task(repo_path=self.temp.name, prompt="choose")
        tool_input = {
            "questions": [
                {
                    "question": "Which database?",
                    "options": [{"label": "SQLite"}, {"label": "Postgres"}],
                    "multiSelect": False,
                }
            ]
        }
        digest = request_hash(task.id, "AskUserQuestion", tool_input)
        approval = self.toolset.db.create_approval(
            task_id=task.id,
            run_id=None,
            tool_name="AskUserQuestion",
            input_data=tool_input,
            request_hash=digest,
            risk="needs_input",
            timeout_seconds=60,
        )

        decided = self.call(
            "approval_decide",
            {
                "approval_id": approval["id"],
                "approve": True,
                "request_hash": digest,
                "answers": {"Which database?": "SQLite"},
            },
        )

        self.assertEqual({"answers": {"Which database?": "SQLite"}}, decided["response"])


if __name__ == "__main__":
    unittest.main()
