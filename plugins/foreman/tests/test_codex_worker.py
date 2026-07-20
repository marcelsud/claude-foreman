from __future__ import annotations

import tempfile
import asyncio
import unittest
from pathlib import Path

from foreman.codex_worker import (
    CodexAppServerWorker,
    CodexUnavailable,
    _redact,
    _subscription_auth_summary,
)
from foreman.config import ForemanConfig
from foreman.database import ForemanDB
from foreman.approval_policy import request_hash


class CodexWorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_worker(self, root: Path) -> tuple[CodexAppServerWorker, ForemanDB]:
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
        task = db.create_task(
            repo_path=str(root), prompt="inspect", provider="codex", model="gpt-5.6-terra"
        )
        worker = CodexAppServerWorker(config, db, "policy")
        worker.task = task
        worker.run_id = db.create_run(task.id)
        worker.worktree = root
        return worker, db

    async def test_routine_command_uses_single_request_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)

            result = await worker._approval_result(
                "item/commandExecution/requestApproval",
                {"command": "git status", "cwd": str(root)},
            )

            self.assertEqual({"decision": "accept"}, result)
            self.assertEqual([], db.approvals_for_task(worker.task.id))

    async def test_non_routine_command_waits_for_exact_durable_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)
            input_data = {
                "command": "custom-generator",
                "cwd": str(root),
                "reason": None,
                "additionalPermissions": None,
                "networkApprovalContext": None,
            }
            waiting = asyncio.create_task(
                worker._approval_result(
                    "item/commandExecution/requestApproval",
                    {"command": "custom-generator", "cwd": str(root)},
                )
            )
            for _ in range(100):
                approvals = db.approvals_for_task(worker.task.id)
                if approvals:
                    break
                await asyncio.sleep(0.01)
            approval = approvals[0]
            db.decide_approval(
                approval["id"],
                approve=False,
                decided_by="codex",
                request_hash=request_hash(worker.task.id, "Bash", input_data),
            )
            self.assertEqual({"decision": "decline"}, await waiting)

    async def test_user_input_maps_question_text_back_to_protocol_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)
            question = {
                "id": "database",
                "header": "Database",
                "question": "Which database?",
                "options": [
                    {"label": "SQLite", "description": "Local"},
                    {"label": "Postgres", "description": "Server"},
                ],
                "isSecret": False,
            }
            waiting = asyncio.create_task(
                worker._approval_result(
                    "item/tool/requestUserInput", {"questions": [question]}
                )
            )
            for _ in range(100):
                approvals = db.approvals_for_task(worker.task.id)
                if approvals:
                    break
                await asyncio.sleep(0.01)
            approval = approvals[0]
            input_data = {"questions": [question]}
            db.decide_approval(
                approval["id"],
                approve=True,
                decided_by="codex",
                request_hash=request_hash(worker.task.id, "AskUserQuestion", input_data),
                response={"answers": {"Which database?": "SQLite"}},
            )
            self.assertEqual(
                {"answers": {"database": {"answers": ["SQLite"]}}}, await waiting
            )

    def test_events_redact_account_and_token_fields(self) -> None:
        self.assertEqual(
            {"account": {"email": "[redacted]", "type": "chatgpt"}, "token": "[redacted]"},
            _redact({"account": {"email": "person@example.test", "type": "chatgpt"}, "token": "x"}),
        )
        usage = {"total": {"inputTokens": 10, "outputTokens": 2}}
        self.assertEqual(usage, _redact({"tokenUsage": usage})["tokenUsage"])

    def test_chatgpt_account_is_accepted(self) -> None:
        self.assertEqual(
            {"type": "chatgpt"},
            _subscription_auth_summary({"account": {"type": "chatgpt"}}),
        )

    def test_managed_authenticated_session_is_accepted(self) -> None:
        self.assertEqual(
            {"type": "managed"},
            _subscription_auth_summary(
                {"account": None, "requiresOpenaiAuth": False}
            ),
        )

    def test_null_account_requiring_auth_is_rejected(self) -> None:
        with self.assertRaises(CodexUnavailable):
            _subscription_auth_summary(
                {"account": None, "requiresOpenaiAuth": True}
            )

    def test_null_account_without_auth_requirement_is_rejected(self) -> None:
        with self.assertRaises(CodexUnavailable):
            _subscription_auth_summary({"account": None})

    def test_non_chatgpt_account_type_is_rejected(self) -> None:
        for account_type in ("apiKey", "enterprise", None):
            with self.subTest(account_type=account_type):
                with self.assertRaises(CodexUnavailable):
                    _subscription_auth_summary(
                        {"account": {"type": account_type}}
                    )

    def test_auth_event_summary_does_not_expose_account_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worker, db = self.make_worker(Path(temp))
            worker._verify_subscription_auth(
                {
                    "account": {
                        "type": "chatgpt",
                        "email": "person@example.test",
                        "accessToken": "access-secret",
                    },
                    "token": "top-level-secret",
                }
            )

            event = db.event_tail(worker.task.id, limit=1)[0]
            self.assertEqual("codex.auth_verified", event["kind"])
            self.assertEqual({"type": "chatgpt"}, event["payload"])
            encoded = str(event["payload"])
            self.assertNotIn("person@example.test", encoded)
            self.assertNotIn("access-secret", encoded)
            self.assertNotIn("top-level-secret", encoded)


if __name__ == "__main__":
    unittest.main()
