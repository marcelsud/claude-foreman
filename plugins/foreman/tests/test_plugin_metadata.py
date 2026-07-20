from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

from foreman import __version__


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class DualPluginMetadataTests(unittest.TestCase):
    def test_manifests_share_identity_version_and_repository(self) -> None:
        codex = read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
        claude = read_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
        project = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("foreman", codex["name"])
        self.assertEqual(codex["name"], claude["name"])
        self.assertEqual(codex["version"], claude["version"])
        self.assertEqual(codex["version"], project["project"]["version"])
        self.assertEqual(codex["version"], __version__)
        self.assertEqual(
            "https://github.com/marcelsud/claude-foreman",
            codex["repository"],
        )
        self.assertEqual(codex["repository"], claude["repository"])

    def test_each_host_uses_its_supported_plugin_root_convention(self) -> None:
        codex_mcp = read_json(PLUGIN_ROOT / ".mcp.json")
        claude = read_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")

        codex_server = codex_mcp["mcpServers"]["foreman"]
        claude_server = claude["mcpServers"]["foreman"]
        self.assertEqual("python3", codex_server["command"])
        self.assertEqual(["./scripts/foreman_mcp.py"], codex_server["args"])
        self.assertEqual(".", codex_server["cwd"])
        self.assertEqual("python3", claude_server["command"])
        self.assertEqual(
            ["${CLAUDE_PLUGIN_ROOT}/scripts/foreman_mcp.py"],
            claude_server["args"],
        )

    def test_claude_marketplace_points_to_the_shared_plugin(self) -> None:
        marketplace = read_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")

        self.assertEqual("foreman", marketplace["name"])
        self.assertEqual(1, len(marketplace["plugins"]))
        entry = marketplace["plugins"][0]
        self.assertEqual("foreman", entry["name"])
        self.assertEqual("./plugins/foreman", entry["source"])


if __name__ == "__main__":
    unittest.main()
