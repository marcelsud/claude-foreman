from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from foreman import __version__
from foreman.approval_policy import request_hash
from foreman.config import ForemanConfig
from foreman.mcp_server import ForemanTools, MCPServer


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
        self.assertEqual("foreman", initialized["result"]["serverInfo"]["name"])
        self.assertEqual(__version__, initialized["result"]["serverInfo"]["version"])
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertIn("task_create", names)
        self.assertIn("approval_decide", names)
        self.assertIn("workflow_run", names)
        self.assertIn("task_usage", names)
        self.assertIn("goal_usage", names)
        self.assertIn("task_wait", names)
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

    def test_task_wait_cursor_is_compatible_with_task_events(self) -> None:
        task = self.toolset.db.create_task(repo_path=self.temp.name, prompt="wait")

        waited = self.call(
            "task_wait",
            {
                "task_id": task.id,
                "after_id": 0,
                "timeout_seconds": 0,
                "actionable_only": False,
            },
        )
        self.assertEqual("events_available", waited["reason"])
        self.assertEqual([], self.call("task_events", {"task_id": task.id, "after_id": waited["cursor"]}))

        event_id = self.toolset.db.add_event(task.id, None, "progress", {"step": 2})
        events = self.call(
            "task_events", {"task_id": task.id, "after_id": waited["cursor"]}
        )
        self.assertEqual([event_id], [event["id"] for event in events])

    def test_stdio_wait_does_not_block_ping_and_writes_are_serialized(self) -> None:
        task = self.toolset.db.create_task(repo_path=self.temp.name, prompt="wait")
        cursor = self.toolset.db.event_tail(task.id, 1)[0]["id"]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "task_wait",
                    "arguments": {
                        "task_id": task.id,
                        "after_id": cursor,
                        "timeout_seconds": 30,
                    },
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        ]
        def request_stream():
            for item in requests:
                yield json.dumps(item) + "\n"
            time.sleep(0.1)

        output_stream = io.StringIO()
        server = MCPServer(
            self.toolset,
            input_stream=request_stream(),
            output_stream=output_stream,
            max_request_workers=1,
            max_wait_workers=1,
        )

        started = time.monotonic()
        server.run()

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual({1, 2}, {response["id"] for response in responses})
        self.assertEqual(2, responses[0]["id"])
        wait_response = next(response for response in responses if response["id"] == 1)
        wait_payload = wait_response["result"]["structuredContent"]["result"]
        self.assertEqual("interrupted", wait_payload["reason"])
        self.assertLess(time.monotonic() - started, 1)

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
