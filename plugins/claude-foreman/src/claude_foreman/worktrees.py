from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ForemanConfig
from .models import Task


class GitError(RuntimeError):
    pass


SANDBOX_ARTIFACT_PATHS = {
    ".claude.json",
    ".claude/settings.local.json",
    "dev/null",
    "dev/random",
    "dev/tty",
    "dev/urandom",
}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise GitError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def repository_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists():
        raise GitError(f"repository path does not exist: {candidate}")
    result = _git(candidate, "rev-parse", "--show-toplevel")
    root = Path(result.stdout.strip()).resolve()
    _git(root, "rev-parse", "--verify", "HEAD")
    return root


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (value or "task")[:32]


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str
    repo_root: Path


class WorktreeManager:
    def __init__(self, config: ForemanConfig):
        self.config = config

    def ensure(self, task: Task) -> Worktree:
        root = repository_root(task.repo_path)
        repo_key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        target = (self.config.worktrees_dir / repo_key / task.id).resolve()
        branch = task.branch_name or f"foreman/{task.id[:8]}-{_slug(task.prompt)}"

        if target.exists():
            registered = _git(root, "worktree", "list", "--porcelain").stdout
            if f"worktree {target}" not in registered:
                raise GitError(f"worktree path exists but is not registered by Git: {target}")
            return Worktree(path=target, branch=branch, repo_root=root)

        target.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
        if branch_exists:
            _git(root, "worktree", "add", str(target), branch)
        else:
            _git(root, "worktree", "add", "-b", branch, str(target), task.base_ref)
        return Worktree(path=target, branch=branch, repo_root=root)

    def reuse(self, task: Task, worktree_path: str, branch: str) -> Worktree:
        root = repository_root(task.repo_path)
        path = Path(worktree_path).expanduser().resolve()
        if not path.is_dir():
            raise GitError(f"shared workflow worktree no longer exists: {path}")
        registered = _git(root, "worktree", "list", "--porcelain").stdout
        if f"worktree {path}" not in registered:
            raise GitError(f"shared workflow worktree is not registered by Git: {path}")
        actual_branch = _git(path, "branch", "--show-current").stdout.strip()
        if actual_branch != branch:
            raise GitError(
                f"shared workflow branch changed: expected {branch}, found {actual_branch or 'detached HEAD'}"
            )
        return Worktree(path=path, branch=branch, repo_root=root)

    @staticmethod
    def _is_sandbox_artifact(root: Path, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/").strip("/")
        if normalized in SANDBOX_ARTIFACT_PATHS:
            return True
        candidate = root / relative_path
        try:
            return candidate.exists() and not (
                candidate.is_file() or candidate.is_dir() or candidate.is_symlink()
            )
        except OSError:
            return True

    def status(self, worktree: str | Path) -> dict[str, object]:
        path = Path(worktree).resolve()
        branch = _git(path, "branch", "--show-current").stdout.strip()
        raw = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        entries = [entry for entry in raw.split("\0") if entry]
        intended: list[str] = []
        artifacts: list[str] = []
        intended_untracked: list[str] = []
        skip_rename_target = False
        for entry in entries:
            if skip_rename_target:
                skip_rename_target = False
                continue
            if len(entry) < 4:
                continue
            code = entry[:2]
            relative = entry[3:]
            if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
                skip_rename_target = True
            if code == "??" and self._is_sandbox_artifact(path, relative):
                artifacts.append(relative)
                continue
            intended.append(entry)
            if code == "??":
                intended_untracked.append(relative)
        prefix = f"## {branch or '(detached)'}\n"
        return {
            "raw_status": prefix + "\n".join(entries) + ("\n" if entries else ""),
            "intended_status": prefix + "\n".join(intended) + ("\n" if intended else ""),
            "sandbox_artifacts": artifacts,
            "intended_untracked": intended_untracked,
        }

    @staticmethod
    def _untracked_stats(path: Path, relative: str) -> dict[str, object]:
        candidate = path / relative
        size = candidate.stat().st_size
        added = 0
        binary = False
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if b"\0" in chunk:
                    binary = True
                added += chunk.count(b"\n")
        if size and not binary:
            with candidate.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    added += 1
        return {
            "path": relative,
            "added": None if binary else added,
            "deleted": 0,
            "binary": binary,
            "untracked": True,
            "size_bytes": size,
        }

    def snapshot(self, worktree: str | Path) -> dict[str, object]:
        path = Path(worktree).resolve()
        status = self.status(path)
        diff_files: list[dict[str, object]] = []
        numstat = _git(path, "diff", "HEAD", "--numstat", "--").stdout
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, relative = parts
            candidate = path / relative
            diff_files.append(
                {
                    "path": relative,
                    "added": int(added) if added.isdigit() else None,
                    "deleted": int(deleted) if deleted.isdigit() else None,
                    "binary": added == "-" or deleted == "-",
                    "untracked": False,
                    "size_bytes": candidate.stat().st_size if candidate.is_file() else 0,
                }
            )
        for relative in status["intended_untracked"]:
            candidate = path / str(relative)
            if candidate.is_file() and not candidate.is_symlink():
                diff_files.append(self._untracked_stats(path, str(relative)))
        stat_lines = []
        for item in diff_files:
            change = (
                f"binary {item['size_bytes']} bytes"
                if item["binary"] else f"+{item['added']} -{item['deleted']}"
            )
            suffix = " (new)" if item["untracked"] else ""
            stat_lines.append(f"{item['path']} | {change}{suffix}")
        return {
            **status,
            "status": status["intended_status"],
            "diff_stat": "\n".join(stat_lines) + ("\n" if stat_lines else ""),
            "diff_files": diff_files,
            "untracked": "\n".join(str(item) for item in status["intended_untracked"])
            + ("\n" if status["intended_untracked"] else ""),
            "commits": _git(path, "log", "--oneline", "--decorate", "-10").stdout,
        }
