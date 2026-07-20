from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, TextIO

from .controller import DaemonController
from .database import ForemanDB


RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
COLORS = {
    "queued": "\x1b[38;5;245m",
    "preparing": "\x1b[38;5;39m",
    "running": "\x1b[38;5;45m",
    "awaiting_approval": "\x1b[38;5;214m",
    "verifying": "\x1b[38;5;141m",
    "awaiting_review": "\x1b[38;5;220m",
    "completed": "\x1b[38;5;82m",
    "failed": "\x1b[38;5;196m",
    "cancelled": "\x1b[38;5;244m",
}
STATUS_ICONS = {
    "queued": "○",
    "preparing": "◐",
    "running": "●",
    "awaiting_approval": "!",
    "verifying": "◆",
    "awaiting_review": "◎",
    "completed": "✓",
    "failed": "✕",
    "cancelled": "−",
}


def _clean(value: Any) -> str:
    """Make persisted worker text safe to paint in a terminal."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return "".join(character if character.isprintable() else " " for character in text)


def _clip(value: Any, width: int) -> str:
    if width <= 0:
        return ""
    text = _clean(value)
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _paint(text: str, style: str, enabled: bool) -> str:
    return f"{style}{text}{RESET}" if enabled else text


def _age(timestamp: Any, now: datetime) -> str:
    if not timestamp:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(timestamp))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = max(0, int((now - parsed.astimezone(UTC)).total_seconds()))
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _verification_label(task: dict[str, Any]) -> str:
    counts = (task.get("verification") or {}).get("counts") or {}
    total = sum(int(value or 0) for value in counts.values())
    if not total:
        return "—"
    passed = int(counts.get("passed") or 0)
    trouble = sum(int(counts.get(key) or 0) for key in ("failed", "error", "timed_out"))
    pending = int(counts.get("pending") or 0)
    if trouble:
        return f"{passed}/{total} !{trouble}"
    if pending:
        return f"{passed}/{total} …"
    return f"{passed}/{total} ✓"


def _progress(task: dict[str, Any]) -> str:
    latest = task.get("latest_progress") or {}
    summary = latest.get("summary")
    if summary:
        return _clean(summary)
    if task.get("result_summary"):
        return _clean(task["result_summary"])
    return "No significant progress yet"


def _token_count(value: Any) -> str:
    number = int(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return str(number)


@dataclass(slots=True)
class TaskMonitor:
    db: ForemanDB
    controller: DaemonController
    status_filter: str | None = None
    limit: int = 100
    tasks: list[dict[str, Any]] = field(default_factory=list)
    daemon: dict[str, Any] = field(default_factory=dict)
    selected: int = 0
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None

    def refresh(self) -> None:
        selected_id = self.selected_task.get("id") if self.selected_task else None
        try:
            self.tasks = self.db.list_tasks(
                self.status_filter, self.limit, compact=True
            )
            self.daemon = self.controller.status()
            self.error = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        if selected_id:
            self.selected = next(
                (
                    index
                    for index, task in enumerate(self.tasks)
                    if task.get("id") == selected_id
                ),
                min(self.selected, max(0, len(self.tasks) - 1)),
            )
        else:
            self.selected = min(self.selected, max(0, len(self.tasks) - 1))
        self.refreshed_at = datetime.now(UTC)

    @property
    def selected_task(self) -> dict[str, Any] | None:
        if not self.tasks:
            return None
        return self.tasks[min(self.selected, len(self.tasks) - 1)]

    def move(self, amount: int) -> None:
        if self.tasks:
            self.selected = max(0, min(len(self.tasks) - 1, self.selected + amount))

    def render(self, width: int, height: int, color: bool) -> str:
        width = max(60, width)
        height = max(18, height)
        now = datetime.now(UTC)
        lines: list[str] = []
        daemon_running = bool(self.daemon.get("running"))
        daemon_label = (
            f"● daemon running (pid {self.daemon.get('pid')})"
            if daemon_running
            else "○ daemon stopped"
        )
        daemon_style = COLORS["completed"] if daemon_running else COLORS["failed"]
        title = _paint("Foreman", BOLD, color)
        refreshed = self.refreshed_at.astimezone().strftime("%H:%M:%S")
        lines.append(f"{title}  {_paint(daemon_label, daemon_style, color)}")

        counts: dict[str, int] = {}
        for task in self.tasks:
            task_status = str(task.get("status") or "unknown")
            counts[task_status] = counts.get(task_status, 0) + 1
        count_text = "  ".join(
            f"{STATUS_ICONS.get(status, '·')} {status.replace('_', ' ')} {count}"
            for status, count in sorted(counts.items())
        ) or "no tasks"
        filter_text = f"filter: {self.status_filter}" if self.status_filter else "all tasks"
        lines.append(
            _clip(f"{count_text}   |   {filter_text}   |   refreshed {refreshed}", width)
        )
        if self.error:
            lines.append(_paint(_clip(f"Monitor error: {self.error}", width), COLORS["failed"], color))
        lines.append("─" * width)

        detail_lines = 8
        table_rows = max(3, height - detail_lines - len(lines) - 2)
        start = 0
        if len(self.tasks) > table_rows:
            start = max(0, min(self.selected - table_rows // 2, len(self.tasks) - table_rows))
        visible = self.tasks[start : start + table_rows]

        if width >= 100:
            fixed = 2 + 21 + 10 + 20 + 8 + 5 + 10 + 7
            progress_width = max(16, width - fixed)
            lines.append(
                f"  {'STATUS':<20} {'ID':<9} {'MODEL':<19} {'EFFORT':<7} "
                f"{'APPR':>4} {'GATES':<9} {'AGE':>5}  PROGRESS"
            )
            for offset, task in enumerate(visible):
                index = start + offset
                status = str(task.get("status") or "unknown")
                marker = "›" if index == self.selected else " "
                status_text = f"{STATUS_ICONS.get(status, '·')} {status.replace('_', ' ')}"
                status_text = f"{status_text:<20}"
                status_text = _paint(status_text, COLORS.get(status, ""), color)
                lines.append(
                    f"{marker} {status_text} "
                    f"{_clip(task.get('id'), 8):<9} "
                    f"{_clip(task.get('model'), 18):<19} "
                    f"{_clip(task.get('effort'), 6):<7} "
                    f"{int(task.get('pending_approvals') or 0):>4} "
                    f"{_verification_label(task):<9} "
                    f"{_age(task.get('updated_at'), now):>5}  "
                    f"{_clip(_progress(task), progress_width)}"
                )
        else:
            lines.append("  STATUS / TASK / WORKER / PROGRESS")
            for offset, task in enumerate(visible):
                index = start + offset
                status = str(task.get("status") or "unknown")
                marker = "›" if index == self.selected else " "
                prefix = (
                    f"{marker} {STATUS_ICONS.get(status, '·')} {status.replace('_', ' ')} "
                    f"{_clip(task.get('id'), 8)} {_clip(task.get('model'), 18)} — "
                )
                lines.append(
                    _paint(prefix, COLORS.get(status, ""), color)
                    + _clip(_progress(task), max(10, width - len(prefix)))
                )

        while len(visible) < table_rows:
            lines.append("")
            visible.append({})

        lines.append("─" * width)
        task = self.selected_task
        if task:
            verification = task.get("verification") or {}
            counts = verification.get("counts") or {}
            usage = self.db.task_usage(str(task["id"]))["totals"]
            repo = Path(str(task.get("worktree_path") or "")).name or "not prepared"
            relations = (
                f"deps {len(task.get('dependencies') or [])} · "
                f"dependents {len(task.get('dependents') or [])}"
            )
            lines.extend(
                [
                    _paint(
                        _clip(
                            f"Selected {task['id']} · {task.get('provider')}/{task.get('model')} "
                            f"· effort {task.get('effort')}",
                            width,
                        ),
                        BOLD,
                        color,
                    ),
                    _clip(
                        f"worktree {repo} · branch {task.get('branch_name') or '—'} · {relations}",
                        width,
                    ),
                    _clip(
                        "verification "
                        + ", ".join(
                            f"{key} {int(value or 0)}" for key, value in counts.items()
                        ),
                        width,
                    ),
                    _clip(
                        f"usage {_token_count(usage.get('total_tokens'))} tokens · "
                        f"input {_token_count(usage.get('input_tokens'))} · "
                        f"cache read {_token_count(usage.get('cache_read_input_tokens'))} · "
                        f"output {_token_count(usage.get('output_tokens'))}",
                        width,
                    ),
                    _clip(f"progress {_progress(task)}", width),
                    _clip(f"result {_clean(task.get('result_summary')) or '—'}", width),
                ]
            )
        else:
            lines.extend(["No tasks match the current filter.", "", "", "", "", ""])
        lines.append(_paint("↑/k ↓/j select   g/G first/last   r refresh   q quit", DIM, color))
        return "\n".join(lines[:height])


@contextmanager
def _interactive_terminal(input_stream: TextIO, output_stream: TextIO) -> Iterator[None]:
    descriptor = input_stream.fileno()
    previous = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)
    output_stream.write("\x1b[?1049h\x1b[?25l")
    output_stream.flush()
    try:
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        output_stream.write("\x1b[?25h\x1b[?1049l")
        output_stream.flush()


def _key(input_stream: TextIO, timeout: float) -> str | None:
    ready, _, _ = select.select([input_stream], [], [], max(0.0, timeout))
    if not ready:
        return None
    raw = os.read(input_stream.fileno(), 8).decode(errors="ignore")
    if raw.endswith("A"):
        return "up"
    if raw.endswith("B"):
        return "down"
    return raw[-1:] or None


def run_monitor(
    db: ForemanDB,
    controller: DaemonController,
    *,
    status: str | None = None,
    interval: float = 1.0,
    limit: int = 100,
    once: bool = False,
    color: bool | None = None,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    monitor = TaskMonitor(db, controller, status_filter=status, limit=limit)
    monitor.refresh()
    interactive = (
        not once
        and input_stream.isatty()
        and output_stream.isatty()
        and os.name == "posix"
    )
    use_color = output_stream.isatty() if color is None else color
    width, height = shutil.get_terminal_size((120, 32))
    if not interactive:
        output_stream.write(monitor.render(width, height, use_color) + "\n")
        output_stream.flush()
        return

    refresh_every = max(0.1, float(interval))
    with _interactive_terminal(input_stream, output_stream):
        next_refresh = 0.0
        while True:
            now = time.monotonic()
            if now >= next_refresh:
                monitor.refresh()
                width, height = shutil.get_terminal_size((120, 32))
                output_stream.write("\x1b[H\x1b[2J")
                output_stream.write(monitor.render(width, height, use_color))
                output_stream.flush()
                next_refresh = now + refresh_every
            try:
                key = _key(input_stream, next_refresh - time.monotonic())
            except KeyboardInterrupt:
                return
            if key in {"q", "\x03"}:
                return
            if key in {"down", "j"}:
                monitor.move(1)
                next_refresh = 0.0
            elif key in {"up", "k"}:
                monitor.move(-1)
                next_refresh = 0.0
            elif key == "g":
                monitor.selected = 0
                next_refresh = 0.0
            elif key == "G":
                monitor.selected = max(0, len(monitor.tasks) - 1)
                next_refresh = 0.0
            elif key == "r":
                next_refresh = 0.0
