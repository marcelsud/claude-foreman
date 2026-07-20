from __future__ import annotations

import tempfile
import asyncio
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock

from foreman.codex_worker import (
    CodexAppServerWorker,
    CodexUnavailable,
    _file_change_approval_input,
    _redact,
    _subscription_auth_summary,
)
from foreman.config import ForemanConfig
from foreman.database import ForemanDB
from foreman.approval_policy import classify_risk, request_hash


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

    async def request_file_change(
        self,
        worker: CodexAppServerWorker,
        db: ForemanDB,
        *,
        params: dict,
        changes: list[dict],
    ) -> tuple[dict, dict]:
        item = {
            "type": "fileChange",
            "id": params["itemId"],
            "changes": changes,
            "status": "inProgress",
        }
        worker._handle_notification(
            {
                "method": "item/started",
                "params": {
                    "threadId": params["threadId"],
                    "turnId": params["turnId"],
                    "startedAtMs": params["startedAtMs"],
                    "item": item,
                },
            }
        )
        waiting = asyncio.create_task(
            worker._approval_result("item/fileChange/requestApproval", params)
        )
        approvals: list[dict] = []
        for _ in range(100):
            approvals = db.approvals_for_task(worker.task.id)
            if approvals:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(approvals)
        approval = approvals[0]
        db.decide_approval(
            approval["id"],
            approve=False,
            decided_by="test",
            message="validation rejection",
            request_hash=approval["request_hash"],
        )
        result = await waiting
        worker._handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turnId": params["turnId"],
                    "completedAtMs": params["startedAtMs"] + 1,
                    "item": {**item, "status": "declined"},
                },
            }
        )
        return approval, result

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

    async def test_file_change_approval_uses_correlated_item_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)
            diff = "@@ -1 +1,2 @@\n existing\n+new line\n"
            params = {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-edit-a",
                "startedAtMs": 1_784_551_773_913,
                "reason": None,
                "grantRoot": None,
            }

            approval, result = await self.request_file_change(
                worker,
                db,
                params=params,
                changes=[
                    {
                        "path": "README.md",
                        "kind": {"type": "update", "move_path": None},
                        "diff": diff,
                    }
                ],
            )

            expected_path = str((root / "README.md").resolve())
            self.assertEqual({"decision": "decline"}, result)
            self.assertEqual("Edit", approval["tool_name"])
            self.assertEqual([expected_path], approval["input"]["paths"])
            self.assertEqual(expected_path, approval["input"]["path"])
            self.assertEqual("exec-edit-a", approval["input"]["item_id"])
            self.assertIsNone(approval["input"]["grant_root"])
            self.assertIn("exec-edit-a", approval["input"]["reason"])
            self.assertIn(expected_path, approval["input"]["reason"])
            self.assertEqual(
                sha256(diff.encode()).hexdigest(),
                approval["input"]["changes"][0]["diff_sha256"],
            )
            self.assertEqual(
                request_hash(worker.task.id, "Edit", approval["input"]),
                approval["request_hash"],
            )

    async def test_distinct_file_changes_persist_distinct_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)
            common = {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "reason": None,
                "grantRoot": None,
            }
            approval_a, _ = await self.request_file_change(
                worker,
                db,
                params={**common, "itemId": "exec-edit-a", "startedAtMs": 100},
                changes=[
                    {
                        "path": "a.txt",
                        "kind": {"type": "update", "move_path": None},
                        "diff": "@@ -1 +1 @@\n-old a\n+new a\n",
                    }
                ],
            )
            approval_b, _ = await self.request_file_change(
                worker,
                db,
                params={**common, "itemId": "exec-edit-b", "startedAtMs": 200},
                changes=[
                    {
                        "path": "b.txt",
                        "kind": {"type": "update", "move_path": None},
                        "diff": "@@ -1 +1 @@\n-old b\n+new b\n",
                    }
                ],
            )

            self.assertNotEqual(approval_a["request_hash"], approval_b["request_hash"])
            self.assertNotEqual(
                approval_a["input"]["patch_sha256"],
                approval_b["input"]["patch_sha256"],
            )
            self.assertEqual([str((root / "a.txt").resolve())], approval_a["input"]["paths"])
            self.assertEqual([str((root / "b.txt").resolve())], approval_b["input"]["paths"])

    def test_different_diff_changes_patch_and_request_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            params = {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-edit-a",
                "startedAtMs": 100,
                "reason": "Update the fixture",
                "grantRoot": None,
            }
            base_item = {
                "type": "fileChange",
                "id": "exec-edit-a",
                "changes": [
                    {
                        "path": "fixture.txt",
                        "kind": {"type": "update", "move_path": None},
                        "diff": "first diff",
                    }
                ],
            }
            first = _file_change_approval_input(params, base_item, root)
            second = _file_change_approval_input(
                params,
                {
                    **base_item,
                    "changes": [{**base_item["changes"][0], "diff": "second diff"}],
                },
                root,
            )

            self.assertNotEqual(first["patch_sha256"], second["patch_sha256"])
            self.assertNotEqual(
                request_hash("task", "Edit", first),
                request_hash("task", "Edit", second),
            )

    def test_multi_file_change_binds_all_paths_and_move_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            params = {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-multi",
                "startedAtMs": 100,
                "reason": None,
                "grantRoot": None,
            }
            result = _file_change_approval_input(
                params,
                {
                    "type": "fileChange",
                    "id": "exec-multi",
                    "changes": [
                        {
                            "path": "one.txt",
                            "kind": {"type": "update", "move_path": None},
                            "diff": "one",
                        },
                        {
                            "path": "two.txt",
                            "kind": {"type": "move", "move_path": "three.txt"},
                            "diff": "two to three",
                        },
                    ],
                },
                root,
            )

            self.assertEqual(
                [
                    str((root / "one.txt").resolve()),
                    str((root / "two.txt").resolve()),
                    str((root / "three.txt").resolve()),
                ],
                result["paths"],
            )
            self.assertNotIn("path", result)
            self.assertIn(str((root / "three.txt").resolve()), result["reason"])

    def test_grant_root_remains_separate_and_high_risk_outside_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            params = {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-grant",
                "startedAtMs": 100,
                "reason": "Request broader write access",
                "grantRoot": str(root.parent),
            }
            result = _file_change_approval_input(
                params,
                {
                    "type": "fileChange",
                    "id": "exec-grant",
                    "changes": [
                        {
                            "path": "inside.txt",
                            "kind": {"type": "update", "move_path": None},
                            "diff": "inside",
                        }
                    ],
                },
                root,
            )

            self.assertEqual(str(root.parent.resolve()), result["grant_root"])
            self.assertEqual(str((root / "inside.txt").resolve()), result["path"])
            self.assertEqual("high", classify_risk("Edit", result, root))

    async def test_uncorrelated_file_change_fails_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)

            with self.assertRaisesRegex(RuntimeError, "cannot be correlated"):
                await worker._approval_result(
                    "item/fileChange/requestApproval",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "missing-item",
                        "startedAtMs": 100,
                        "reason": None,
                        "grantRoot": None,
                    },
                )

            self.assertEqual([], db.approvals_for_task(worker.task.id, None))

    async def test_uncorrelated_server_request_returns_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, db = self.make_worker(root)
            worker._send = AsyncMock()

            await worker._handle_server_request(
                {
                    "id": 0,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "missing-item",
                        "startedAtMs": 100,
                        "reason": None,
                        "grantRoot": None,
                    },
                }
            )

            worker._send.assert_awaited_once_with(
                {
                    "id": 0,
                    "error": {
                        "code": -32000,
                        "message": (
                            "Codex file-change approval cannot be correlated with its "
                            "fileChange item"
                        ),
                    },
                }
            )
            self.assertEqual([], db.approvals_for_task(worker.task.id, None))
            event = db.event_tail(worker.task.id, limit=1)[0]
            self.assertEqual("codex.protocol_error", event["kind"])

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
