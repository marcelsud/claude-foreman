from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import signal
from pathlib import Path

from .config import ForemanConfig, enforce_subscription_environment
from .database import ForemanDB
from .runner import ClaudeWorker


class ForemanDaemon:
    def __init__(self, config: ForemanConfig):
        self.config = config
        self.db = ForemanDB(config.db_path, data_dir=config.data_dir)
        self.worker = ClaudeWorker(config, self.db)
        self.stop_event = asyncio.Event()
        self.running: set[asyncio.Task[None]] = set()

    async def serve(self) -> None:
        self.config.ensure_directories()
        lock_path = self.config.data_dir / "foremand.lock"
        lock_handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.close()
            raise RuntimeError(f"another Foreman daemon holds {lock_path}") from exc
        self.db.initialize()
        recovered = self.db.recover_interrupted_tasks()
        removed = enforce_subscription_environment()
        self.config.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        if recovered:
            self.db.add_event(None, None, "daemon.tasks_recovered", {"task_ids": recovered})
        if removed:
            self.db.add_event(None, None, "daemon.auth_environment_scrubbed", {"variables": removed})
        queue_generation = self.db.wake.subscribe(channel="queue")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        try:
            while not self.stop_event.is_set():
                self.running = {item for item in self.running if not item.done()}
                while len(self.running) < self.config.max_workers:
                    task = self.db.claim_next_task()
                    if task is None:
                        break
                    future = asyncio.create_task(self.worker.run(task), name=f"foreman-{task.id}")
                    future.add_done_callback(
                        lambda _: self.db.wake.publish(channel="queue")
                    )
                    self.running.add(future)
                queue_generation = await asyncio.to_thread(
                    self.db.wake.wait,
                    queue_generation,
                    self.config.poll_interval,
                    channel="queue",
                )
            if self.running:
                for worker in self.running:
                    worker.cancel()
                await asyncio.gather(*self.running, return_exceptions=True)
        finally:
            try:
                if self.config.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    self.config.pid_path.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
            self.db.wake.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Foreman scheduler")
    parser.add_argument("command", nargs="?", default="run", choices=["run"])
    parser.parse_args(argv)
    asyncio.run(ForemanDaemon(ForemanConfig.from_env()).serve())


if __name__ == "__main__":
    main()
