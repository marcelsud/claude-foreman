from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WorkerProvider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
DEFAULT_MODELS = {
    WorkerProvider.CLAUDE: "sonnet",
    WorkerProvider.CODEX: "gpt-5.6-sol",
}


def resolve_worker_config(
    provider: str | None, model: str | None, effort: str
) -> tuple[str, str, str]:
    if model is not None and not model.strip():
        raise ValueError("model must not be empty")
    clean_model = model.strip() if model else None
    inferred = WorkerProvider.CODEX if clean_model and clean_model.startswith("gpt-") else WorkerProvider.CLAUDE
    try:
        resolved_provider = WorkerProvider(provider or inferred)
    except ValueError as exc:
        raise ValueError(f"invalid provider: {provider}") from exc
    resolved_model = clean_model or DEFAULT_MODELS[resolved_provider]
    if resolved_provider == WorkerProvider.CLAUDE and resolved_model.startswith("gpt-"):
        raise ValueError("GPT models require provider 'codex'")
    if resolved_provider == WorkerProvider.CODEX and resolved_model not in CODEX_MODELS:
        raise ValueError(
            "Codex model must be one of: " + ", ".join(CODEX_MODELS)
        )
    allowed_efforts = {"low", "medium", "high", "xhigh", "max"}
    if resolved_provider == WorkerProvider.CODEX and resolved_model != "gpt-5.6-luna":
        allowed_efforts.add("ultra")
    if effort not in allowed_efforts:
        raise ValueError(f"invalid effort {effort!r} for {resolved_model}")
    return str(resolved_provider), resolved_model, effort


@dataclass(slots=True)
class Goal:
    id: str
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Task:
    id: str
    goal_id: str | None
    repo_path: str
    prompt: str
    status: str
    priority: int
    provider: str
    model: str
    effort: str
    base_ref: str
    workspace_group: str | None
    branch_name: str | None
    worktree_path: str | None
    claude_session_id: str | None
    worker_session_id: str | None
    max_turns: int
    error: str | None
    result_summary: str | None
    cancel_requested: int
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
