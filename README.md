# Claude Foreman

[![CI](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml/badge.svg)](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Give Codex a background Claude Code worker for implementation tasks. Codex remains the manager: it defines the work, handles routine approvals, watches progress, reviews the diff, and asks you before anything risky.

Claude Foreman uses your existing Claude subscription login. It does not accept or use an Anthropic API key.

## What you get

- Background coding tasks that survive beyond a single Codex response
- One isolated Git worktree per task
- Durable goals, tasks, progress events, approvals, and workflows in SQLite
- Configurable Claude model, effort, priority, dependencies, and turn budget
- Sandboxed Claude Code commands with network access disabled by default
- A review gate before any result is accepted
- No automatic commits, pushes, merges, deployments, or worktree deletion

## Quick start

### 1. Check the prerequisites

You need:

- Codex with plugin support
- Python 3.11 or newer
- Git
- Linux, macOS, or WSL2
- A Claude Pro, Max, Team, or Enterprise subscription
- Claude Code signed in for the current OS user

Sign in if needed:

```bash
claude auth login
```

On Ubuntu or WSL Ubuntu, install the sandbox helpers:

```bash
sudo apt-get update
sudo apt-get install bubblewrap socat
```

### 2. Install Claude Foreman

```bash
git clone https://github.com/marcelsud/claude-foreman.git
cd claude-foreman
python3 plugins/claude-foreman/scripts/bootstrap_runtime.py
codex plugin marketplace add .
codex plugin add claude-foreman@claude-foreman
```

The runtime and persistent state are stored outside the clone under `~/.local/share/claude-foreman`.

### 3. Start a new Codex task

Open a new task in Codex after installation so it discovers the Claude Foreman skill and MCP tools.

Your target project must be a Git repository with at least one commit. Use its absolute path when delegating work.

### 4. Delegate your first task

Paste this into Codex and replace the repository path and requested change:

```text
Use Claude Foreman to implement this in /absolute/path/to/my-repo:

Add validation for empty usernames and cover it with tests.

Run Foreman doctor first. Use Claude Sonnet with medium effort, start the
task in the background, and monitor meaningful progress. Handle only routine
in-scope approvals. Ask me before risky or external actions. When Claude is
done, inspect the complete diff and test results. Requeue with specific
feedback if needed; otherwise accept it and tell me where the worktree is.
Do not commit, push, merge, or deploy.
```

You can keep talking to Codex while Claude works. Ask for status at any time:

```text
Show me the current Claude Foreman tasks, meaningful progress, and pending approvals.
```

## What happens next

| State | Meaning |
| --- | --- |
| `queued` | The task is waiting for the scheduler or a dependency. |
| `preparing` | Foreman is creating the isolated worktree and run. |
| `running` | Claude is working inside its isolated worktree. |
| `awaiting_approval` | Claude needs an answer or an exact approval decision. |
| `verifying` | Foreman is collecting the final repository state and results. |
| `awaiting_review` | Codex should inspect the full diff and verification results. |
| `completed` | Codex accepted the result after review. |
| `failed` / `cancelled` | The run ended without an accepted result. |

Codex can approve reversible work confined to the task. Credential access, sandbox bypass, destructive commands, publishing, deployment, and other critical actions must come back to you.

## Review and integrate the result

Acceptance is a review decision, not a merge. The changes remain uncommitted in the Foreman worktree. Ask Codex to show the worktree path and final diff, then decide how to integrate them into your branch.

For example:

```text
Show me the accepted task's worktree, branch, full diff, and the safest commands
to bring those changes into my current branch. Do not commit or merge yet.
```

## Choosing model and effort

- `sonnet` + `medium`: the default for routine implementation
- `sonnet` + `high`: larger refactors or difficult debugging
- `opus` + `xhigh`: unusually difficult work where cost and latency are acceptable

You can change model or effort while a task is still queued. Once claimed, the task keeps its original configuration for a reproducible audit trail.

## Reusable workflows

For complex work, ask Codex to propose a reviewed multi-phase workflow:

```text
Propose a Claude Foreman workflow for this repository with separate phases for
investigation, implementation, and verification. Show me every phase, model,
effort, dependency, and permission implication before activating it.
```

Each phase waits for review before its dependent phase starts.

## Troubleshooting

Ask Codex:

```text
Run Claude Foreman doctor and explain every failed check without changing anything.
```

Common causes are an expired Claude login, missing `bubblewrap` or `socat`, a repository without an initial commit, or opening Codex before the plugin was installed. After installing or updating, start a new Codex task.

Do not put the SQLite database or active worktrees in OneDrive or another synchronized directory. The defaults already use a local path.

## Developer documentation

- [Architecture, control model, and safety defaults](plugins/claude-foreman/README.md)
- [Contributing](CONTRIBUTING.md)
- [Approval policy](plugins/claude-foreman/skills/manage-claude-foreman/references/approval-policy.md)
- [Workflow schema](plugins/claude-foreman/skills/manage-claude-foreman/references/workflow-schema.md)

Claude Foreman is open source under the [MIT License](LICENSE).
