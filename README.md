# Foreman

[![CI](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml/badge.svg)](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Give Claude Code or Codex background Claude Code and Codex workers for implementation tasks. Your interactive assistant remains the manager: it defines the work, handles routine approvals, watches progress, reviews the diff, and asks you before anything risky.

Foreman uses your existing Claude and ChatGPT subscription logins. It removes Anthropic and OpenAI API credentials from worker processes, so delegated work cannot silently switch to pay-as-you-go API billing.

## What you get

- Background coding tasks that survive beyond a single manager response
- One isolated Git worktree per task
- Durable goals, tasks, progress events, approvals, and workflows in SQLite
- Native token and duration summaries by model, run, task, requeue, and goal
- Configurable provider, model, effort, priority, dependencies, and turn budget
- Claude models plus the GPT-5.6 Codex family: Sol, Terra, and Luna
- Sandboxed worker commands with network access disabled by default
- A review gate before any result is accepted
- Foreman-run verification gates with command, exit code, duration, output, and tested snapshot
- No automatic commits, pushes, merges, deployments, or worktree deletion

## Quick start

### 1. Check the prerequisites

You need:

- Claude Code or Codex with plugin support
- Python 3.11 or newer
- Git
- Linux, macOS, or WSL2
- At least one supported subscription login:
  - Claude Pro, Max, Team, or Enterprise with Claude Code signed in
  - ChatGPT with Codex CLI signed in

Sign in if needed:

```bash
claude auth login
codex login
```

The Claude worker needs the sandbox helpers below on Ubuntu or WSL Ubuntu. Codex workers use the Codex CLI sandbox and do not require them.

```bash
sudo apt-get update
sudo apt-get install bubblewrap socat
```

### 2. Bootstrap Foreman

```bash
git clone https://github.com/marcelsud/claude-foreman.git
cd claude-foreman
python3 plugins/foreman/scripts/bootstrap_runtime.py
```

The runtime and persistent state are stored outside the clone under `~/.local/share/foreman` and are shared by the Codex and Claude Code integrations.

### 3. Install it in your manager

For Codex:

```bash
codex plugin marketplace add .
codex plugin add foreman@foreman
```

For Claude Code, run these commands inside Claude Code:

```text
/plugin marketplace add marcelsud/claude-foreman
/plugin install foreman@foreman
/reload-plugins
```

The Claude Code plugin exposes the same MCP tools and the skill
`/foreman:manage-foreman-agents`. For local plugin development without
installing the marketplace, start Claude Code with:

```bash
claude --plugin-dir ./plugins/foreman
```

### 4. Start a manager session

Open a new Codex task after installation, or reload/restart Claude Code, so the manager discovers the Foreman skill and MCP tools.

Your target project must be a Git repository with at least one commit. Use its absolute path when delegating work.

### 5. Delegate your first task

Paste this into Claude Code or Codex and replace the repository path and requested change:

```text
Use Foreman to implement this in /absolute/path/to/my-repo:

Add validation for empty usernames and cover it with tests.

Run Foreman doctor first. Use Claude Sonnet with medium effort, start the
task in the background, and monitor meaningful progress. Handle only routine
in-scope approvals. Ask me before risky or external actions. When Claude is
done, inspect the complete diff and test results. Requeue with specific
feedback if needed; otherwise accept it and tell me where the worktree is.
Do not commit, push, merge, or deploy.
```

To delegate the same work to Codex through your ChatGPT subscription, replace the model instruction with:

```text
Use the Codex provider with gpt-5.6-terra and medium effort. Do not use an API key.
```

You can keep talking to the managing assistant while the worker runs. Ask for status at any time:

```text
Show me the current Foreman tasks, meaningful progress, and pending approvals.
```

`task_list` is compact by default, so operational status does not repeat full prompts. Ask for a full listing only when you need the original task definitions.

## What happens next

| State | Meaning |
| --- | --- |
| `queued` | The task is waiting for the scheduler or a dependency. |
| `preparing` | Foreman is creating the isolated worktree and run. |
| `running` | The selected worker is working inside its isolated worktree. |
| `awaiting_approval` | The worker needs an answer or an exact approval decision. |
| `verifying` | Foreman is collecting the final repository state and results. |
| `awaiting_review` | The manager should inspect the full diff and verification results. |
| `completed` | The manager accepted the result after review. |
| `failed` / `cancelled` | The run ended without an accepted result. |

The manager can approve reversible work confined to the task. Credential access, sandbox bypass, destructive commands, publishing, deployment, and other critical actions must come back to you.

## Review and integrate the result

Acceptance is a review decision, not a merge. The changes remain uncommitted in the Foreman worktree. Ask the manager to show the worktree path and final diff, then decide how to integrate them into your branch.

For example:

```text
Show me the accepted task's worktree, branch, full diff, and the safest commands
to bring those changes into my current branch. Do not commit or merge yet.
```

## Choosing model and effort

| Provider | Model | Good fit | Effort |
| --- | --- | --- | --- |
| Claude | `sonnet` | Default, routine implementation | `medium` or `high` |
| Claude | `opus` | Unusually difficult work | up to `max` |
| Codex | `gpt-5.6-sol` | Strongest complex coding and research | `low` through `ultra` |
| Codex | `gpt-5.6-terra` | Balanced everyday coding | `low` through `ultra` |
| Codex | `gpt-5.6-luna` | Fastest iteration | `low` through `max` |

If you specify a `gpt-5.6-*` model, Foreman infers the Codex provider. If you specify only `provider: codex`, it defaults to `gpt-5.6-sol`. Claude Sonnet remains the default for backward compatibility.

You can change model or effort while a task is still queued. Once claimed, the task keeps its original configuration for a reproducible audit trail.

## Usage and verification

Add exact verification commands when delegating work:

```text
Use gpt-5.6-terra with high effort. After implementation, have Foreman run
these verification gates: npm test, npm run lint, and git status.
```

Foreman executes declared gates independently through the Codex App Server command sandbox: argv-only, workspace write access, no network, bounded output, and a configurable timeout. Results include the exit code, duration, output excerpts, and a fingerprint of the worktree state tested.

Ask for aggregated usage at any time:

```text
Show task usage by run and model, including new input, cache created, cache read,
output, reasoning output, total tokens, and duration. Then show the goal total.
```

For subscription-authenticated runs, a monetary charge per task is not available. Claude may report an API-equivalent dollar estimate; Foreman labels it as an estimate. Codex subscription runs report tokens and duration without inventing a dollar cost.

## Reusable workflows

For complex work, ask the manager to propose a reviewed multi-phase workflow:

```text
Propose a Foreman workflow for this repository with separate phases for
investigation, implementation, and verification. Show me every phase, model,
effort, dependency, and permission implication before activating it.
```

Each phase waits for review before its dependent phase starts.

## Troubleshooting

Ask your manager:

```text
Run Foreman doctor and explain every failed check without changing anything.
```

Common causes are an expired Claude or ChatGPT login, missing `bubblewrap` or `socat` for Claude workers, a repository without an initial commit, an unavailable verification executable, or starting the manager before the plugin was installed. After installing or updating, reload plugins or start a new manager session.

Do not put the SQLite database or active worktrees in OneDrive or another synchronized directory. The defaults already use a local path.

## Developer documentation

- [Architecture, control model, and safety defaults](plugins/foreman/README.md)
- [Contributing](CONTRIBUTING.md)
- [Approval policy](plugins/foreman/skills/manage-foreman-agents/references/approval-policy.md)
- [Workflow schema](plugins/foreman/skills/manage-foreman-agents/references/workflow-schema.md)

Foreman is open source under the [MIT License](LICENSE).
