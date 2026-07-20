from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from time import monotonic
from typing import Any

from .config import (
    ForemanConfig,
    codex_subscription_command,
    subscription_environment,
)
from .database import ForemanDB
from .models import Task
from .worktrees import SANDBOX_ARTIFACT_PATHS


def _git_bytes(path: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "git failed")
    return result.stdout


def worktree_fingerprint(worktree: str | Path) -> str:
    """Hash the tracked patch plus untracked regular files before verification."""
    path = Path(worktree).resolve()
    digest = hashlib.sha256()
    digest.update(_git_bytes(path, "rev-parse", "HEAD"))
    digest.update(_git_bytes(path, "diff", "HEAD", "--binary", "--"))
    names = _git_bytes(path, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_name in sorted(filter(None, names.split(b"\0"))):
        relative = raw_name.decode(errors="surrogateescape")
        candidate = path / relative
        normalized = relative.replace("\\", "/").strip("/")
        if normalized in SANDBOX_ARTIFACT_PATHS:
            continue
        if candidate.exists() and not (
            candidate.is_file() or candidate.is_dir() or candidate.is_symlink()
        ):
            continue
        digest.update(raw_name)
        if candidate.is_file() and not candidate.is_symlink():
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        elif candidate.is_symlink():
            digest.update(b"symlink:")
            digest.update(os.readlink(candidate).encode(errors="surrogateescape"))
        else:
            digest.update(f"special:{candidate.stat().st_mode}".encode())
    return "sha256:" + digest.hexdigest()


class _AppServerCommands:
    def __init__(self, config: ForemanConfig):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.next_id = 1

    async def __aenter__(self) -> "_AppServerCommands":
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLI is unavailable for sandboxed verification")
        self.process = await asyncio.create_subprocess_exec(
            *codex_subscription_command(codex, "app-server"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=subscription_environment({"CLAUDE_FOREMAN_COMPONENT": "verification"}),
        )
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude-foreman-verifier",
                        "title": "Claude Foreman Verifier",
                        "version": "0.3.1",
                    }
                },
            )
            await self.send({"method": "initialized"})
            return self
        except Exception:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_args: Any) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader and not self.reader.done():
            self.reader.cancel()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)

    async def send(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex App Server is not running")
        self.process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send({"id": request_id, "method": method, "params": params})
        return await future

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                request_id = message.get("id")
                if request_id is None:
                    continue
                future = self.pending.pop(request_id, None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(f"Codex RPC error: {message['error']}"))
                    else:
                        future.set_result(message.get("result"))
        finally:
            error = RuntimeError("Codex App Server exited during verification")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)


async def run_verification_gates(
    config: ForemanConfig,
    db: ForemanDB,
    task: Task,
    run_id: str,
    worktree: str | Path,
) -> dict[str, Any]:
    gates = db.verification_gates(task.id)
    if not gates:
        return db.verification_summary(task.id)
    root = Path(worktree).resolve()
    snapshot_sha = worktree_fingerprint(root)
    db.add_event(
        task.id,
        run_id,
        "verification.started",
        {"gate_count": len(gates), "snapshot_sha": snapshot_sha},
    )
    try:
        completed_gates: set[str] = set()
        async with _AppServerCommands(config) as server:
            for gate in gates:
                started = monotonic()
                try:
                    result = await server.request(
                        "command/exec",
                        {
                            "command": shlex.split(gate["command"], posix=os.name != "nt"),
                            "cwd": str(root),
                            "sandboxPolicy": {
                                "type": "workspaceWrite",
                                "writableRoots": [str(root)],
                                "networkAccess": False,
                            },
                            "timeoutMs": config.verification_timeout_seconds * 1000,
                            "outputBytesCap": 50_000,
                        },
                    )
                    exit_code = int(result["exitCode"])
                    status = "passed" if exit_code == 0 else "failed"
                    stdout = str(result.get("stdout") or "")
                    stderr = str(result.get("stderr") or "")
                except Exception as exc:
                    exit_code = None
                    status = "timed_out" if "timeout" in str(exc).lower() else "error"
                    stdout = ""
                    stderr = f"{type(exc).__name__}: {exc}"
                duration_ms = round((monotonic() - started) * 1000)
                db.record_verification_result(
                    gate_id=gate["id"],
                    task_id=task.id,
                    run_id=run_id,
                    status=status,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    stdout_excerpt=stdout,
                    stderr_excerpt=stderr,
                    snapshot_sha=snapshot_sha,
                )
                completed_gates.add(str(gate["id"]))
                db.add_event(
                    task.id,
                    run_id,
                    "verification.gate_completed",
                    {
                        "gate_id": gate["id"],
                        "command": gate["command"],
                        "status": status,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "snapshot_sha": snapshot_sha,
                    },
                )
    except Exception as exc:
        for gate in gates:
            if str(gate["id"]) in completed_gates:
                continue
            db.record_verification_result(
                gate_id=gate["id"], task_id=task.id, run_id=run_id,
                status="error", exit_code=None, duration_ms=None,
                stdout_excerpt="", stderr_excerpt=f"{type(exc).__name__}: {exc}",
                snapshot_sha=snapshot_sha,
            )
    summary = db.verification_summary(task.id)
    db.add_event(
        task.id,
        run_id,
        "verification.completed",
        {"required_ok": summary["required_ok"], "counts": summary["counts"]},
    )
    return summary
