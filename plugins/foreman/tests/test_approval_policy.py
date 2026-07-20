from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foreman.approval_policy import auto_allow, classify_risk, human_only


class ApprovalPolicyTests(unittest.TestCase):
    def test_routine_sandboxed_work_is_auto_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bash_input = {"command": "python -m unittest"}
            risk = classify_risk("Bash", bash_input, root)
            self.assertEqual("medium", risk)
            self.assertTrue(auto_allow("Bash", bash_input, root, risk))

            arbitrary = {"command": "python -c 'print(1)'"}
            risk = classify_risk("Bash", arbitrary, root)
            self.assertFalse(auto_allow("Bash", arbitrary, root, risk))

            edit_input = {"file_path": "src/example.py"}
            risk = classify_risk("Edit", edit_input, root)
            self.assertTrue(auto_allow("Edit", edit_input, root, risk))

    def test_publication_and_outside_edits_require_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            push = {"command": "git push origin main"}
            risk = classify_risk("Bash", push, root)
            self.assertEqual("high", risk)
            self.assertFalse(auto_allow("Bash", push, root, risk))

            outside = {"file_path": str(root.parent / "outside.txt")}
            risk = classify_risk("Edit", outside, root)
            self.assertEqual("high", risk)
            self.assertFalse(auto_allow("Edit", outside, root, risk))

            multiple = {
                "paths": [str(root / "inside.txt"), str(root.parent / "outside.txt")]
            }
            risk = classify_risk("Edit", multiple, root)
            self.assertEqual("high", risk)
            self.assertFalse(auto_allow("Edit", multiple, root, risk))

            grant_root = {
                "paths": [str(root / "inside.txt")],
                "grant_root": str(root.parent),
            }
            self.assertEqual("high", classify_risk("Edit", grant_root, root))

            credentials = {"file_path": "~/.claude/.credentials.json"}
            risk = classify_risk("Read", credentials, root)
            self.assertEqual("high", risk)
            self.assertTrue(human_only(risk, "Read", credentials))

            multiple_with_credentials = {
                "paths": [str(root / "inside.txt"), "~/.codex/auth.json"]
            }
            self.assertTrue(human_only("high", "Edit", multiple_with_credentials))

            shell_credentials = {"command": "cat ~/.claude/.credentials.json"}
            risk = classify_risk("Bash", shell_credentials, root)
            self.assertEqual("high", risk)
            self.assertTrue(human_only(risk, "Bash", shell_credentials))


if __name__ == "__main__":
    unittest.main()
