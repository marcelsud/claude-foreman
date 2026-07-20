from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


SUBSCRIPTION_AUTH_BLOCKLIST = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "ANTHROPIC_AWS_WORKSPACE_ID",
}


@dataclass(frozen=True, slots=True)
class ForemanConfig:
    data_dir: Path
    db_path: Path
    worktrees_dir: Path
    logs_dir: Path
    pid_path: Path
    poll_interval: float = 2.0
    max_workers: int = 1
    approval_timeout_seconds: int = 86_400
    python_executable: str = sys.executable

    @classmethod
    def from_env(cls) -> "ForemanConfig":
        data_dir = Path(
            os.environ.get(
                "FOREMAN_DATA_DIR",
                Path.home() / ".local" / "share" / "claude-foreman",
            )
        ).expanduser().resolve()
        db_path = Path(os.environ.get("FOREMAN_DB_PATH", data_dir / "state.db")).expanduser().resolve()
        worktrees_dir = Path(
            os.environ.get("FOREMAN_WORKTREES_DIR", data_dir / "worktrees")
        ).expanduser().resolve()
        logs_dir = Path(os.environ.get("FOREMAN_LOGS_DIR", data_dir / "logs")).expanduser().resolve()
        pid_path = Path(
            os.environ.get("FOREMAN_PID_PATH", data_dir / "foremand.pid")
        ).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            db_path=db_path,
            worktrees_dir=worktrees_dir,
            logs_dir=logs_dir,
            pid_path=pid_path,
            poll_interval=max(0.1, float(os.environ.get("FOREMAN_POLL_INTERVAL", "2"))),
            max_workers=max(1, int(os.environ.get("FOREMAN_MAX_WORKERS", "1"))),
            approval_timeout_seconds=max(
                30, int(os.environ.get("FOREMAN_APPROVAL_TIMEOUT_SECONDS", "86400"))
            ),
            python_executable=os.environ.get("FOREMAN_PYTHON", sys.executable),
        )

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.worktrees_dir, self.logs_dir):
            existed = path.exists()
            path.mkdir(parents=True, exist_ok=True)
            if not existed and os.name == "posix":
                path.chmod(0o700)


def subscription_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot silently switch to API/provider billing."""
    env = dict(os.environ)
    for key in SUBSCRIPTION_AUTH_BLOCKLIST:
        env.pop(key, None)
    if extra:
        env.update(extra)
    env["CLAUDE_FOREMAN_AUTH_MODE"] = "subscription"
    # Windows commonly exports an AppData temp path into WSL. The outer Codex
    # sandbox cannot write there, while /tmp is an approved, private runtime area.
    env.update({"TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"})
    return env


def active_non_subscription_credentials() -> list[str]:
    return sorted(key for key in SUBSCRIPTION_AUTH_BLOCKLIST if os.environ.get(key))


def enforce_subscription_environment() -> list[str]:
    """Remove non-subscription auth from this process before any worker is spawned."""
    removed = active_non_subscription_credentials()
    for key in SUBSCRIPTION_AUTH_BLOCKLIST:
        os.environ.pop(key, None)
    os.environ["CLAUDE_FOREMAN_AUTH_MODE"] = "subscription"
    os.environ.update({"TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"})
    return removed


def missing_sandbox_dependencies() -> list[str]:
    if not sys.platform.startswith("linux"):
        return []
    return [command for command in ("bwrap", "socat") if shutil.which(command) is None]
