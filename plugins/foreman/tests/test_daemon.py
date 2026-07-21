from __future__ import annotations

import asyncio
import fcntl
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from foreman.config import ForemanConfig
from foreman.daemon import ForemanDaemon
from foreman.database import ForemanDB


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    def config(self, root: Path) -> ForemanConfig:
        return ForemanConfig(
            data_dir=root,
            db_path=root / "state.db",
            worktrees_dir=root / "worktrees",
            logs_dir=root / "logs",
            pid_path=root / "foremand.pid",
            poll_interval=0.01,
        )

    async def test_clean_start_and_stop_removes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            daemon = ForemanDaemon(config)
            daemon.stop_event.set()

            await daemon.serve()

            self.assertFalse(config.pid_path.exists())
            self.assertTrue((config.data_dir / "foremand.lock").exists())

    async def test_second_daemon_is_rejected_by_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            config.ensure_directories()
            lock_path = config.data_dir / "foremand.lock"
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                daemon = ForemanDaemon(config)
                with self.assertRaisesRegex(RuntimeError, "another Foreman daemon"):
                    await daemon.serve()

    async def test_new_task_wakes_scheduler_before_recovery_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = replace(self.config(Path(temp)), poll_interval=5.0)
            daemon = ForemanDaemon(config)
            started = asyncio.Event()

            class Worker:
                async def run(inner_self, _task) -> None:
                    started.set()
                    daemon.stop_event.set()

            daemon.worker = Worker()  # type: ignore[assignment]
            serving = asyncio.create_task(daemon.serve())
            writer = ForemanDB(config.db_path, data_dir=config.data_dir)
            try:
                deadline = asyncio.get_running_loop().time() + 1
                while not config.pid_path.exists():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("daemon did not start")
                    await asyncio.sleep(0.01)

                writer.wake.close()
                writer.wake = daemon.db.wake
                writer.create_task(repo_path=temp, prompt="wake the scheduler")

                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.wait_for(serving, timeout=1)
            finally:
                daemon.stop_event.set()
                daemon.db.wake.publish(channel="queue")
                if not serving.done():
                    await serving


if __name__ == "__main__":
    unittest.main()
