from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path

from claude_foreman.config import ForemanConfig
from claude_foreman.daemon import ForemanDaemon


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


if __name__ == "__main__":
    unittest.main()
