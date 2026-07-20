from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from .approval_policy import auto_allow, classify_risk, human_only, request_hash
from .config import ForemanConfig, enforce_subscription_environment, missing_sandbox_dependencies
from .database import ForemanDB, utcnow
from .models import ApprovalStatus, Task, TaskStatus
from .worktrees import WorktreeManager
from .verification import run_verification_gates
from .usage import record_claude_result_usage


WORKER_POLICY = """
You are an implementation worker managed by Codex through Foreman.
Work only on the assigned task inside the current Git worktree.
Do not commit, push, merge, deploy, change credentials, disable the sandbox, or edit Foreman policy.
Do not read credential files, authentication tokens, shell profiles, browser data, or unrelated repositories.
Run relevant tests and report concise progress. Ask for input when requirements are ambiguous.
When finished, summarize files changed, verification performed, and remaining risks. Leave all changes uncommitted for Codex review.
""".strip()


class WorkerUnavailable(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class ClaudeWorker:
    def __init__(self, config: ForemanConfig, db: ForemanDB):
        self.config = config
        self.db = db
        self.worktrees = WorktreeManager(config)

    async def run(self, task: Task) -> None:
        run_id = self.db.create_run(task.id)
        self.db.add_event(
            task.id, run_id, "run.started",
            {"provider": task.provider, "model": task.model, "effort": task.effort},
        )
        try:
            missing = missing_sandbox_dependencies() if task.provider == "claude" else []
            if missing:
                raise WorkerUnavailable(
                    "refusing to run without the Claude Bash sandbox; missing: " + ", ".join(missing)
                )
            shared = self.db.workspace_for_group(task.workspace_group) if task.workspace_group else None
            if shared:
                worktree = self.worktrees.reuse(task, shared[0], shared[1])
            else:
                worktree = self.worktrees.ensure(task)
            task = self.db.update_task(
                task.id,
                status=TaskStatus.RUNNING,
                branch_name=worktree.branch,
                worktree_path=str(worktree.path),
                claude_session_id=None,
                worker_session_id=None,
                error=None,
                completed_at=None,
            )
            self.db.add_event(
                task.id,
                run_id,
                "worktree.ready",
                {"path": str(worktree.path), "branch": worktree.branch, "repo": str(worktree.repo_root)},
            )
            query_task = asyncio.create_task(self._query(task, run_id, worktree.path))
            while not query_task.done():
                await asyncio.sleep(self.config.poll_interval)
                if self.db.get_task(task.id).cancel_requested:
                    query_task.cancel()
                    await asyncio.gather(query_task, return_exceptions=True)
                    raise asyncio.CancelledError
            result = await query_task
            self.db.update_task(task.id, status=TaskStatus.VERIFYING)
            verification = await run_verification_gates(
                self.config, self.db, task, run_id, worktree.path
            )
            snapshot = self.worktrees.snapshot(worktree.path)
            self.db.add_event(task.id, run_id, "worktree.snapshot", snapshot)
            self.db.update_task(
                task.id,
                status=TaskStatus.AWAITING_REVIEW,
                result_summary=(
                    result or f"{task.provider.title()} finished without a textual summary."
                ) + (
                    (
                        "\n\nForeman verification gates: passed."
                        if verification["required_ok"]
                        else "\n\nForeman verification gates require attention."
                    )
                    if verification["gates"] else ""
                ),
            )
            self.db.close_pending_approvals(run_id, "run finished")
            self.db.finish_run(run_id, "awaiting_review", 0, None)
            self.db.add_event(task.id, run_id, "run.awaiting_review", {"summary": result})
        except asyncio.CancelledError:
            current = self.db.get_task(task.id)
            if current.cancel_requested:
                self.db.update_task(
                    task.id,
                    status=TaskStatus.CANCELLED,
                    completed_at=utcnow(),
                    error="cancelled",
                )
                run_status = "cancelled"
                message = "task was cancelled"
            else:
                self.db.update_task(
                    task.id,
                    status=TaskStatus.QUEUED,
                    completed_at=None,
                    error="interrupted by daemon shutdown",
                )
                run_status = "interrupted"
                message = "daemon stopped; task requeued"
            self.db.close_pending_approvals(run_id, message)
            self.db.finish_run(run_id, run_status, None, message)
            self.db.add_event(task.id, run_id, f"run.{run_status}", {"message": message})
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            cancelled = bool(self.db.get_task(task.id).cancel_requested)
            status = TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED
            run_status = "cancelled" if cancelled else "failed"
            self.db.update_task(task.id, status=status, completed_at=utcnow(), error=message)
            self.db.close_pending_approvals(run_id, f"run {run_status}")
            self.db.finish_run(run_id, run_status, None if cancelled else 1, message)
            self.db.add_event(task.id, run_id, f"run.{run_status}", {"error": message})

    async def _query(self, task: Task, run_id: str, worktree: Path) -> str | None:
        if task.provider == "codex":
            from .codex_worker import CodexAppServerWorker

            return await CodexAppServerWorker(
                self.config, self.db, WORKER_POLICY
            ).query(task, run_id, worktree)
        return await self._query_claude(task, run_id, worktree)

    async def _query_claude(self, task: Task, run_id: str, worktree: Path) -> str | None:
        enforce_subscription_environment()
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
            from claude_agent_sdk.types import (
                HookMatcher,
                PermissionResultAllow,
                PermissionResultDeny,
            )
        except ImportError as exc:
            raise WorkerUnavailable(
                "claude-agent-sdk is not installed; install the project into a virtual environment"
            ) from exc

        async def permission_callback(tool_name: str, input_data: dict[str, Any], _context: Any):
            risk = classify_risk(tool_name, input_data, worktree)
            if auto_allow(tool_name, input_data, worktree, risk):
                self.db.add_event(
                    task.id,
                    run_id,
                    "approval.auto_allowed",
                    {"tool_name": tool_name, "risk": risk},
                )
                return PermissionResultAllow(updated_input=input_data)
            digest = request_hash(task.id, tool_name, input_data)
            approval = self.db.create_approval(
                task_id=task.id,
                run_id=run_id,
                tool_name=tool_name,
                input_data=input_data,
                request_hash=digest,
                risk=risk,
                timeout_seconds=self.config.approval_timeout_seconds,
            )
            if human_only(risk, tool_name, input_data):
                self.db.add_event(
                    task.id,
                    run_id,
                    "approval.human_required",
                    {"approval_id": approval["id"], "risk": risk},
                )
            while True:
                await asyncio.sleep(self.config.poll_interval)
                current_task = self.db.get_task(task.id)
                if current_task.cancel_requested:
                    return PermissionResultDeny(message="Task was cancelled by its manager")
                current = self.db.get_approval(approval["id"])
                if current["status"] == ApprovalStatus.APPROVED:
                    self.db.update_task(task.id, status=TaskStatus.RUNNING)
                    updated_input = input_data
                    if tool_name == "AskUserQuestion":
                        response = current.get("response") or {}
                        updated_input = {
                            "questions": input_data.get("questions", []),
                            "answers": response.get("answers", {}),
                        }
                    return PermissionResultAllow(updated_input=updated_input)
                if current["status"] in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
                    self.db.update_task(task.id, status=TaskStatus.RUNNING)
                    return PermissionResultDeny(
                        message=current.get("decision_message") or f"Manager {current['status']} this action"
                    )

        async def pre_tool_gate(input_data: Any, _tool_use_id: str | None, _context: Any):
            tool_name = str(input_data.get("tool_name", ""))
            tool_input = input_data.get("tool_input") or {}
            risk = classify_risk(tool_name, tool_input, worktree)
            decision = "allow" if auto_allow(tool_name, tool_input, worktree, risk) else "ask"
            if decision == "allow":
                self.db.add_event(
                    task.id,
                    run_id,
                    "approval.auto_allowed",
                    {"tool_name": tool_name, "risk": risk, "via": "pre_tool_hook"},
                )
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": (
                        "Foreman policy auto-allows this scoped action"
                        if decision == "allow"
                        else "Foreman manager approval is required"
                    ),
                }
            }

        async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
            feedback = self.db.latest_review_feedback(task.id)
            prompt = task.prompt
            if feedback:
                prompt += f"\n\nCodex review feedback from the previous attempt:\n{feedback}"
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }

        stderr_lines: list[str] = []
        sandbox_failures: list[str] = []

        def stderr(line: str) -> None:
            stderr_lines.append(line)
            if len(stderr_lines) > 50:
                stderr_lines.pop(0)
            lowered = line.lower()
            if "sandbox disabled" in lowered or "without sandboxing" in lowered:
                sandbox_failures.append(line.strip())
            self.db.add_event(task.id, run_id, "claude.stderr", {"text": line[-8000:]})

        options = ClaudeAgentOptions(
            tools=[
                "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "Bash",
                "AskUserQuestion", "WebFetch", "WebSearch",
            ],
            system_prompt={"type": "preset", "preset": "claude_code", "append": WORKER_POLICY},
            cwd=worktree,
            model=task.model,
            effort=task.effort,
            max_turns=task.max_turns,
            permission_mode="default",
            can_use_tool=permission_callback,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_gate])]},
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
                "excludedCommands": [],
                "network": {
                    "allowedDomains": [],
                    "deniedDomains": ["*"],
                    "allowUnixSockets": [],
                    "allowAllUnixSockets": False,
                    "allowLocalBinding": False,
                },
            },
            setting_sources=["project"],
            env={
                "CLAUDE_FOREMAN_AUTH_MODE": "subscription",
                "CLAUDE_CODE_MAX_RETRIES": "3",
            },
            stderr=stderr,
            include_hook_events=True,
        )

        final_result: str | None = None
        async for message in query(prompt=prompt_stream(), options=options):
            if sandbox_failures:
                raise WorkerUnavailable(
                    "Claude reported that its sandbox is unavailable: " + " | ".join(sandbox_failures)
                )
            payload = _jsonable(message)
            session_id = getattr(message, "session_id", None)
            if session_id:
                self.db.update_task(
                    task.id,
                    claude_session_id=str(session_id),
                    worker_session_id=str(session_id),
                )
            self.db.add_event(
                task.id,
                run_id,
                f"claude.{type(message).__name__}",
                payload,
            )
            if isinstance(message, ResultMessage):
                record_claude_result_usage(self.db, task, run_id, message)
                final_result = getattr(message, "result", None)
                if getattr(message, "is_error", False):
                    subtype = getattr(message, "subtype", "error")
                    raise RuntimeError(f"Claude run failed: {subtype}: {final_result}")
        if sandbox_failures:
            raise WorkerUnavailable(
                "Claude reported that its sandbox is unavailable: " + " | ".join(sandbox_failures)
            )
        return final_result
