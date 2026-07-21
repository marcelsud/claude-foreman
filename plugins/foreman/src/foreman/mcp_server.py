from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from . import __version__
from .approval_policy import human_only
from .config import ForemanConfig
from .controller import DaemonController
from .database import ForemanDB
from .doctor import run_doctor
from .worktrees import WorktreeManager


JSON = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: JSON
    handler: Callable[[JSON], Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False

    def definition(self) -> JSON:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
        }


def obj(properties: JSON | None = None, required: list[str] | None = None) -> JSON:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _task_payload(db: ForemanDB, task_id: str) -> JSON:
    task = db.get_task(task_id).to_dict()
    task["events_tail"] = db.event_tail(task_id, limit=20)
    task["pending_approvals"] = db.approvals_for_task(task_id, "pending", 100)
    task["verification"] = db.verification_summary(task_id)
    task["usage"] = db.task_usage(task_id)
    return task


def _bounded_process_output(
    command: list[str], limit: int, allowed_returncodes: set[int]
) -> tuple[bytes, bool]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    output = process.stdout.read(limit + 1)
    truncated = len(output) > limit
    if truncated:
        process.kill()
    remaining, stderr = process.communicate()
    if not truncated:
        output += remaining
    if not truncated and process.returncode not in allowed_returncodes:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "command failed")
    return output[:limit], truncated


