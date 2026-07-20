from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from .approval_policy import auto_allow, classify_risk, request_hash
from .config import ForemanConfig, subscription_environment
from .database import ForemanDB
from .models import ApprovalStatus, Task, TaskStatus
from .usage import record_codex_usage


class CodexUnavailable(RuntimeError):
    pass


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            secret_key = (
                lowered in {
                    "token", "accesstoken", "access_token", "authtoken", "auth_token",
                    "refreshtoken", "refresh_token", "apikey", "api_key",
                }
                or "email" in lowered
                or "credential" in lowered
                or "secret" in lowered
            )
            if secret_key:
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 8000:
        return value[:8000] + "…[truncated]"
    return value


class CodexAppServerWorker:
    """One-task Codex App Server client using the user's saved ChatGPT login."""

    def __init__(self, config: ForemanConfig, db: ForemanDB, policy: str):
        self.config = config
        self.db = db
        self.policy = policy
        self.process: asyncio.subprocess.Process | None = None
        self.task: Task | None = None
        self.run_id: str | None = None
        self.worktree: Path | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.final_result: str | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._completed: asyncio.Future[dict[str, Any]] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_requests: set[asyncio.Task[None]] = set()

    async def query(self, task: Task, run_id: str, worktree: Path) -> str | None:
        codex = shutil.which("codex")
        if not codex:
            raise CodexUnavailable("Codex CLI is not installed or is not on PATH")
        self.task, self.run_id, self.worktree = task, run_id, worktree.resolve()
        self._completed = asyncio.get_running_loop().create_future()
        env = subscription_environment({"CLAUDE_FOREMAN_WORKER_PROVIDER": "codex"})
        self.process = await asyncio.create_subprocess_exec(
            codex,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude-foreman",
                        "title": "Claude Foreman",
                        "version": "0.2.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._notify("initialized")
            account = await self._request("account/read", {"refreshToken": False})
            account_type = ((account or {}).get("account") or {}).get("type")
            if account_type != "chatgpt":
                raise CodexUnavailable(
                    "Codex must be logged in with ChatGPT subscription auth; "
                    f"found {account_type or 'no login'}"
                )
            self.db.add_event(task.id, run_id, "codex.auth_verified", {"type": "chatgpt"})
            models = await self._request("model/list", {"limit": 100})
            catalog = {item.get("model"): item for item in (models or {}).get("data", [])}
            selected = catalog.get(task.model)
            if not selected:
                raise CodexUnavailable(f"Codex model is not available to this account: {task.model}")
            efforts = {
                item.get("reasoningEffort")
                for item in selected.get("supportedReasoningEfforts", [])
            }
            if task.effort not in efforts:
                raise CodexUnavailable(
                    f"{task.model} does not advertise reasoning effort {task.effort}"
                )
            thread = await self._request(
                "thread/start",
                {
                    "model": task.model,
                    "cwd": str(worktree),
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": "workspace-write",
                    "developerInstructions": self.policy,
                    "ephemeral": True,
                    "environments": [],
                    "dynamicTools": [],
                    "allowProviderModelFallback": False,
                },
            )
            self.thread_id = thread["thread"]["id"]
            self.db.update_task(task.id, worker_session_id=self.thread_id)
            prompt = task.prompt
            feedback = self.db.latest_review_feedback(task.id)
            if feedback:
                prompt += f"\n\nManager review feedback from the previous attempt:\n{feedback}"
            turn = await self._request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "model": task.model,
                    "effort": task.effort,
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "cwd": str(worktree),
                },
            )
            self.turn_id = turn["turn"]["id"]
            completed = await self._completed
            status = (completed.get("turn") or completed).get("status")
            if status == "failed":
                error = (completed.get("turn") or {}).get("error")
                raise RuntimeError(f"Codex turn failed: {error}")
            if status == "interrupted":
                raise asyncio.CancelledError
            return self.final_result
        finally:
            await self._shutdown()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"id": request_id, "method": method, "params": params})
        return await future

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise CodexUnavailable("Codex App Server is not running")
        self.process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )
        await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._event("codex.protocol_error", {"line": line.decode(errors="replace")})
                    continue
                request_id = message.get("id")
                if request_id is not None and "method" in message:
                    handler = asyncio.create_task(self._handle_server_request(message))
                    self._server_requests.add(handler)
                    handler.add_done_callback(self._server_requests.discard)
                elif request_id is not None:
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(f"Codex RPC error: {message['error']}"))
                        else:
                            future.set_result(message.get("result"))
                else:
                    self._handle_notification(message)
        finally:
            error = CodexUnavailable("Codex App Server exited before the turn completed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            if self._completed and not self._completed.done():
                self._completed.set_exception(error)

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self._event("codex.stderr", {"text": line.decode(errors="replace")[-8000:]})

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", "notification"))
        params = message.get("params") or {}
        if (
            method == "thread/tokenUsage/updated"
            and self.task and self.run_id
            and isinstance(params.get("tokenUsage"), dict)
        ):
            record_codex_usage(
                self.db, self.task, self.run_id, params["tokenUsage"]
            )
        self._event("codex." + method.replace("/", "."), params)
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text"):
                self.final_result = str(item["text"])
        if method == "turn/completed" and self._completed and not self._completed.done():
            turn = params.get("turn") or {}
            if self.task and self.run_id and turn.get("durationMs") is not None:
                self.db.set_run_usage_duration(
                    self.run_id, self.task.model, int(turn["durationMs"])
                )
            self._completed.set_result(params)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        method = str(message.get("method"))
        params = message.get("params") or {}
        try:
            result = await self._approval_result(method, params)
            await self._send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._send(
                {"id": request_id, "error": {"code": -32000, "message": str(exc)}}
            )

    async def _approval_result(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.task and self.run_id and self.worktree
        if method == "item/commandExecution/requestApproval":
            tool_name = "Bash"
            input_data = {
                "command": params.get("command") or "",
                "cwd": params.get("cwd"),
                "reason": params.get("reason"),
                "additionalPermissions": params.get("additionalPermissions"),
                "networkApprovalContext": params.get("networkApprovalContext"),
            }
            risk = classify_risk(tool_name, input_data, self.worktree)
            if auto_allow(tool_name, input_data, self.worktree, risk):
                self._event("approval.auto_allowed", {"tool_name": tool_name, "risk": risk})
                return {"decision": "accept"}
            approved, _ = await self._wait_for_approval(tool_name, input_data, risk)
            return {"decision": "accept" if approved else "decline"}
        if method == "item/fileChange/requestApproval":
            tool_name = "Edit"
            input_data = {"path": params.get("grantRoot"), "reason": params.get("reason")}
            risk = classify_risk(tool_name, input_data, self.worktree)
            approved, _ = await self._wait_for_approval(tool_name, input_data, risk)
            return {"decision": "accept" if approved else "decline"}
        if method == "item/tool/requestUserInput":
            tool_name = "AskUserQuestion"
            input_data = {"questions": params.get("questions") or []}
            approved, response = await self._wait_for_approval(tool_name, input_data, "needs_input")
            answers_by_text = (response or {}).get("answers", {}) if approved else {}
            answers: dict[str, Any] = {}
            for question in input_data["questions"]:
                answer = answers_by_text.get(question.get("question"), "")
                answers[str(question.get("id"))] = {"answers": [answer] if answer else []}
            return {"answers": answers}
        if method == "item/permissions/requestApproval":
            tool_name = "RequestPermissions"
            input_data = {
                "permissions": params.get("permissions") or {},
                "cwd": params.get("cwd"),
                "reason": params.get("reason"),
            }
            approved, _ = await self._wait_for_approval(tool_name, input_data, "high")
            return {
                "permissions": input_data["permissions"] if approved else {},
                "scope": "turn",
            }
        raise RuntimeError(f"unsupported Codex server request: {method}")

    async def _wait_for_approval(
        self, tool_name: str, input_data: dict[str, Any], risk: str
    ) -> tuple[bool, dict[str, Any] | None]:
        assert self.task and self.run_id
        digest = request_hash(self.task.id, tool_name, input_data)
        approval = self.db.create_approval(
            task_id=self.task.id,
            run_id=self.run_id,
            tool_name=tool_name,
            input_data=input_data,
            request_hash=digest,
            risk=risk,
            timeout_seconds=self.config.approval_timeout_seconds,
        )
        while True:
            await asyncio.sleep(self.config.poll_interval)
            if self.db.get_task(self.task.id).cancel_requested:
                return False, None
            current = self.db.get_approval(approval["id"])
            if current["status"] == ApprovalStatus.APPROVED:
                self.db.update_task(self.task.id, status=TaskStatus.RUNNING)
                return True, current.get("response")
            if current["status"] in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
                self.db.update_task(self.task.id, status=TaskStatus.RUNNING)
                return False, current.get("response")

    def _event(self, kind: str, payload: Any) -> None:
        if not self.task or not self.run_id:
            return
        safe = _redact(payload)
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
        if len(encoded) > 32_000:
            safe = {"truncated": True, "preview": encoded[:32_000]}
        self.db.add_event(self.task.id, self.run_id, kind, safe)

    async def _shutdown(self) -> None:
        if self.process and self.process.returncode is None:
            if self.thread_id and self.turn_id:
                try:
                    await asyncio.wait_for(
                        self._request(
                            "turn/interrupt",
                            {"threadId": self.thread_id, "turnId": self.turn_id},
                        ),
                        timeout=2,
                    )
                except (Exception, asyncio.CancelledError):
                    pass
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for request in self._server_requests:
            request.cancel()
        if self._server_requests:
            await asyncio.gather(*self._server_requests, return_exceptions=True)
        for reader in (self._reader_task, self._stderr_task):
            if reader and not reader.done():
                reader.cancel()
        await asyncio.gather(
            *(item for item in (self._reader_task, self._stderr_task) if item),
            return_exceptions=True,
        )
