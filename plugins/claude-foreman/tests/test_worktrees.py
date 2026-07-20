from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_foreman.config import ForemanConfig
from claude_foreman.database import ForemanDB
from claude_foreman.worktrees import WorktreeManager


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class WorktreeTests(unittest.TestCase):
    def test_creates_and_reuses_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            run("git", "init", "-q", cwd=repo)
            run("git", "config", "user.email", "test@example.com", cwd=repo)
            run("git", "config", "user.name", "Test", cwd=repo)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            run("git", "add", "README.md", cwd=repo)
            run("git", "commit", "-qm", "initial", cwd=repo)

            data = root / "data"
            config = ForemanConfig(
                data_dir=data,
                db_path=data / "state.db",
                worktrees_dir=data / "worktrees",
                logs_dir=data / "logs",
                pid_path=data / "foremand.pid",
            )
            config.ensure_directories()
            db = ForemanDB(config.db_path)
            db.initialize()
            task = db.create_task(repo_path=str(repo), prompt="Add greeting")
            manager = WorktreeManager(config)
            first = manager.ensure(task)
            db.update_task(task.id, branch_name=first.branch, worktree_path=str(first.path))
            second = manager.ensure(db.get_task(task.id))

            self.assertEqual(first.path, second.path)
            self.assertNotEqual(repo.resolve(), first.path)
            self.assertTrue((first.path / "README.md").exists())
            self.assertTrue(first.branch.startswith("foreman/"))

    def test_reviewed_workflow_phases_reuse_the_same_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            run("git", "init", "-q", cwd=repo)
            run("git", "config", "user.email", "test@example.com", cwd=repo)
            run("git", "config", "user.name", "Test", cwd=repo)
            run("git", "commit", "--allow-empty", "-qm", "initial", cwd=repo)
            data = root / "data"
            config = ForemanConfig(
                data_dir=data,
                db_path=data / "state.db",
                worktrees_dir=data / "worktrees",
                logs_dir=data / "logs",
                pid_path=data / "foremand.pid",
            )
            db = ForemanDB(config.db_path)
            db.initialize()
            workflow = db.propose_workflow(
                "two-phase",
                {
                    "tasks": [
                        {"key": "implement", "prompt": "implement"},
                        {"key": "verify", "prompt": "verify", "depends_on": ["implement"]},
                    ]
                },
            )
            db.review_workflow("two-phase", workflow["version"], approve=True, actor="codex")
            compiled = db.run_workflow(name="two-phase", repo_path=str(repo))
            manager = WorktreeManager(config)

            first_task = db.claim_next_task()
            first_worktree = manager.ensure(first_task)
            db.update_task(
                first_task.id,
                status="awaiting_review",
                worktree_path=str(first_worktree.path),
                branch_name=first_worktree.branch,
            )
            db.accept_task(first_task.id)
            second_task = db.claim_next_task()
            shared = db.workspace_for_group(second_task.workspace_group)
            second_worktree = manager.reuse(second_task, shared[0], shared[1])

            self.assertEqual(compiled["tasks"]["verify"]["id"], second_task.id)
            self.assertEqual(first_worktree.path, second_worktree.path)


if __name__ == "__main__":
    unittest.main()
