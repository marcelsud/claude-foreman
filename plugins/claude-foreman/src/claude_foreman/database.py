from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import ApprovalStatus, Goal, GoalStatus, Task, TaskStatus, resolve_worker_config


SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT REFERENCES goals(id),
    repo_path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'claude',
    model TEXT NOT NULL DEFAULT 'sonnet',
    effort TEXT NOT NULL DEFAULT 'medium',
    base_ref TEXT NOT NULL DEFAULT 'HEAD',
    workspace_group TEXT,
    branch_name TEXT,
    worktree_path TEXT,
    claude_session_id TEXT,
    worker_session_id TEXT,
    max_turns INTEGER NOT NULL DEFAULT 80,
    error TEXT,
    result_summary TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    exit_code INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_message TEXT,
    response_json TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_status_created ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS workflows (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    path TEXT,
    content TEXT,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ForemanDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "workspace_group" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN workspace_group TEXT")
            if "provider" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'")
            if "worker_session_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN worker_session_id TEXT")
                conn.execute(
                    "UPDATE tasks SET worker_session_id=claude_session_id "
                    "WHERE claude_session_id IS NOT NULL"
                )
            approval_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(approvals)")
            }
            if "response_json" not in approval_columns:
                conn.execute("ALTER TABLE approvals ADD COLUMN response_json TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_workspace_group ON tasks(workspace_group, status)"
            )
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_goal(self, title: str, description: str = "") -> Goal:
        if not title.strip():
            raise ValueError("goal title must not be empty")
        goal_id = str(uuid.uuid4())
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, title.strip(), description, GoalStatus.ACTIVE, now, now),
            )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> Goal:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            raise KeyError(f"goal not found: {goal_id}")
        return Goal(**dict(row))

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM goals"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def update_goal_status(self, goal_id: str, status: str) -> Goal:
        if status not in set(GoalStatus):
            raise ValueError(f"invalid goal status: {status}")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE goals SET status=?, updated_at=? WHERE id=?",
                (status, utcnow(), goal_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"goal not found: {goal_id}")
        return self.get_goal(goal_id)

    def create_task(
        self,
        *,
        repo_path: str,
        prompt: str,
        goal_id: str | None = None,
        priority: int = 0,
        provider: str | None = None,
        model: str | None = None,
        effort: str = "medium",
        base_ref: str = "HEAD",
        max_turns: int = 80,
        depends_on: list[str] | None = None,
        workspace_group: str | None = None,
    ) -> Task:
        provider, model, effort = resolve_worker_config(provider, model, effort)
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        task_id = str(uuid.uuid4())
        now = utcnow()
        with self.connect() as conn:
            if goal_id and not conn.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone():
                raise KeyError(f"goal not found: {goal_id}")
            conn.execute(
                """INSERT INTO tasks (
                    id, goal_id, repo_path, prompt, status, priority, provider, model, effort,
                    base_ref, workspace_group, max_turns, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    goal_id,
                    str(Path(repo_path).expanduser().resolve()),
                    prompt.strip(),
                    TaskStatus.QUEUED,
                    int(priority),
                    provider,
                    model,
                    effort,
                    base_ref,
                    workspace_group,
                    max(1, int(max_turns)),
                    now,
                    now,
                ),
            )
            for dep in depends_on or []:
                conn.execute(
                    "INSERT INTO task_dependencies(task_id, depends_on) VALUES (?, ?)",
                    (task_id, dep),
                )
        self.add_event(
            task_id, None, "task.created",
            {"provider": provider, "model": model, "effort": effort},
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"task not found: {task_id}")
        return Task(**dict(row))

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def claim_next_task(self) -> Task | None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT t.id
                   FROM tasks t
                   LEFT JOIN goals g ON g.id=t.goal_id
                   WHERE t.status=?
                     AND t.cancel_requested=0
                     AND (t.goal_id IS NULL OR g.status=?)
                     AND NOT EXISTS (
                        SELECT 1 FROM task_dependencies d
                        JOIN tasks parent ON parent.id=d.depends_on
                        WHERE d.task_id=t.id AND parent.status != ?
                     )
                     AND (
                        t.workspace_group IS NULL OR NOT EXISTS (
                            SELECT 1 FROM tasks active
                            WHERE active.workspace_group=t.workspace_group
                              AND active.id != t.id
                              AND active.status IN (?, ?, ?, ?)
                        )
                     )
                   ORDER BY t.priority DESC, t.created_at ASC
                   LIMIT 1""",
                (
                    TaskStatus.QUEUED,
                    GoalStatus.ACTIVE,
                    TaskStatus.COMPLETED,
                    TaskStatus.PREPARING,
                    TaskStatus.RUNNING,
                    TaskStatus.AWAITING_APPROVAL,
                    TaskStatus.VERIFYING,
                ),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE tasks SET status=?, started_at=COALESCE(started_at, ?), updated_at=? WHERE id=? AND status=?",
                (TaskStatus.PREPARING, now, now, row["id"], TaskStatus.QUEUED),
            )
            if cur.rowcount != 1:
                return None
        return self.get_task(row["id"])

    def workspace_for_group(self, workspace_group: str) -> tuple[str, str] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT worktree_path, branch_name FROM tasks
                   WHERE workspace_group=? AND worktree_path IS NOT NULL AND branch_name IS NOT NULL
                   ORDER BY created_at ASC LIMIT 1""",
                (workspace_group,),
            ).fetchone()
        if not row:
            return None
        return str(row["worktree_path"]), str(row["branch_name"])

    def update_task(self, task_id: str, **fields: Any) -> Task:
        allowed = {
            "status", "branch_name", "worktree_path", "claude_session_id",
            "worker_session_id", "error",
            "result_summary", "cancel_requested", "started_at", "completed_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if not fields:
            return self.get_task(task_id)
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", values)
            if cur.rowcount != 1:
                raise KeyError(f"task not found: {task_id}")
        return self.get_task(task_id)

    def configure_queued_task(
        self,
        task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        priority: int | None = None,
        max_turns: int | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        if task.status != TaskStatus.QUEUED:
            raise ValueError(f"only queued tasks can be reconfigured, not {task.status}")
        changes: dict[str, Any] = {}
        if provider is not None or model is not None or effort is not None:
            target_provider = provider or task.provider
            target_model = model
            if provider is None and model is None:
                target_model = task.model
            elif provider is None and model is not None:
                target_provider = None
            elif provider is not None and model is None:
                target_model = task.model if provider == task.provider else None
            target_effort = effort or (
                task.effort if provider is None or provider == task.provider else "medium"
            )
            resolved_provider, resolved_model, resolved_effort = resolve_worker_config(
                target_provider, target_model, target_effort
            )
            changes.update(
                provider=resolved_provider, model=resolved_model, effort=resolved_effort
            )
        if priority is not None:
            changes["priority"] = int(priority)
        if max_turns is not None:
            changes["max_turns"] = max(1, int(max_turns))
        if not changes:
            raise ValueError("provide at least one task configuration field")
        changes["updated_at"] = utcnow()
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=? AND status=?",
                [*changes.values(), task_id, TaskStatus.QUEUED],
            )
            if cur.rowcount != 1:
                raise ValueError("task left the queue while it was being configured")
        self.add_event(task_id, None, "task.configured", changes)
        return self.get_task(task_id)

    def request_cancel(self, task_id: str) -> Task:
        current = self.get_task(task_id)
        if current.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return current
        task = self.update_task(task_id, cancel_requested=1)
        if task.status in {TaskStatus.QUEUED, TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED}:
            task = self.update_task(task_id, status=TaskStatus.CANCELLED, completed_at=utcnow())
        self.add_event(task_id, None, "task.cancel_requested", {})
        return task

    def create_run(self, task_id: str) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            attempt = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM runs WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO runs(id, task_id, attempt, status, pid, started_at) VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, task_id, attempt, os.getpid(), utcnow()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, exit_code: int | None, error: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, completed_at=?, exit_code=?, error=? WHERE id=?",
                (status, utcnow(), exit_code, error, run_id),
            )

    def add_event(
        self,
        task_id: str | None,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any] | list[Any] | str,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO events(task_id, run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, run_id, kind, json.dumps(payload, ensure_ascii=False, default=str), utcnow()),
            )
            return int(cur.lastrowid)

    def events(self, task_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (task_id, after_id, max(1, min(limit, 1000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def event_tail(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, max(1, min(limit, 1000))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def create_approval(
        self,
        *,
        task_id: str,
        run_id: str | None,
        tool_name: str,
        input_data: dict[str, Any],
        request_hash: str,
        risk: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        approval_id = str(uuid.uuid4())
        created = datetime.now(UTC)
        expires = created + timedelta(seconds=timeout_seconds)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO approvals(
                    id, task_id, run_id, tool_name, input_json, request_hash, risk,
                    status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id, task_id, run_id, tool_name,
                    json.dumps(input_data, ensure_ascii=False, sort_keys=True), request_hash,
                    risk, ApprovalStatus.PENDING, created.isoformat(), expires.isoformat(),
                ),
            )
        self.update_task(task_id, status=TaskStatus.AWAITING_APPROVAL)
        self.add_event(
            task_id,
            run_id,
            "approval.requested",
            {"approval_id": approval_id, "tool_name": tool_name, "risk": risk, "input": input_data},
        )
        return self.get_approval(approval_id)

    def expire_pending_approvals(self) -> int:
        now = utcnow()
        expired: list[tuple[str, str, str | None]] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, run_id FROM approvals WHERE status=? AND expires_at<=?",
                (ApprovalStatus.PENDING, now),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    """UPDATE approvals SET status=?, decision_message=?, decided_by=?, decided_at=?
                       WHERE id=? AND status=?""",
                    (
                        ApprovalStatus.EXPIRED,
                        "Approval expired",
                        "foreman",
                        now,
                        row["id"],
                        ApprovalStatus.PENDING,
                    ),
                )
                if cur.rowcount == 1:
                    expired.append((row["id"], row["task_id"], row["run_id"]))
        for approval_id, task_id, run_id in expired:
            self.add_event(
                task_id,
                run_id,
                "approval.expired",
                {"approval_id": approval_id},
            )
        return len(expired)

    def close_pending_approvals(self, run_id: str, message: str) -> int:
        closed: list[tuple[str, str]] = []
        now = utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id FROM approvals WHERE run_id=? AND status=?",
                (run_id, ApprovalStatus.PENDING),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    """UPDATE approvals SET status=?, decision_message=?, decided_by=?, decided_at=?
                       WHERE id=? AND status=?""",
                    (
                        ApprovalStatus.REJECTED,
                        message,
                        "foreman",
                        now,
                        row["id"],
                        ApprovalStatus.PENDING,
                    ),
                )
                if cur.rowcount == 1:
                    closed.append((row["id"], row["task_id"]))
        for approval_id, task_id in closed:
            self.add_event(
                task_id,
                run_id,
                "approval.closed",
                {"approval_id": approval_id, "message": message},
            )
        return len(closed)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        self.expire_pending_approvals()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError(f"approval not found: {approval_id}")
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        response_json = item.pop("response_json", None)
        item["response"] = json.loads(response_json) if response_json else None
        return item

    def list_approvals(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        self.expire_pending_approvals()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status=? ORDER BY created_at ASC LIMIT ?",
                (status, max(1, min(limit, 1000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json"))
            response_json = item.pop("response_json", None)
            item["response"] = json.loads(response_json) if response_json else None
            result.append(item)
        return result

    def approvals_for_task(
        self, task_id: str, status: str | None = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        self.expire_pending_approvals()
        query = "SELECT * FROM approvals WHERE task_id=?"
        params: list[Any] = [task_id]
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json"))
            response_json = item.pop("response_json", None)
            item["response"] = json.loads(response_json) if response_json else None
            result.append(item)
        return result

    def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by: str,
        message: str = "",
        request_hash: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval = self.get_approval(approval_id)
        if approval["status"] != ApprovalStatus.PENDING:
            raise ValueError(f"approval is already {approval['status']}")
        if request_hash and request_hash != approval["request_hash"]:
            raise ValueError("request hash mismatch; refusing stale approval")
        if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(UTC):
            status = ApprovalStatus.EXPIRED
            approve = False
            message = message or "Approval expired"
        else:
            status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE approvals SET status=?, decision_message=?, response_json=?, decided_by=?, decided_at=?
                   WHERE id=? AND status=?""",
                (
                    status,
                    message,
                    json.dumps(response, ensure_ascii=False, sort_keys=True) if response else None,
                    decided_by,
                    utcnow(),
                    approval_id,
                    ApprovalStatus.PENDING,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("approval was decided concurrently; refresh before retrying")
        self.add_event(
            approval["task_id"],
            approval["run_id"],
            "approval.decided",
            {"approval_id": approval_id, "status": status, "decided_by": decided_by, "message": message},
        )
        return self.get_approval(approval_id)

    def recover_interrupted_tasks(self) -> list[str]:
        active_statuses = (
            TaskStatus.PREPARING,
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.VERIFYING,
        )
        recovered: list[tuple[str, str]] = []
        now = utcnow()
        placeholders = ",".join("?" for _ in active_statuses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id, cancel_requested FROM tasks WHERE status IN ({placeholders})",
                active_statuses,
            ).fetchall()
            for row in rows:
                status = TaskStatus.CANCELLED if row["cancel_requested"] else TaskStatus.QUEUED
                completed_at = now if status == TaskStatus.CANCELLED else None
                conn.execute(
                    """UPDATE tasks SET status=?, error=?, completed_at=?, updated_at=? WHERE id=?""",
                    (status, "previous daemon process was interrupted", completed_at, now, row["id"]),
                )
                recovered.append((row["id"], status))
            conn.execute(
                """UPDATE runs SET status='interrupted', completed_at=?, error=?
                   WHERE status='running'""",
                (now, "daemon process was interrupted"),
            )
        with self.connect() as conn:
            run_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM runs WHERE status='interrupted'")
            ]
        for run_id in run_ids:
            self.close_pending_approvals(run_id, "run was interrupted")
        for task_id, status in recovered:
            self.add_event(task_id, None, "task.recovered", {"status": status})
        return [task_id for task_id, _ in recovered]

    def latest_run_id(self, task_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)
            ).fetchone()
        return row["id"] if row else None

    def accept_task(self, task_id: str, actor: str = "codex") -> Task:
        task = self.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            return task
        if task.status != TaskStatus.AWAITING_REVIEW:
            raise ValueError(f"task must be awaiting_review, not {task.status}")
        task = self.update_task(task_id, status=TaskStatus.COMPLETED, completed_at=utcnow())
        self.add_event(task_id, self.latest_run_id(task_id), "task.accepted", {"actor": actor})
        return task

    def requeue_task(self, task_id: str, feedback: str, actor: str = "codex") -> Task:
        task = self.get_task(task_id)
        if task.status not in {TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise ValueError(f"cannot requeue task in state {task.status}")
        self.add_event(
            task_id,
            self.latest_run_id(task_id),
            "review.feedback",
            {"actor": actor, "feedback": feedback.strip()},
        )
        return self.update_task(
            task_id,
            status=TaskStatus.QUEUED,
            error=None,
            result_summary=None,
            cancel_requested=0,
            completed_at=None,
        )

    def latest_review_feedback(self, task_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM events
                   WHERE task_id=? AND kind='review.feedback'
                   ORDER BY id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return str(json.loads(row["payload_json"]).get("feedback") or "") or None

    def propose_workflow(self, name: str, definition: dict[str, Any]) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("workflow name must not be empty")
        tasks = definition.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("workflow definition requires a non-empty tasks array")
        keys: set[str] = set()
        children: dict[str, int] = {}
        for item in tasks:
            if not isinstance(item, dict) or not item.get("key") or not item.get("prompt"):
                raise ValueError("each workflow task requires key and prompt")
            resolve_worker_config(
                item.get("provider"), item.get("model"), item.get("effort", "medium")
            )
            if item["key"] in keys:
                raise ValueError(f"duplicate workflow task key: {item['key']}")
            keys.add(item["key"])
        for item in tasks:
            dependencies = item.get("depends_on", [])
            if not isinstance(dependencies, list):
                raise ValueError(f"workflow task {item['key']} depends_on must be an array")
            if len(dependencies) > 1:
                raise ValueError(
                    "workflow phases may have at most one dependency so each worktree has a linear history"
                )
            unknown = set(dependencies) - keys
            if unknown:
                raise ValueError(f"workflow task {item['key']} has unknown dependencies: {sorted(unknown)}")
            for dependency in dependencies:
                children[dependency] = children.get(dependency, 0) + 1
                if children[dependency] > 1:
                    raise ValueError(
                        "workflow phases may not fan out; use separate workflow chains for parallel work"
                    )
        seen: set[str] = set()
        for item in tasks:
            dependencies = item.get("depends_on", [])
            if any(dependency not in seen for dependency in dependencies):
                raise ValueError("workflow tasks must be ordered after their dependencies")
            seen.add(item["key"])
        with self.connect() as conn:
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM workflows WHERE name=?", (name,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO workflows(name, version, definition_json, status, created_at) VALUES (?, ?, ?, 'proposed', ?)",
                (name, version, json.dumps(definition, ensure_ascii=False, sort_keys=True), utcnow()),
            )
        return self.get_workflow(name, version)

    def get_workflow(self, name: str, version: int | None = None) -> dict[str, Any]:
        query = "SELECT * FROM workflows WHERE name=?"
        params: list[Any] = [name]
        if version is None:
            query += " ORDER BY version DESC LIMIT 1"
        else:
            query += " AND version=?"
            params.append(version)
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            raise KeyError(f"workflow not found: {name}@{version or 'latest'}")
        item = dict(row)
        item["definition"] = json.loads(item.pop("definition_json"))
        return item

    def list_workflows(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workflows"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY name, version DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["definition"] = json.loads(item.pop("definition_json"))
            result.append(item)
        return result

    def review_workflow(
        self, name: str, version: int, *, approve: bool, actor: str, message: str = ""
    ) -> dict[str, Any]:
        workflow = self.get_workflow(name, version)
        if workflow["status"] != "proposed":
            raise ValueError(f"workflow is already {workflow['status']}")
        status = "active" if approve else "rejected"
        with self.connect() as conn:
            if approve:
                conn.execute("UPDATE workflows SET status='superseded' WHERE name=? AND status='active'", (name,))
            conn.execute(
                """UPDATE workflows SET status=?, reviewed_at=?, reviewed_by=?
                   WHERE name=? AND version=?""",
                (status, utcnow(), actor, name, version),
            )
        self.add_event(None, None, "workflow.reviewed", {
            "name": name, "version": version, "status": status, "actor": actor, "message": message
        })
        return self.get_workflow(name, version)

    def run_workflow(
        self,
        *,
        name: str,
        repo_path: str,
        goal_id: str | None = None,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT version FROM workflows WHERE name=? AND status='active' ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        if not row:
            raise ValueError(f"workflow has no active reviewed version: {name}")
        workflow = self.get_workflow(name, int(row["version"]))
        if not goal_id:
            goal_id = self.create_goal(
                title=f"Workflow: {name}",
                description=workflow["definition"].get("description", ""),
            ).id
        values = {str(key): str(value) for key, value in (inputs or {}).items()}
        created: dict[str, Task] = {}
        workspace_groups: dict[str, str] = {}
        for spec in workflow["definition"]["tasks"]:
            prompt = str(spec["prompt"])
            for key, value in values.items():
                prompt = prompt.replace("${" + key + "}", value)
            dependencies = [created[key].id for key in spec.get("depends_on", [])]
            if spec.get("depends_on"):
                workspace_group = workspace_groups[spec["depends_on"][0]]
            else:
                workspace_group = str(uuid.uuid4())
            workspace_groups[spec["key"]] = workspace_group
            created[spec["key"]] = self.create_task(
                repo_path=repo_path,
                prompt=prompt,
                goal_id=goal_id,
                priority=int(spec.get("priority", 0)),
                provider=spec.get("provider"),
                model=spec.get("model"),
                effort=spec.get("effort", "medium"),
                base_ref=spec.get("base_ref", "HEAD"),
                max_turns=int(spec.get("max_turns", 80)),
                depends_on=dependencies,
                workspace_group=workspace_group,
            )
        return {
            "workflow": {"name": name, "version": workflow["version"]},
            "goal_id": goal_id,
            "tasks": {key: task.to_dict() for key, task in created.items()},
        }
