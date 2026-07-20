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
    codex_subscription_command,
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
    codex_cli = shutil.which("codex")
    codex_auth: dict[str, Any] = {"loggedIn": False}
    if codex_cli:
        try:
            result = subprocess.run(
                codex_subscription_command(codex_cli, "login", "status"),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=subscription_environment(),
                check=False,
                timeout=20,
            )
            output = (result.stdout + result.stderr).strip()
            codex_auth = {
                "loggedIn": result.returncode == 0 and "chatgpt" in output.lower(),
                "mode": "chatgpt" if "chatgpt" in output.lower() else "unknown",
                "returncode": result.returncode,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            codex_auth = {"loggedIn": False, "error": f"{type(exc).__name__}: {exc}"}
    claude_ready = bool(sdk_installed and claude_cli and logged_in and not sandbox_missing)
    codex_ready = bool(codex_cli and codex_auth["loggedIn"])
    checks = {
        "database": {"ok": config.db_path.exists(), "path": str(config.db_path)},
        "git": {"ok": shutil.which("git") is not None, "path": shutil.which("git")},
        "providers": {
            "ok": claude_ready or codex_ready,
            "claude": {
                "ok": claude_ready,
                "agent_sdk": sdk_installed,
                "cli": claude_cli,
                "subscription_logged_in": logged_in,
                "credential_file_present": credentials.exists(),
                "sandbox_missing": sandbox_missing,
            },
            "codex": {
                "ok": codex_ready,
                "cli": codex_cli,
                "subscription_logged_in": codex_auth["loggedIn"],
                "auth_mode": codex_auth.get("mode"),
                "model_provider": "openai",
                "model_provider_source": "foreman_process_override",
                "forced_login_method": "chatgpt",
            },
        },
        "api_or_provider_variables": {
            "ok": not active_non_subscription_credentials(),
            "present": active_non_subscription_credentials(),
            "note": "Foreman removes these variables from daemon and worker processes.",
        },
    }
    return {
        "ok": all(item["ok"] for key, item in checks.items() if key != "api_or_provider_variables"),
        "auth_mode": "subscription",
        "checks": checks,
    }
