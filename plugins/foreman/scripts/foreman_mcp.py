#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def default_data_dir() -> Path:
    current = Path.home() / ".local" / "share" / "foreman"
    legacy = Path.home() / ".local" / "share" / "claude-foreman"
    if current.exists() or not legacy.exists():
        return current
    return legacy


if "FOREMAN_DATA_DIR" not in os.environ:
    preferred_data = default_data_dir()
    try:
        preferred_data.mkdir(parents=True, exist_ok=True)
        if not os.access(preferred_data, os.W_OK):
            raise OSError(f"data directory is not writable: {preferred_data}")
        os.environ["FOREMAN_DATA_DIR"] = str(preferred_data)
    except OSError:
        os.environ["FOREMAN_DATA_DIR"] = str(ROOT / ".foreman-data")

source_path = str(ROOT / "src")
existing_pythonpath = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = (
    source_path if not existing_pythonpath else source_path + os.pathsep + existing_pythonpath
)

# A source checkout keeps its own runtime so Codex can launch the MCP bridge
# with plain python3 while workers still use the SDK-enabled interpreter.
configured_python = os.environ.get("FOREMAN_PYTHON")
source_venv_python = ROOT / ".venv" / "bin" / "python"
shared_venv_python = Path(os.environ["FOREMAN_DATA_DIR"]) / "runtime" / "bin" / "python"
if configured_python:
    target_python = Path(configured_python)
elif source_venv_python.is_file():
    target_python = source_venv_python
else:
    target_python = shared_venv_python
if target_python.is_file() and Path(sys.executable).resolve() != target_python.resolve():
    environment = dict(os.environ)
    environment["FOREMAN_PYTHON"] = str(target_python.resolve())
    os.execve(
        str(target_python.resolve()),
        [str(target_python.resolve()), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

sys.path.insert(0, str(ROOT / "src"))

# Use an explicitly configured interpreter for daemon workers while keeping this
# bridge runnable from a source checkout without installation.
os.environ.setdefault("FOREMAN_PYTHON", sys.executable)

from foreman.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    main()
