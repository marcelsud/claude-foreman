from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_foreman.approval_policy import request_hash
from claude_foreman.database import ForemanDB
from claude_foreman.models import TaskStatus


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = ForemanDB(Path(self.temp.name) / "state.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_goal_task_dependency_and_claim(self) -> None:
        goal = self.db.create_goal("Ship feature", "Implement and verify")
        first = self.db.create_task(repo_path=self.temp.name, prompt="first", goal_id=goal.id)
        second = self.db.create_task(
            repo_path=self.temp.name,
            prompt="second",
            goal_id=goal.id,
            depends_on=[first.id],
            priority=50,
        )
        claimed = self.db.claim_next_task()
        self.assertEqual(first.id, claimed.id)
        self.assertIsNone(self.db.claim_next_task())
        self.db.update_task(first.id, status=TaskStatus.COMPLETED)
        self.assertEqual(second.id, self.db.claim_next_task().id)

    def test_queued_task_model_and_effort_can_be_reconfigured(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        configured = self.db.configure_queued_task(
            task.id,
            model="opus",
            effort="xhigh",
            priority=42,
            max_turns=120,
        )
        self.assertEqual("opus", configured.model)
        self.assertEqual("xhigh", configured.effort)
        self.assertEqual(42, configured.priority)
        self.assertEqual(120, configured.max_turns)
        self.db.claim_next_task()
        with self.assertRaises(ValueError):
            self.db.configure_queued_task(task.id, model="sonnet")

    def test_codex_models_infer_provider_and_validate_effort(self) -> None:
        sol = self.db.create_task(
            repo_path=self.temp.name,
            prompt="deep work",
            model="gpt-5.6-sol",
            effort="ultra",
        )
        self.assertEqual("codex", sol.provider)
        self.assertEqual("gpt-5.6-sol", sol.model)
        switched = self.db.configure_queued_task(sol.id, provider="claude")
        self.assertEqual("claude", switched.provider)
        self.assertEqual("sonnet", switched.model)
        self.assertEqual("medium", switched.effort)
        with self.assertRaises(ValueError):
            self.db.create_task(
                repo_path=self.temp.name,
                prompt="too much",
                model="gpt-5.6-luna",
                effort="ultra",
            )
        with self.assertRaises(ValueError):
            self.db.create_task(
                repo_path=self.temp.name,
                prompt="mismatch",
                provider="claude",
                model="gpt-5.6-sol",
            )

    def test_codex_provider_defaults_to_sol(self) -> None:
        task = self.db.create_task(
            repo_path=self.temp.name, prompt="do work", provider="codex"
        )
        self.assertEqual("codex", task.provider)
        self.assertEqual("gpt-5.6-sol", task.model)

    def test_usage_aggregates_runs_models_and_goal(self) -> None:
        goal = self.db.create_goal("Measure work")
        task = self.db.create_task(
            repo_path=self.temp.name, prompt="work", goal_id=goal.id
        )
        first_run = self.db.create_run(task.id)
        self.db.upsert_run_usage(
            run_id=first_run, task_id=task.id, provider="claude", model="sonnet",
            input_tokens=10, cache_creation_input_tokens=20,
            cache_read_input_tokens=30, output_tokens=5, total_tokens=65,
            duration_ms=1000, api_equivalent_cost_usd=0.01,
        )
        second_run = self.db.create_run(task.id)
        self.db.upsert_run_usage(
            run_id=second_run, task_id=task.id, provider="claude", model="sonnet",
            input_tokens=4, cache_read_input_tokens=6, output_tokens=2,
            total_tokens=12, duration_ms=500, api_equivalent_cost_usd=0.002,
        )

        usage = self.db.task_usage(task.id)

        self.assertEqual(2, len(usage["runs"]))
        self.assertEqual(77, usage["totals"]["total_tokens"])
        self.assertEqual(36, usage["totals"]["cache_read_input_tokens"])
        self.assertEqual(0.012, usage["totals"]["api_equivalent_cost_usd"])
        self.assertEqual(77, self.db.goal_usage(goal.id)["totals"]["total_tokens"])
        self.assertTrue(usage["totals"]["cost_is_estimate"])

    def test_task_list_is_compact_by_default(self) -> None:
        task = self.db.create_task(
            repo_path=self.temp.name,
            prompt="large prompt " * 1000,
            verification_commands=["git status"],
        )
        compact = self.db.list_tasks()
        full = self.db.list_tasks(compact=False)

        self.assertNotIn("prompt", compact[0])
        self.assertEqual(task.id, compact[0]["id"])
        self.assertEqual(1, compact[0]["verification"]["counts"]["pending"])
        self.assertIn("prompt", full[0])

    def test_verification_commands_are_validated_and_reconfigurable(self) -> None:
        task = self.db.create_task(
            repo_path=self.temp.name,
            prompt="work",
            verification_commands=["python3 -m unittest", "git status"],
        )
        self.assertEqual(
            ["python3 -m unittest", "git status"],
            [gate["command"] for gate in self.db.verification_gates(task.id)],
        )
        self.db.configure_queued_task(
            task.id, verification_commands=["pytest tests"]
        )
        self.assertEqual(
            ["pytest tests"],
            [gate["command"] for gate in self.db.verification_gates(task.id)],
        )
        with self.assertRaises(ValueError):
            self.db.create_task(
                repo_path=self.temp.name,
                prompt="unsafe",
                verification_commands=["npm test; curl example.com"],
            )

    def test_approval_is_hash_bound_and_single_use(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        payload = {"command": "npm test"}
        digest = request_hash(task.id, "Bash", payload)
        approval = self.db.create_approval(
            task_id=task.id,
            run_id=None,
            tool_name="Bash",
            input_data=payload,
            request_hash=digest,
            risk="medium",
            timeout_seconds=60,
        )
        with self.assertRaises(ValueError):
            self.db.decide_approval(
                approval["id"], approve=True, decided_by="codex", request_hash="wrong"
            )
        decided = self.db.decide_approval(
            approval["id"], approve=True, decided_by="codex", request_hash=digest
        )
        self.assertEqual("approved", decided["status"])
        with self.assertRaises(ValueError):
            self.db.decide_approval(
                approval["id"], approve=False, decided_by="codex", request_hash=digest
            )

    def test_approval_expiration_is_enforced_while_worker_polls(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        approval = self.db.create_approval(
            task_id=task.id,
            run_id=None,
            tool_name="Bash",
            input_data={"command": "custom-tool"},
            request_hash="digest",
            risk="medium",
            timeout_seconds=-1,
        )
        expired = self.db.get_approval(approval["id"])
        self.assertEqual("expired", expired["status"])
        self.assertIn("approval.expired", [event["kind"] for event in self.db.events(task.id)])

    def test_recovery_requeues_active_tasks_and_closes_stale_approvals(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        self.db.update_task(task.id, status=TaskStatus.RUNNING)
        run_id = self.db.create_run(task.id)
        approval = self.db.create_approval(
            task_id=task.id,
            run_id=run_id,
            tool_name="Bash",
            input_data={"command": "custom-tool"},
            request_hash="digest",
            risk="medium",
            timeout_seconds=60,
        )

        recovered = self.db.recover_interrupted_tasks()

        self.assertEqual([task.id], recovered)
        self.assertEqual(TaskStatus.QUEUED, self.db.get_task(task.id).status)
        self.assertEqual("rejected", self.db.get_approval(approval["id"])["status"])

    def test_review_requeue_and_accept(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        self.db.update_task(task.id, status=TaskStatus.AWAITING_REVIEW)
        requeued = self.db.requeue_task(task.id, "add a regression test")
        self.assertEqual(TaskStatus.QUEUED, requeued.status)
        self.assertEqual("add a regression test", self.db.latest_review_feedback(task.id))
        self.db.update_task(task.id, status=TaskStatus.AWAITING_REVIEW)
        accepted = self.db.accept_task(task.id)
        self.assertEqual(TaskStatus.COMPLETED, accepted.status)

    def test_event_tail_returns_the_most_recent_events_in_order(self) -> None:
        task = self.db.create_task(repo_path=self.temp.name, prompt="do work")
        for number in range(30):
            self.db.add_event(task.id, None, "progress", {"number": number})
        tail = self.db.event_tail(task.id, 5)
        self.assertEqual([25, 26, 27, 28, 29], [item["payload"]["number"] for item in tail])

    def test_reviewed_workflow_compiles_dependencies(self) -> None:
        definition = {
            "description": "two phases",
            "tasks": [
                {"key": "implement", "prompt": "Implement ${thing}"},
                {"key": "verify", "prompt": "Verify ${thing}", "depends_on": ["implement"]},
            ],
        }
        workflow = self.db.propose_workflow("two-phase", definition)
        with self.assertRaises(ValueError):
            self.db.run_workflow(name="two-phase", repo_path=self.temp.name, inputs={"thing": "x"})
        self.db.review_workflow("two-phase", workflow["version"], approve=True, actor="codex")
        run = self.db.run_workflow(
            name="two-phase", repo_path=self.temp.name, inputs={"thing": "auth"}
        )
        self.assertEqual("Implement auth", run["tasks"]["implement"]["prompt"])
        self.assertEqual(
            run["tasks"]["implement"]["workspace_group"],
            run["tasks"]["verify"]["workspace_group"],
        )
        first = self.db.claim_next_task()
        self.assertEqual(run["tasks"]["implement"]["id"], first.id)
        self.assertIsNone(self.db.claim_next_task())

    def test_workflow_rejects_fanout_that_would_mix_worktree_history(self) -> None:
        with self.assertRaises(ValueError):
            self.db.propose_workflow(
                "fanout",
                {
                    "tasks": [
                        {"key": "root", "prompt": "root"},
                        {"key": "left", "prompt": "left", "depends_on": ["root"]},
                        {"key": "right", "prompt": "right", "depends_on": ["root"]},
                    ]
                },
            )

    def test_workflow_validates_codex_model_before_review(self) -> None:
        with self.assertRaises(ValueError):
            self.db.propose_workflow(
                "bad-codex",
                {
                    "tasks": [
                        {
                            "key": "implement",
                            "prompt": "work",
                            "provider": "codex",
                            "model": "gpt-5.6-unknown",
                        }
                    ]
                },
            )


if __name__ == "__main__":
    unittest.main()
