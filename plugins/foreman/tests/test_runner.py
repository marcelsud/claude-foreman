from __future__ import annotations

import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk.types import PermissionResultAllow

from foreman.approval_policy import request_hash
from foreman.config import ForemanConfig
from foreman.database import ForemanDB
from foreman.runner import ClaudeWorker
from foreman.worktrees import Worktree


class RunnerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_fails_closed_before_creating_worktree_without_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            config = ForemanConfig(
                data_dir=data,
                db_path=data / "state.db",
                worktrees_dir=data / "worktrees",
                logs_dir=data / "logs",
                pid_path=data / "foremand.pid",
            )
            db = ForemanDB(config.db_path)
            db.initialize()
            task = db.create_task(repo_path=temp, prompt="do work")

            with patch("foreman.runner.missing_sandbox_dependencies", return_value=["socat"]):
                await ClaudeWorker(config, db).run(task)

            failed = db.get_task(task.id)
            self.assertEqual("failed", failed.status)
            self.assertIn("refusing to run without", failed.error or "")
            self.assertIsNone(failed.worktree_path)

    async def test_daemon_shutdown_requeues_in_progress_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ForemanConfig(
                data_dir=root,
                db_path=root / "state.db",
                worktrees_dir=root / "worktrees",
                logs_dir=root / "logs",
                pid_path=root / "foremand.pid",
                poll_interval=0.01,
            )
            db = ForemanDB(config.db_path)
            db.initialize()
            task = db.create_task(repo_path=temp, prompt="do work")
            worker = ClaudeWorker(config, db)

            async def wait_forever(*_args):
                await asyncio.Event().wait()

            fake_worktree = Worktree(path=root, branch="foreman/test", repo_root=root)
            with (
                patch("foreman.runner.missing_sandbox_dependencies", return_value=[]),
                patch.object(worker.worktrees, "ensure", return_value=fake_worktree),
                patch.object(worker, "_query", side_effect=wait_forever),
            ):
                running = asyncio.create_task(worker.run(task))
                for _ in range(100):
                    if db.get_task(task.id).status == "running":
                        break
                    await asyncio.sleep(0.01)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running

            interrupted = db.get_task(task.id)
            self.assertEqual("queued", interrupted.status)
            self.assertIn("daemon shutdown", interrupted.error or "")

    async def test_question_answers_resume_the_permission_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ForemanConfig(
                data_dir=root,
                db_path=root / "state.db",
                worktrees_dir=root / "worktrees",
                logs_dir=root / "logs",
                pid_path=root / "foremand.pid",
                poll_interval=0.01,
            )
            db = ForemanDB(config.db_path)
            db.initialize()
            task = db.create_task(repo_path=temp, prompt="ask first")
            run_id = db.create_run(task.id)
            worker = ClaudeWorker(config, db)
            question_input = {
                "questions": [
                    {
                        "question": "Which database?",
                        "options": [{"label": "SQLite"}, {"label": "Postgres"}],
                        "multiSelect": False,
                    }
                ]
            }

            async def fake_query(*, prompt, options):
                hook = options.hooks["PreToolUse"][0].hooks[0]
                hook_result = await hook(
                    {"tool_name": "AskUserQuestion", "tool_input": question_input},
                    "tool-use-id",
                    {},
                )
                self.assertTrue(hook_result["continue_"])
                self.assertEqual(
                    "ask",
                    hook_result["hookSpecificOutput"]["permissionDecision"],
                )
                waiting = asyncio.create_task(
                    options.can_use_tool("AskUserQuestion", question_input, {})
                )
                for _ in range(100):
                    approvals = db.approvals_for_task(task.id)
                    if approvals:
                        break
                    await asyncio.sleep(0.01)
                approval = approvals[0]
                db.decide_approval(
                    approval["id"],
                    approve=True,
                    decided_by="codex",
                    request_hash=request_hash(task.id, "AskUserQuestion", question_input),
                    response={"answers": {"Which database?": "SQLite"}},
                )
                result = await waiting
                self.assertIsInstance(result, PermissionResultAllow)
                self.assertEqual(
                    {"Which database?": "SQLite"},
                    result.updated_input["answers"],
                )
                if False:
                    yield None

            with patch("claude_agent_sdk.query", fake_query):
                result = await worker._query(task, run_id, root)
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
