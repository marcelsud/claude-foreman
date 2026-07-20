# Contributing

Thanks for helping improve Claude Foreman.

## Development setup

1. Fork and clone the repository.
2. Create a virtual environment with Python 3.11 or newer.
3. Install the project with `.venv/bin/pip install -e .`.
4. Run `.venv/bin/python -m unittest discover -s tests -v`.
5. Run `.venv/bin/python scripts/foreman_mcp.py --self-test`.

Keep changes focused and include tests for behavior changes. Never add API keys, OAuth tokens, state databases, daemon logs, or active worktrees to commits.

## Pull requests

Describe the problem, the chosen approach, safety implications, and verification performed. Changes to approval policy, sandboxing, authentication, or destructive-action handling should include explicit regression tests.
