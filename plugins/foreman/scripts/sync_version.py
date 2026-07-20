#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
VERSION_SOURCE = PLUGIN_ROOT / "src" / "foreman" / "__init__.py"
VERSION_PATTERN = re.compile(r'("version"\s*:\s*")([^"]+)(")')
MANIFESTS = (
    PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
    PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
    REPO_ROOT / ".claude-plugin" / "marketplace.json",
)


def canonical_version() -> str:
    tree = ast.parse(VERSION_SOURCE.read_text(encoding="utf-8"), filename=str(VERSION_SOURCE))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value
        ):
            return node.value.value
    raise RuntimeError(f"{VERSION_SOURCE} must define a non-empty string __version__")


def manifest_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    json.loads(text)
    matches = list(VERSION_PATTERN.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"{path} must contain exactly one JSON version field")
    return matches[0].group(2)


def sync_manifest(path: Path, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    updated, count = VERSION_PATTERN.subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count != 1:
        raise RuntimeError(f"{path} must contain exactly one JSON version field")
    json.loads(updated)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize generated plugin versions with foreman.__version__."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when generated metadata is out of sync",
    )
    args = parser.parse_args()
    version = canonical_version()
    mismatches = [path for path in MANIFESTS if manifest_version(path) != version]
    if args.check:
        if mismatches:
            rendered = ", ".join(str(path.relative_to(REPO_ROOT)) for path in mismatches)
            raise SystemExit(
                f"version metadata is out of sync with {version}: {rendered}; "
                "run python scripts/sync_version.py"
            )
        print(f"Version metadata is synchronized at {version}")
        return
    for path in mismatches:
        sync_manifest(path, version)
        print(f"Updated {path.relative_to(REPO_ROOT)} to {version}")
    if not mismatches:
        print(f"Version metadata already synchronized at {version}")


if __name__ == "__main__":
    main()
