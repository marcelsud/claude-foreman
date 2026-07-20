from __future__ import annotations

import argparse
import json
from typing import Any

from .config import ForemanConfig
from .controller import DaemonController
from .database import ForemanDB
from .doctor import run_doctor


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foreman")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    daemon = sub.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop", "status"])
    goals = sub.add_parser("goals")
    goals.add_argument("--status")
    tasks = sub.add_parser("tasks")
    tasks.add_argument("--status")
    approvals = sub.add_parser("approvals")
    approvals.add_argument("--status", default="pending")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = ForemanConfig.from_env()
    db = ForemanDB(config.db_path)
    if args.command == "init":
        config.ensure_directories()
        db.initialize()
        _print({"ok": True, "db_path": str(config.db_path), "auth_mode": "subscription"})
    elif args.command == "doctor":
        _print(run_doctor(config))
    elif args.command == "daemon":
        controller = DaemonController(config)
        _print(getattr(controller, args.action)())
    elif args.command == "goals":
        db.initialize()
        _print(db.list_goals(args.status))
    elif args.command == "tasks":
        db.initialize()
        _print(db.list_tasks(args.status))
    elif args.command == "approvals":
        db.initialize()
        _print(db.list_approvals(args.status))


if __name__ == "__main__":
    main()
