# Claude Foreman developer reference

[![CI](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml/badge.svg)](https://github.com/marcelsud/claude-foreman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

Claude Foreman is a local control plane that lets Codex manage background Claude Code and Codex coding tasks. It keeps goals, tasks, runs, approvals, and audit events in SQLite; isolates every task in a Git worktree; runs Claude through the Claude Agent SDK; and runs GPT-5.6 workers through the Codex App Server.

It never accepts Anthropic or OpenAI API billing credentials. Worker processes remove API-key, gateway, cloud-provider, and access-token environment variables before starting. Claude uses the saved Claude Code login; Codex requires App Server to report a saved `chatgpt` account and rejects `apiKey` accounts.

## Requirements

- Python 3.11+
- Git
- At least one ready worker provider: Claude Code with a Claude subscription, or Codex CLI with a ChatGPT subscription
- Linux, macOS, or WSL2 for Claude Code Bash sandboxing
- On Linux/WSL: `bubblewrap` (`bwrap`) and `socat`

The final sandbox-helper requirement applies to Claude workers. Codex workers use the Codex CLI `workspace-write` sandbox.

Do not keep the state database or active worktrees in OneDrive. The defaults use `~/.local/share/claude-foreman`.

For the user-oriented installation and first-task guide, start with the [repository README](../../README.md).

## Set up a source checkout

```bash
git clone https://github.com/marcelsud/claude-foreman.git
cd claude-foreman/plugins/claude-foreman
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/claude-foreman init
.venv/bin/claude-foreman doctor
python3 scripts/bootstrap_runtime.py
```

On Linux/WSL, install the sandbox dependencies with your system package manager. For Ubuntu:

```bash
sudo apt-get install bubblewrap socat
```

Start a new Codex task after installing or updating the plugin so Codex discovers its skill and MCP tools.

## Install for development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/claude-foreman init
.venv/bin/claude-foreman doctor
```

Start the scheduler:

```bash
.venv/bin/claude-foreman daemon start
```

The bundled Codex plugin starts its MCP bridge with `scripts/foreman_mcp.py`. In a source checkout the bridge automatically re-executes under `.venv`; `FOREMAN_PYTHON` can override that interpreter.

Before installing a cached/personal copy of the plugin, create its stable shared runtime:

```bash
python3 scripts/bootstrap_runtime.py
```

The installed bridge uses `~/.local/share/claude-foreman/runtime`, injects its own cached `src/` into `PYTHONPATH`, and keeps SQLite state under `~/.local/share/claude-foreman`. A source checkout falls back to its ignored `.foreman-data/` only when the managed Codex sandbox cannot write the home directory.

## Control model

- Goals group durable outcomes; tasks carry a repository, prompt, provider, model, effort, turn budget, and dependencies. Queued tasks can be retuned with `task_configure` before the scheduler claims them.
- The detached scheduler atomically claims ready tasks from SQLite and records structured progress events.
- Codex reads events, answers scoped approval requests, reviews the worktree diff, then accepts or requeues the task.
- Clarifying questions are durable approval records; Codex returns structured selections through `approval_decide.answers`, allowing either paused worker to resume.
- `task_cancel` interrupts an active SDK query. Stopping the daemon cancels its active workers before exiting.
- Reviewed workflows compile into dependency-gated phases. A linear phase chain shares one isolated worktree so later phases see accepted earlier changes.
- Workflow versions are immutable and cannot run until Codex explicitly activates the reviewed version.

## Safety defaults

- Work occurs only in Foreman-created Git worktrees.
- Claude uses `permission_mode="default"` plus a `PreToolUse` policy hook. In-worktree reads/edits and a narrow allowlist of test, lint, build, and read-only Git commands are auto-allowed and audited; arbitrary shell commands require a manager decision.
- Bash runs in Claude's sandbox with network denied, local binding denied, and unsandboxed commands disabled.
- On Linux/WSL, startup fails closed unless both `bubblewrap` (`bwrap`) and `socat` are installed; Foreman never accepts Claude Code's unsandboxed fallback.
- Codex runs through App Server with `workspace-write`, `on-request` approvals, no model fallback, and no environment capabilities. Foreman verifies ChatGPT authentication and the selected model/effort against the live model catalog before starting the turn.
- App Server approvals are exact and single-use. Foreman never returns `acceptForSession` or persists a relaxed permission rule.
- External paths, web/MCP access, destructive commands, Git commits/publication, and questions become exact hash-bound approval requests.
- Force-push, merge, deployment, infrastructure apply, and sandbox bypass additionally require explicit human confirmation.
- Foreman never commits, pushes, merges, deletes a worktree, or deploys automatically.

SQLite state, daemon logs, and worktrees default to `~/.local/share/claude-foreman`. Override the root with `FOREMAN_DATA_DIR`, or use `FOREMAN_DB_PATH`, `FOREMAN_WORKTREES_DIR`, `FOREMAN_LOGS_DIR`, and `FOREMAN_PID_PATH` individually. Prefer a local, non-synced filesystem when your sandbox policy permits it. Worker concurrency is `FOREMAN_MAX_WORKERS`; polling is `FOREMAN_POLL_INTERVAL`.

## Development commands

```bash
python3 -m unittest discover -s tests -v
python3 scripts/foreman_mcp.py --self-test
```

Contributions are welcome. See [CONTRIBUTING.md](../../CONTRIBUTING.md) for the development and pull-request workflow.
