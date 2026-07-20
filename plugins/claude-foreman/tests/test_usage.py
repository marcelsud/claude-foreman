from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_foreman.database import ForemanDB
from claude_foreman.usage import record_claude_result_usage, record_codex_usage


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = ForemanDB(Path(self.temp.name) / "state.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claude_and_codex_usage_are_normalized(self) -> None:
        claude = self.db.create_task(repo_path=self.temp.name, prompt="work")
        claude_run = self.db.create_run(claude.id)
        record_claude_result_usage(
            self.db,
            claude,
            claude_run,
            SimpleNamespace(
                model_usage={
                    "sonnet": {
                        "inputTokens": 10,
                        "cacheCreationInputTokens": 20,
                        "cacheReadInputTokens": 30,
                        "outputTokens": 4,
                        "costUSD": 0.02,
                    }
                },
                usage=None,
                duration_ms=1500,
                total_cost_usd=0.02,
            ),
        )
        codex = self.db.create_task(
            repo_path=self.temp.name, prompt="work", provider="codex"
        )
        codex_run = self.db.create_run(codex.id)
        record_codex_usage(
            self.db,
            codex,
            codex_run,
            {
                "total": {
                    "inputTokens": 100,
                    "cachedInputTokens": 70,
                    "cacheWriteInputTokens": 5,
                    "outputTokens": 8,
                    "reasoningOutputTokens": 3,
                    "totalTokens": 111,
                }
            },
        )

        claude_totals = self.db.task_usage(claude.id)["totals"]
        codex_totals = self.db.task_usage(codex.id)["totals"]
        self.assertEqual(30, claude_totals["cache_read_input_tokens"])
        self.assertEqual(64, claude_totals["total_tokens"])
        self.assertEqual(70, codex_totals["cache_read_input_tokens"])
        self.assertEqual(5, codex_totals["cache_creation_input_tokens"])
        self.assertEqual(25, codex_totals["input_tokens"])
        self.assertEqual(111, codex_totals["total_tokens"])
        self.assertIsNone(codex_totals["api_equivalent_cost_usd"])
        self.assertEqual("unavailable_for_subscription", codex_totals["cost_kind"])


if __name__ == "__main__":
    unittest.main()