class ForemanTools:
    def __init__(self, config: ForemanConfig):
        self.config = config
        config.ensure_directories()
        self.db = ForemanDB(config.db_path, data_dir=config.data_dir)
        self.db.initialize()
        self.controller = DaemonController(config)
        self.worktrees = WorktreeManager(config)
        self.mutation_lock = threading.RLock()
        self.tools = self._build()

    def _build(self) -> dict[str, Tool]:
        string = {"type": "string"}
        integer = {"type": "integer"}
        boolean = {"type": "boolean"}
        tools = [
            Tool(
                "doctor",
                "Check Foreman, Git, Claude and ChatGPT subscription authentication, and worker readiness without exposing credentials.",
                obj(),
                lambda _: run_doctor(self.config),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "goal_create",
                "Create a durable goal that can own one or more background coding tasks.",
                obj({"title": string, "description": string}, ["title"]),
                lambda a: self.db.create_goal(a["title"], a.get("description", "")).to_dict(),
            ),
            Tool(
                "goal_list",
                "List Foreman goals, optionally filtered by status.",
                obj({"status": {"type": "string", "enum": ["active", "paused", "completed", "cancelled"]}}),
                lambda a: self.db.list_goals(a.get("status")),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "goal_set_status",
                "Pause, resume, complete, or cancel a goal.",
                obj(
                    {
                        "goal_id": string,
                        "status": {"type": "string", "enum": ["active", "paused", "completed", "cancelled"]},
                    },
                    ["goal_id", "status"],
                ),
                lambda a: self.db.update_goal_status(a["goal_id"], a["status"]).to_dict(),
                idempotent=True,
            ),
            Tool(
                "task_create",
                "Queue an isolated Claude or Codex coding task in a Git repository and optionally start the scheduler.",
                obj(
                    {
                        "repo_path": string,
                        "prompt": string,
                        "goal_id": string,
                        "priority": integer,
                        "provider": {"type": "string", "enum": ["claude", "codex"]},
                        "model": string,
                        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]},
                        "base_ref": string,
                        "max_turns": integer,
                        "depends_on": {"type": "array", "items": string},
                        "verification_commands": {"type": "array", "items": string},
                        "autostart": boolean,
                    },
                    ["repo_path", "prompt"],
                ),
                self._task_create,
            ),
            Tool(
                "task_list",
                "List tasks in compact operational form by default, optionally returning full task rows.",
                obj({"status": string, "limit": integer, "compact": boolean}),
                lambda a: self.db.list_tasks(
                    a.get("status"), a.get("limit", 100), a.get("compact", True)
                ),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_get",
                "Get a task with recent events and pending approval requests.",
                obj({"task_id": string}, ["task_id"]),
                lambda a: _task_payload(self.db, a["task_id"]),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_configure",
                "Change the provider, model, effort, priority, or turn budget of a task while it is still queued.",
                obj(
                    {
                        "task_id": string,
                        "provider": {"type": "string", "enum": ["claude", "codex"]},
                        "model": string,
                        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]},
                        "priority": integer,
                        "max_turns": integer,
                        "verification_commands": {"type": "array", "items": string},
                    },
                    ["task_id"],
                ),
                lambda a: self.db.configure_queued_task(
                    a["task_id"],
                    provider=a.get("provider"),
                    model=a.get("model"),
                    effort=a.get("effort"),
                    priority=a.get("priority"),
                    max_turns=a.get("max_turns"),
                    verification_commands=a.get("verification_commands"),
                ).to_dict(),
            ),
            Tool(
                "task_usage",
                "Aggregate subscription usage by model and run for one task. Dollar values are API-equivalent estimates, not actual subscription charges.",
                obj({"task_id": string}, ["task_id"]),
                lambda a: self.db.task_usage(a["task_id"]),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "goal_usage",
                "Aggregate task and model usage for one durable goal.",
                obj({"goal_id": string}, ["goal_id"]),
                lambda a: self.db.goal_usage(a["goal_id"]),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_events",
                "Read structured progress events for a task after an event cursor.",
                obj({"task_id": string, "after_id": integer, "limit": integer}, ["task_id"]),
                lambda a: self.db.events(a["task_id"], a.get("after_id", 0), a.get("limit", 200)),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_wait",
                "Wait up to a bounded timeout for one actionable durable task event and return a reusable cursor; timeout is a normal recovery heartbeat.",
                obj(
                    {
                        "task_id": string,
                        "after_id": integer,
                        "timeout_seconds": integer,
                        "actionable_only": boolean,
                        "limit": integer,
                    },
                    ["task_id"],
                ),
                lambda a: self.db.wait_for_task(
                    a["task_id"],
                    after_id=a.get("after_id", 0),
                    timeout_seconds=a.get("timeout_seconds", 30),
                    actionable_only=a.get("actionable_only", True),
                    limit=a.get("limit", 200),
                ),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_diff",
                "Read the current worktree status, diff stat, and bounded unified diff for Codex review.",
                obj({"task_id": string, "max_chars": integer}, ["task_id"]),
                self._task_diff,
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "task_cancel",
                "Request cancellation of a queued or running task.",
                obj({"task_id": string}, ["task_id"]),
                lambda a: self.db.request_cancel(a["task_id"]).to_dict(),
                destructive=True,
                idempotent=True,
            ),
            Tool(
                "task_accept",
                "Accept a reviewed worktree result and mark its task complete. This does not commit, push, or delete anything.",
                obj({"task_id": string, "actor": string}, ["task_id"]),
                lambda a: self.db.accept_task(a["task_id"], a.get("actor", "codex")).to_dict(),
                idempotent=True,
            ),
            Tool(
                "task_requeue",
                "Return a failed or review-ready task to the queue with concrete review feedback.",
                obj({"task_id": string, "feedback": string, "actor": string}, ["task_id", "feedback"]),
                lambda a: self.db.requeue_task(
                    a["task_id"], a["feedback"], a.get("actor", "codex")
                ).to_dict(),
            ),
            Tool(
                "approval_list",
                "List durable approval or input requests awaiting a manager decision.",
                obj({"status": string, "limit": integer}),
                lambda a: self.db.list_approvals(a.get("status", "pending"), a.get("limit", 100)),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "approval_decide",
                "Allow or reject one exact, hash-bound worker request. Critical actions require human_confirmed=true.",
                obj(
                    {
                        "approval_id": string,
                        "approve": boolean,
                        "message": string,
                        "actor": string,
                        "request_hash": string,
                        "human_confirmed": boolean,
                        "answers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    ["approval_id", "approve", "request_hash"],
                ),
                self._approval_decide,
            ),
            Tool(
                "workflow_propose",
                "Create an immutable proposed workflow version. Proposed workflows cannot run until reviewed.",
                obj({"name": string, "definition": {"type": "object"}}, ["name", "definition"]),
                lambda a: self.db.propose_workflow(a["name"], a["definition"]),
            ),
            Tool(
                "workflow_list",
                "List workflow versions and their proposed, active, rejected, or superseded status.",
                obj({"status": string}),
                lambda a: self.db.list_workflows(a.get("status")),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "workflow_review",
                "Approve or reject a proposed workflow version after Codex inspects every phase and permission boundary.",
                obj(
                    {"name": string, "version": integer, "approve": boolean, "actor": string, "message": string},
                    ["name", "version", "approve"],
                ),
                lambda a: self.db.review_workflow(
                    a["name"], a["version"], approve=a["approve"],
                    actor=a.get("actor", "codex"), message=a.get("message", "")
                ),
            ),
            Tool(
                "workflow_run",
                "Compile the active reviewed workflow into dependency-gated tasks for one repository.",
                obj(
                    {"name": string, "repo_path": string, "goal_id": string,
                     "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
                     "autostart": boolean},
                    ["name", "repo_path"],
                ),
                self._workflow_run,
            ),
            Tool(
                "daemon_status",
                "Report whether the background scheduler is running.",
                obj(),
                lambda _: self.controller.status(),
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "daemon_start",
                "Start the local background scheduler using subscription authentication only.",
                obj(),
                lambda _: self.controller.start(),
                idempotent=True,
            ),
            Tool(
                "daemon_stop",
                "Stop the local scheduler after active workers finish their current cancellation handling.",
                obj(),
                lambda _: self.controller.stop(),
                destructive=True,
                idempotent=True,
            ),
        ]
        return {tool.name: tool for tool in tools}

    def _task_create(self, args: JSON) -> JSON:
        task = self.db.create_task(
            repo_path=args["repo_path"],
            prompt=args["prompt"],
            goal_id=args.get("goal_id"),
            priority=args.get("priority", 0),
            provider=args.get("provider"),
            model=args.get("model"),
            effort=args.get("effort", "medium"),
            base_ref=args.get("base_ref", "HEAD"),
            max_turns=args.get("max_turns", 80),
            depends_on=args.get("depends_on", []),
            verification_commands=args.get("verification_commands", []),
        )
        result = task.to_dict()
        if args.get("autostart", True):
            result["daemon"] = self.controller.start()
        return result

    def _approval_decide(self, args: JSON) -> JSON:
        approval = self.db.get_approval(args["approval_id"])
        if args["approve"] and human_only(approval["risk"], approval["tool_name"], approval["input"]):
            if not args.get("human_confirmed", False):
                raise PermissionError(
                    "this action is human-only; ask the user and retry with human_confirmed=true"
                )
        response = None
        if approval["tool_name"] == "AskUserQuestion" and args["approve"]:
            answers = args.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("AskUserQuestion approval requires an answers object")
            questions = approval["input"].get("questions", [])
            expected = {str(question.get("question", "")) for question in questions}
            if not expected or set(answers) != expected:
                raise ValueError(
                    "answers must contain exactly one entry for each question text in the approval input"
                )
            if any(not isinstance(value, str) or not value.strip() for value in answers.values()):
                raise ValueError("every answer must be a non-empty string")
            response = {"answers": answers}
        elif args.get("answers") is not None:
            raise ValueError("answers are only valid for AskUserQuestion approvals")
        return self.db.decide_approval(
            args["approval_id"],
            approve=args["approve"],
            decided_by=args.get("actor", "codex"),
            message=args.get("message", ""),
            request_hash=args["request_hash"],
            response=response,
        )

    def _task_diff(self, args: JSON) -> JSON:
        task = self.db.get_task(args["task_id"])
        if not task.worktree_path:
            raise ValueError("task does not have a worktree yet")
        max_chars = max(1000, min(int(args.get("max_chars", 100_000)), 500_000))
        snapshot = self.worktrees.snapshot(task.worktree_path)
        diff, truncated = _bounded_process_output(
            ["git", "-C", task.worktree_path, "diff", "HEAD", "--binary", "--"],
            max_chars,
            {0},
        )
        chunks = [diff]
        remaining = max_chars - len(diff)
        names_truncated = False
        names = [
            str(path).encode(errors="surrogateescape")
            for path in snapshot["intended_untracked"]
        ]
        for raw_path in names:
            remaining = max_chars - sum(len(chunk) for chunk in chunks)
            if remaining <= 0:
                truncated = True
                break
            relative_path = raw_path.decode(errors="surrogateescape")
            addition, addition_truncated = _bounded_process_output(
                [
                    "git", "-C", task.worktree_path, "diff", "--no-index", "--binary",
                    "--", "/dev/null", relative_path,
                ],
                remaining,
                {0, 1},
            )
            chunks.append(addition)
            if addition_truncated:
                truncated = True
                break
        truncated = truncated or names_truncated
        diff = b"".join(chunks).decode(errors="replace")
        snapshot["diff"] = diff
        snapshot["truncated"] = truncated
        snapshot["worktree_path"] = task.worktree_path
        return snapshot

    def _workflow_run(self, args: JSON) -> JSON:
        result = self.db.run_workflow(
            name=args["name"], repo_path=args["repo_path"],
            goal_id=args.get("goal_id"), inputs=args.get("inputs", {})
        )
        if args.get("autostart", True):
            result["daemon"] = self.controller.start()
        return result


