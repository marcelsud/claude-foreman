from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from foreman.cli import build_parser
from foreman.dashboard import TaskMonitor, run_monitor
from foreman.database import ForemanDB
from foreman.models import TaskStatus


class FakeController:
    def status(self) -> dict[str, object]:
        return {"running": True, "pid": 4242}


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = ForemanDB(Path(self.temp.name) / "state.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_parser_exposes_monitor_options(self) -> None:
        args = build_parser().parse_args(
            ["monitor", "--status", "running", "--interval", "0.5", "--once"]
        )
        self.assertEqual("monitor", args.command)
        self.assertEqual("running", args.status)
        self.assertEqual(0.5, args.interval)
        self.assertTrue(args.once)

    def test_dashboard_renders_operational_task_details(self) -> None:
        task = self.db.create_task(
            repo_path=self.temp.name,
            prompt="build dashboard",
            provider="codex",
            model="gpt-5.6-sol",
            effort="high",
            verification_commands=["git status"],
        )
        self.db.update_task(
            task.id,
            status=TaskStatus.RUNNING,
            branch_name="foreman/dashboard",
            worktree_path=str(Path(self.temp.name) / "worktree"),
        )
        run_id = self.db.create_run(task.id)
        self.db.upsert_run_usage(
            run_id=run_id,
            task_id=task.id,
            provider="codex",
            model="gpt-5.6-sol",
            input_tokens=1200,
            cache_read_input_tokens=3000,
            output_tokens=450,
            total_tokens=4650,
        )
        self.db.add_event(
            task.id,
            run_id,
            "task.progress",
            {"summary": "Implementing HTTP handlers\x1b[2J"},
        )
        monitor = TaskMonitor(self.db, FakeController())

        monitor.refresh()
        rendered = monitor.render(140, 32, color=False)

        self.assertIn("Foreman", rendered)
        self.assertIn("daemon running (pid 4242)", rendered)
        self.assertIn("gpt-5.6-sol", rendered)
        self.assertIn("Implementing HTTP handlers", rendered)
        self.assertIn("4.7k tokens", rendered)
        self.assertIn("foreman/dashboard", rendered)
        self.assertIn("0/1 …", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_once_mode_is_a_noninteractive_snapshot(self) -> None:
        self.db.create_task(repo_path=self.temp.name, prompt="queued work")
        output = io.StringIO()

        run_monitor(
            self.db,
            FakeController(),
            once=True,
            color=False,
            output_stream=output,
        )

        rendered = output.getvalue()
        self.assertIn("queued", rendered)
        self.assertIn("sonnet", rendered)
        self.assertIn("q quit", rendered)


if __name__ == "__main__":
    unittest.main()
