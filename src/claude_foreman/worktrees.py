from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ForemanConfig
from .models import Task


class GitError(RuntimeError):
    pass


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

    def snapshot(self, worktree: str | Path) -> dict[str, str]:
        path = Path(worktree).resolve()
        return {
            "status": _git(path, "status", "--short", "--branch").stdout,
            "diff_stat": _git(path, "diff", "HEAD", "--stat").stdout,
            "untracked": _git(path, "ls-files", "--others", "--exclude-standard").stdout,
            "commits": _git(path, "log", "--oneline", "--decorate", "-10").stdout,
        }
