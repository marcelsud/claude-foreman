from __future__ import annotations

from typing import Any

from .database import ForemanDB
from .models import Task


def _number(data: dict[str, Any], *names: str) -> int:
    for name in names:
        value = data.get(name)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


def normalized_usage(data: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": _number(data, "input_tokens", "inputTokens"),
        "cache_creation_input_tokens": _number(
            data,
            "cache_creation_input_tokens", "cacheCreationInputTokens",
            "cacheWriteInputTokens",
        ),
        "cache_read_input_tokens": _number(
            data, "cache_read_input_tokens", "cacheReadInputTokens", "cachedInputTokens"
        ),
        "output_tokens": _number(data, "output_tokens", "outputTokens"),
        "reasoning_output_tokens": _number(
            data, "reasoning_output_tokens", "reasoningOutputTokens"
        ),
        "total_tokens": _number(data, "total_tokens", "totalTokens"),
    }


def record_claude_result_usage(
    db: ForemanDB, task: Task, run_id: str, message: Any
) -> None:
    model_usage = getattr(message, "model_usage", None) or {}
    duration_ms = getattr(message, "duration_ms", None)
    total_cost = getattr(message, "total_cost_usd", None)
    if isinstance(model_usage, dict) and model_usage:
        for model, raw in model_usage.items():
            if not isinstance(raw, dict):
                continue
            cost = raw.get("costUSD", raw.get("cost_usd"))
            db.upsert_run_usage(
                run_id=run_id,
                task_id=task.id,
                provider="claude",
                model=str(model),
                duration_ms=duration_ms,
                api_equivalent_cost_usd=float(cost) if cost is not None else None,
                **normalized_usage(raw),
            )
        return
    usage = getattr(message, "usage", None) or {}
    if isinstance(usage, dict):
        db.upsert_run_usage(
            run_id=run_id,
            task_id=task.id,
            provider="claude",
            model=task.model,
            duration_ms=duration_ms,
            api_equivalent_cost_usd=float(total_cost) if total_cost is not None else None,
            **normalized_usage(usage),
        )


def record_codex_usage(
    db: ForemanDB,
    task: Task,
    run_id: str,
    token_usage: dict[str, Any],
) -> None:
    total = token_usage.get("total") or {}
    if not isinstance(total, dict):
        return
    usage = normalized_usage(total)
    usage["input_tokens"] = max(
        0,
        usage["input_tokens"]
        - usage["cache_creation_input_tokens"]
        - usage["cache_read_input_tokens"],
    )
    db.upsert_run_usage(
        run_id=run_id,
        task_id=task.id,
        provider="codex",
        model=task.model,
        **usage,
    )