class MCPServer:
    def __init__(
        self,
        toolset: ForemanTools,
        *,
        input_stream: Iterable[str] | None = None,
        output_stream: TextIO | None = None,
        max_request_workers: int = 16,
        max_wait_workers: int = 32,
    ):
        self.toolset = toolset
        self.input_stream = input_stream if input_stream is not None else sys.stdin
        self.output_stream = output_stream if output_stream is not None else sys.stdout
        self.max_request_workers = max(1, int(max_request_workers))
        self.max_wait_workers = max(1, int(max_wait_workers))
        self._send_lock = threading.Lock()

    def send(self, payload: JSON) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._send_lock:
            self.output_stream.write(encoded)
            self.output_stream.flush()

    def handle(self, message: JSON) -> JSON | None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "foreman", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool.definition() for tool in self.toolset.tools.values()]}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                tool = self.toolset.tools.get(name)
                if not tool:
                    raise KeyError(f"unknown tool: {name}")
                if tool.read_only:
                    value = tool.handler(params.get("arguments") or {})
                else:
                    with self.toolset.mutation_lock:
                        value = tool.handler(params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
                    "structuredContent": {"result": value},
                    "isError": False,
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            if method == "tools/call":
                result = {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                }
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }

    def run(self) -> None:
        with ThreadPoolExecutor(
            max_workers=self.max_request_workers,
            thread_name_prefix="foreman-mcp",
        ) as request_executor, ThreadPoolExecutor(
            max_workers=self.max_wait_workers,
            thread_name_prefix="foreman-wait",
        ) as wait_executor:
            try:
                for raw in self.input_stream:
                    if not raw.strip():
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32700, "message": str(exc)},
                            }
                        )
                        continue
                    if not isinstance(message, dict):
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32600, "message": "request must be an object"},
                            }
                        )
                        continue
                    params = message.get("params")
                    is_wait = (
                        message.get("method") == "tools/call"
                        and isinstance(params, dict)
                        and params.get("name") == "task_wait"
                    )
                    executor = wait_executor if is_wait else request_executor
                    executor.submit(self._handle_and_send, message)
            finally:
                # EOF means the MCP client disconnected. Wake long-running reads
                # so the bridge can exit instead of waiting for their timeouts.
                self.toolset.db.wake.close()

    def _handle_and_send(self, message: JSON) -> None:
        response = self.handle(message)
        if response is not None:
            self.send(response)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    args, _ = parser.parse_known_args(argv)
    toolset = ForemanTools(ForemanConfig.from_env())
    if args.self_test:
        print(json.dumps({"ok": True, "tools": sorted(toolset.tools)}, indent=2))
        return
    MCPServer(toolset).run()


if __name__ == "__main__":
    main()
