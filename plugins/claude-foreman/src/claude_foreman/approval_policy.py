from __future__ import annotations

import json
import shlex
from hashlib import sha256
from pathlib import Path
from typing import Any


HIGH_RISK_COMMAND_MARKERS = (
    "rm ", "rmdir ", "git push", "git merge", "git rebase", "git reset",
    "git clean", "git commit", "git checkout", "git switch", "gh pr merge",
    "kubectl", "terraform apply", "deploy", "sudo ", "chmod ", "chown ", "docker ",
)

SENSITIVE_MARKERS = (
    "/.ssh/",
    "~/.ssh",
    "/.aws/",
    "~/.aws",
    "/.claude/",
    "~/.claude",
    "/.codex/",
    "~/.codex",
    "credentials",
    "auth.json",
    "id_rsa",
    "id_ed25519",
    "private_key",
    "anthropic_api_key",
    "anthropic_auth_token",
    "openai_api_key",
    "codex_api_key",
    "codex_access_token",
)


def sensitive_reference(value: str) -> bool:
    lowered = value.lower().replace("\\", "/")
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def routine_sandboxed_command(command: str) -> bool:
    if not command.strip() or any(character in command for character in ";&|><`$()\n\r"):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = Path(tokens[0]).name
    if executable in {"pwd", "ls", "rg", "pytest", "ruff", "mypy", "eslint", "tsc"}:
        return True
    if executable == "git" and len(tokens) >= 2:
        return tokens[1] in {"status", "diff", "log", "show", "grep", "rev-parse", "ls-files"}
    if executable in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m":
        return tokens[2] in {"pytest", "unittest", "compileall"}
    if executable in {"npm", "pnpm", "yarn", "bun"} and len(tokens) >= 2:
        if tokens[1] in {"test", "lint", "typecheck"}:
            return True
        return len(tokens) >= 3 and tokens[1] == "run" and tokens[2] in {
            "test", "lint", "build", "typecheck", "check"
        }
    if executable == "cargo" and len(tokens) >= 2:
        return tokens[1] in {"test", "check", "fmt", "clippy"}
    if executable == "go" and len(tokens) >= 2:
        return tokens[1] == "test"
    if executable == "make" and len(tokens) >= 2:
        return tokens[1] in {"test", "check", "lint", "build"}
    return False


def request_hash(task_id: str, tool_name: str, input_data: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"task_id": task_id, "tool_name": tool_name, "input": input_data},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode()).hexdigest()


def classify_risk(tool_name: str, input_data: dict[str, Any], worktree: str | Path) -> str:
    if tool_name == "AskUserQuestion":
        return "needs_input"
    if tool_name in {"WebFetch", "WebSearch"} or tool_name.startswith("mcp__"):
        return "external"
    if tool_name == "Bash":
        command = str(input_data.get("command", "")).lower()
        if input_data.get("dangerouslyDisableSandbox"):
            return "critical"
        if any(marker in command for marker in HIGH_RISK_COMMAND_MARKERS) or sensitive_reference(command):
            return "high"
        return "medium"
    path_value = input_data.get("file_path") or input_data.get("path")
    if path_value:
        try:
            root = Path(worktree).resolve()
            path = Path(path_value).expanduser()
            path = (root / path).resolve() if not path.is_absolute() else path.resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            return "high"
    return "medium"


def auto_allow(tool_name: str, input_data: dict[str, Any], worktree: str | Path, risk: str) -> bool:
    """Allow routine sandboxed work without creating manager busywork."""
    if tool_name == "Bash":
        return risk == "medium" and routine_sandboxed_command(str(input_data.get("command", "")))
    if tool_name in {"Read", "Glob", "Grep"}:
        return risk == "medium"
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return risk == "medium"
    return False


def human_only(risk: str, tool_name: str, input_data: dict[str, Any]) -> bool:
    if risk == "critical":
        return True
    if tool_name == "Bash":
        command = str(input_data.get("command", "")).lower()
        return sensitive_reference(command) or any(
            marker in command
            for marker in ("git push --force", "git push -f", "gh pr merge", "deploy", "terraform apply")
        )
    if tool_name == "AskUserQuestion":
        return any(bool(question.get("isSecret")) for question in input_data.get("questions", []))
    path_value = input_data.get("file_path") or input_data.get("path")
    return bool(path_value and sensitive_reference(str(path_value)))
