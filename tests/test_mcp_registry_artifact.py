"""Static checks for the published MCP Registry artifact."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_server_description_obeys_registry_length_contract() -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert 1 <= len(manifest["description"]) <= 100
