from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from foreman.database import ForemanDB
from foreman.models import TaskStatus
from foreman.wake import LocalWake


class TaskWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.db = ForemanDB(self.data_dir / "state.db", data_dir=self.data_dir)
        self.db.initialize()
        self.task = self.db.create_task(repo_path=self.temp.name, prompt="wait")

    def tearDown(self) -> None:
        self.db.wake.close()
        self.temp.cleanup()

    def latest_event_id(self) -> int:
        return self.db.event_tail(self.task.id, 1)[0]["id"]

    def test_immediate_cursor_catch_up(self) -> None:
        cursor = self.latest_event_id()
        event_id = self.db.add_event(
            self.task.id, None, "approval.requested", {"approval_id": "approval-1"}
        )

        result = self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=2)

        self.assertEqual("approval_required", result["reason"])
        self.assertEqual(event_id, result["cursor"])
        self.assertEqual([event_id], [event["id"] for event in result["events"]])

    def test_wait_wakes_across_database_instances(self) -> None:
        cursor = self.latest_event_id()
        result: dict = {}

        def wait() -> None:
            result.update(
                self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=2)
            )

        waiter = threading.Thread(target=wait)
        waiter.start()
        deadline = time.monotonic() + 1
        while not self.db.wake.ipc_available and time.monotonic() < deadline:
            time.sleep(0.01)

        writer = ForemanDB(self.data_dir / "state.db", data_dir=self.data_dir)
        started = time.monotonic()
        event_id = writer.add_event(
            self.task.id, None, "run.awaiting_review", {"summary": "done"}
        )
        waiter.join(2)
        writer.wake.close()

        self.assertFalse(waiter.is_alive())
        self.assertEqual("review_required", result["reason"])
        self.assertEqual(event_id, result["cursor"])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_timeout_advances_past_filtered_events(self) -> None:
        cursor = self.latest_event_id()
        progress_id = self.db.add_event(self.task.id, None, "worker.progress", {"step": 1})

        result = self.db.wait_for_task(
            self.task.id,
            after_id=cursor,
            timeout_seconds=0.05,
            actionable_only=True,
        )

        self.assertEqual("timeout", result["reason"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(progress_id, result["cursor"])
        self.assertEqual(1, result["filtered_events"])

    def test_non_actionable_mode_returns_any_durable_events(self) -> None:
        cursor = self.latest_event_id()
        progress_id = self.db.add_event(self.task.id, None, "worker.progress", {"step": 1})

        result = self.db.wait_for_task(
            self.task.id,
            after_id=cursor,
            timeout_seconds=0,
            actionable_only=False,
        )

        self.assertEqual("events_available", result["reason"])
        self.assertEqual(progress_id, result["cursor"])

    def test_failed_verification_is_actionable(self) -> None:
        cursor = self.latest_event_id()
        self.db.add_event(
            self.task.id,
            None,
            "verification.gate_completed",
            {"status": "failed", "gate_id": "gate-1"},
        )

        result = self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=0)

        self.assertEqual("verification_failed", result["reason"])

    def test_one_action_is_returned_without_skipping_the_next(self) -> None:
        cursor = self.latest_event_id()
        approval_id = self.db.add_event(
            self.task.id, None, "approval.requested", {"approval_id": "approval-1"}
        )
        review_id = self.db.add_event(
            self.task.id, None, "run.awaiting_review", {"summary": "done"}
        )

        first = self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=0)
        second = self.db.wait_for_task(
            self.task.id, after_id=first["cursor"], timeout_seconds=0
        )

        self.assertEqual("approval_required", first["reason"])
        self.assertEqual(approval_id, first["cursor"])
        self.assertEqual("review_required", second["reason"])
        self.assertEqual(review_id, second["cursor"])

    def test_current_review_state_survives_cursor_reconnect(self) -> None:
        self.db.update_task(self.task.id, status=TaskStatus.AWAITING_REVIEW)
        cursor = self.latest_event_id()

        result = self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=2)

        self.assertEqual("review_required", result["reason"])
        self.assertEqual([], result["events"])

    def test_ipc_unavailable_uses_bounded_recovery_read(self) -> None:
        self.db.wake.close()

        def unavailable_socket(*_args, **_kwargs):
            raise OSError("Unix sockets unavailable")

        self.db.wake = LocalWake(
            self.data_dir,
            recovery_interval=0.05,
            socket_factory=unavailable_socket,
        )
        cursor = self.latest_event_id()
        result: dict = {}

        waiter = threading.Thread(
            target=lambda: result.update(
                self.db.wait_for_task(self.task.id, after_id=cursor, timeout_seconds=1)
            )
        )
        waiter.start()
        time.sleep(0.1)
        writer = ForemanDB(self.data_dir / "state.db", data_dir=self.data_dir)
        writer.add_event(self.task.id, None, "run.failed", {"error": "boom"})
        waiter.join(1)
        writer.wake.close()

        self.assertFalse(waiter.is_alive())
        self.assertEqual("failed", result["reason"])
        self.assertFalse(result["ipc_available"])


if __name__ == "__main__":
    unittest.main()
