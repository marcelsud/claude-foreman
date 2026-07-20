# Contributing

Thanks for helping improve Foreman.

## Development setup

1. Fork and clone the repository.
2. Create a virtual environment with Python 3.11 or newer.
3. Change to `plugins/foreman`.
4. Create the environment with `python3 -m venv .venv`.
5. Install the project with `.venv/bin/pip install -e .`.
6. Run `.venv/bin/python -m unittest discover -s tests -v`.
7. Run `.venv/bin/python scripts/foreman_mcp.py --self-test`.

Keep changes focused and include tests for behavior changes. Never add API keys, OAuth tokens, state databases, daemon logs, or active worktrees to commits.

## Versioning

`plugins/foreman/src/foreman/__init__.py` is the single source of truth for the Foreman version. After changing `__version__`, run `python scripts/sync_version.py` from `plugins/foreman` to update the generated Codex, Claude Code, and marketplace metadata. CI runs the same command with `--check` and rejects unsynchronized versions.

## Pull requests

Describe the problem, the chosen approach, safety implications, and verification performed. Changes to approval policy, sandboxing, authentication, or destructive-action handling should include explicit regression tests.
