---
name: manage-foreman-agents
description: Manage background Claude Code or Codex implementation work through the Foreman MCP tools. Use when Claude Code or Codex needs to create goals, delegate isolated Git-worktree tasks, choose a provider, model, or effort level, monitor structured progress, decide routine approval requests, escalate dangerous actions to the user, review diffs, request rework, accept results, or create and review reusable multi-phase workflows.
---

# Manage Foreman

Use Claude or Codex as an implementation worker and keep the managing session responsible for scope, approvals, and final review.

## Delegate work

1. Call `doctor` before the first task in a session. Require `auth_mode: subscription` and confirm the chosen provider is ready; never request or configure an Anthropic or OpenAI API key.
2. Create a goal for multi-task outcomes. For one atomic task, call `task_create` directly.
3. Supply an absolute Git repository path, a concrete completion condition, provider, model, effort, and relevant verification commands in the prompt.
   Pass routine commands through `verification_commands` so Foreman executes them independently and records structured gates. Do not encode shell operators, network access, deployment, or destructive actions as verification gates.
   Use `task_configure` to retune a queued task; never change model or effort after it has been claimed.
4. Keep unrelated tasks in separate worktrees. Use dependencies when one task needs an accepted result from another.
5. Start the daemon only when queued work should run.

For Claude, use `sonnet` with `medium` effort for routine implementation and `opus` only for unusually difficult work. For Codex, choose `gpt-5.6-terra` for balanced everyday work, `gpt-5.6-sol` for the hardest work, or `gpt-5.6-luna` for the fastest iteration. Sol and Terra support through `ultra`; Luna supports through `max`. Preserve an explicit user choice.

## Monitor and unblock

Use compact `task_list` for operational status. Poll `task_get` or `task_events` only when details are needed, using the last event ID as a cursor. Report meaningful phase changes, blockers, approval requests, and completed verification; do not relay every low-level event.

Use `task_usage` for per-run and per-model usage and `goal_usage` for project totals. Treat `input_tokens` as new, non-cached input. Never describe `api_equivalent_cost_usd` as an actual subscription charge; a null cost means the subscription provider did not expose a per-task monetary value.

For each pending approval:

1. Inspect the exact tool, input, risk, request hash, worktree, and task scope.
2. Approve only the exact hash presented by the tool.
3. Allow routine in-scope operations when the effect is reversible and confined to the task worktree.
4. Reject scope expansion, credential access, sandbox bypass, policy edits, unexplained network access, and destructive commands. Include a useful alternative in the rejection message.
5. Ask the user before any human-only action. Only pass `human_confirmed: true` after explicit confirmation for that exact action.

For `AskUserQuestion`, present the worker's exact questions and options. Approve with an `answers` object whose keys exactly match the question text; Foreman returns those structured answers to the paused worker. Do not treat a plain approval message as an answer.

Read [approval-policy.md](references/approval-policy.md) when an approval is ambiguous.

## Review results

When a task reaches `awaiting_review`:

1. Call `task_diff` and inspect the complete diff. If truncated, request smaller task-specific diffs before deciding.
2. Compare the changes with the original task and repository instructions.
3. Check the recorded tests and run additional verification when risk warrants it.
   Prefer declared Foreman verification gates. Require every required gate to be `passed`; inspect its snapshot fingerprint, exit code, duration, and bounded output when a gate fails.
4. Call `task_requeue` with concrete feedback when work is incomplete or incorrect.
5. Call `task_accept` only when the task is genuinely complete. Acceptance does not commit, push, merge, or delete the worktree.

Do not mark a goal complete until all required tasks are accepted and the combined outcome is verified.

## Build reviewed workflows

Use `workflow_propose` for reusable multi-phase work. Inspect every task prompt, dependency, model, effort, and permission implication before calling `workflow_review`.

Only active reviewed workflow versions may run. Each compiled phase waits for Codex to accept its dependency, creating a review gate between phases. Read [workflow-schema.md](references/workflow-schema.md) before proposing or changing a workflow.
