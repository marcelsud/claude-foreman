from __future__ import annotations

import argparse
import json
from typing import Any

from .config import ForemanConfig
from .controller import DaemonController
from .dashboard import run_monitor
from .database import ForemanDB
from .doctor import run_doctor
from .models import TaskStatus


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
    monitor = sub.add_parser("monitor", help="open the live terminal task dashboard")
    monitor.add_argument("--status", choices=[str(status) for status in TaskStatus])
    monitor.add_argument("--interval", type=float, default=1.0)
    monitor.add_argument("--limit", type=int, default=100)
    monitor.add_argument("--once", action="store_true", help="render one snapshot and exit")
    monitor.add_argument("--no-color", action="store_true")
    approvals = sub.add_parser("approvals")
    approvals.add_argument("--status", default="pending")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = ForemanConfig.from_env()
    db = ForemanDB(config.db_path, data_dir=config.data_dir)
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
    elif args.command == "monitor":
        db.initialize()
        run_monitor(
            db,
            DaemonController(config),
            status=args.status,
            interval=max(0.1, args.interval),
            limit=max(1, min(args.limit, 1000)),
            once=args.once,
            color=False if args.no_color else None,
        )
    elif args.command == "approvals":
        db.initialize()
        _print(db.list_approvals(args.status))


if __name__ == "__main__":
    main()
