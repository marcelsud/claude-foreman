from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    ForemanConfig,
    active_non_subscription_credentials,
    missing_sandbox_dependencies,
    subscription_environment,
)
from .database import ForemanDB


def run_doctor(config: ForemanConfig) -> dict[str, Any]:
    config.ensure_directories()
    db = ForemanDB(config.db_path)
    db.initialize()
    credentials = Path.home() / ".claude" / ".credentials.json"
    sdk_spec = importlib.util.find_spec("claude_agent_sdk")
    sdk_installed = sdk_spec is not None
    bundled_cli = None
    if sdk_spec and sdk_spec.origin:
        candidate = Path(sdk_spec.origin).resolve().parent / "_bundled" / "claude"
        if candidate.is_file():
            bundled_cli = str(candidate)
    claude_cli = shutil.which("claude") or bundled_cli
    auth_status: dict[str, Any] | None = None
    if claude_cli:
        try:
            result = subprocess.run(
                [claude_cli, "auth", "status"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=subscription_environment(),
                check=False,
                timeout=20,
            )
            try:
                auth_status = json.loads(result.stdout) if result.stdout.strip() else None
            except json.JSONDecodeError:
                auth_status = {
                    "loggedIn": False,
                    "returncode": result.returncode,
                    "output": result.stdout.strip(),
                }
        except (OSError, subprocess.TimeoutExpired) as exc:
            auth_status = {"loggedIn": False, "error": f"{type(exc).__name__}: {exc}"}
    logged_in = bool(auth_status and auth_status.get("loggedIn"))
    sandbox_missing = missing_sandbox_dependencies()
    checks = {
        "database": {"ok": config.db_path.exists(), "path": str(config.db_path)},
        "git": {"ok": shutil.which("git") is not None, "path": shutil.which("git")},
        "claude_agent_sdk": {"ok": sdk_installed},
        "subscription_credentials": {
            "ok": logged_in,
            "credential_file_present": credentials.exists(),
            "claude_cli": claude_cli,
            "auth_status": auth_status,
        },
        "api_or_provider_variables": {
            "ok": not active_non_subscription_credentials(),
            "present": active_non_subscription_credentials(),
            "note": "Foreman removes these variables from daemon and worker processes.",
        },
        "platform": {
            "ok": not sandbox_missing,
            "sandbox_missing": sandbox_missing,
            "sandbox_note": "Foreman fails closed when Claude Bash sandbox dependencies are unavailable.",
        },
    }
    return {
        "ok": all(item["ok"] for key, item in checks.items() if key != "api_or_provider_variables"),
        "auth_mode": "subscription",
        "checks": checks,
    }
