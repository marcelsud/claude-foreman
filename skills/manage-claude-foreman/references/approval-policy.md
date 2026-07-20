# Approval policy

## Codex may decide

- Clarifying questions that do not change scope
- Reversible commands confined to the task worktree
- Test, lint, formatter, and build commands using already-installed dependencies. Foreman auto-allows only its narrow command allowlist; review all other shell requests.
- Reading public documentation when the domain and purpose are expected for the task
- Creating temporary files inside the worktree or Foreman run directory

Bind every decision to the supplied `request_hash`. Prefer one-time approval; never create persistent allow rules from a worker request.

## Reject or redirect

- Writing outside the task worktree
- Reading credential files, shell profiles, browser data, SSH configuration, or unrelated repositories
- Disabling or bypassing the sandbox
- Modifying Foreman, Claude, Codex, hook, or workflow policy during the run
- Installing unrequested dependencies
- Contacting an unexplained network destination
- Destructive Git operations or deletion whose target is not exact and recoverable

Explain why and suggest a scoped alternative.

## Require explicit user confirmation

- Force-push, merge, or production branch updates
- Deployment, infrastructure apply, or production database migration
- Credential access or authorization changes
- Deletion of shared or material data
- Sandbox bypass or privileged execution

Approval must describe the exact command, target, expected effect, and recovery path. A general instruction such as "finish the task" is not confirmation for these actions.
