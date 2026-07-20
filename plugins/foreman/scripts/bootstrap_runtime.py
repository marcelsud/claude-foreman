#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path


def default_runtime_dir() -> Path:
    current = Path.home() / ".local" / "share" / "foreman"
    legacy = Path.home() / ".local" / "share" / "claude-foreman"
    data_dir = current if current.exists() or not legacy.exists() else legacy
    return data_dir / "runtime"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create Foreman's shared subscription-worker runtime"
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=default_runtime_dir(),
    )
    args = parser.parse_args(argv)
    runtime = args.runtime_dir.expanduser().resolve()
    venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(runtime)
    python = runtime / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "claude-agent-sdk>=0.2.111"],
        check=True,
    )
    print(f"Foreman runtime ready: {python}")


if __name__ == "__main__":
    main()
