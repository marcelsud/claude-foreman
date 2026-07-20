from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import ForemanConfig, subscription_environment


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_foreman_process(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 and any(
        module in result.stdout for module in ("foreman.daemon", "claude_foreman.daemon")
    )


class DaemonController:
    def __init__(self, config: ForemanConfig):
        self.config = config

    def status(self) -> dict[str, Any]:
        try:
            pid = int(self.config.pid_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return {"running": False, "pid": None, "pid_path": str(self.config.pid_path)}
        running = _is_foreman_process(pid)
        return {"running": running, "pid": pid, "pid_path": str(self.config.pid_path)}

    def start(self) -> dict[str, Any]:
        self.config.ensure_directories()
        current = self.status()
        if current["running"]:
            return current
        from .doctor import run_doctor

        readiness = run_doctor(self.config)
        if not readiness["ok"]:
            failed_checks = [
                name for name, check in readiness["checks"].items() if not check.get("ok", False)
            ]
            return {
                "running": False,
                "pid": None,
                "error": "Foreman readiness checks failed",
                "failed_checks": failed_checks,
                "doctor": readiness,
            }
        log_path = self.config.logs_dir / "foremand.log"
        log_handle = log_path.open("ab", buffering=0)
        if os.name == "posix":
            log_path.chmod(0o600)
        process = subprocess.Popen(
            [self.config.python_executable, "-m", "foreman.daemon", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=subscription_environment(),
            start_new_session=True,
            close_fds=True,
        )
        log_handle.close()
        for _ in range(30):
            time.sleep(0.1)
            current = self.status()
            if current["running"]:
                current["log_path"] = str(log_path)
                return current
            if process.poll() is not None:
                break
        return {
            "running": False,
            "pid": process.pid,
            "log_path": str(log_path),
            "error": "daemon did not become ready; inspect the log",
        }

    def stop(self) -> dict[str, Any]:
        current = self.status()
        if not current["running"]:
            return current
        os.kill(int(current["pid"]), signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            if not _pid_alive(int(current["pid"])):
                break
        return self.status()
