#!/usr/bin/env python
"""Regenerate the MCP schema-fidelity baseline (`tests/fixtures/mcp_tool_schemas.json`).

`tests/test_mcp_schema_fidelity.py` pins every MCP tool's `description` + `inputSchema`
byte-for-byte — that JSON IS what Claude sees. Adding, removing, or renaming a command,
or editing a tool docstring, intentionally changes that baseline, and there was no tool
to refresh it. Run this after such a change, then review the diff (it should contain only
your intended addition/edit):

    PYTHONPATH=src python scripts/dump-tool-schemas.py

It builds the server under the SAME env the test captures the fixture with
(embeddings/media/CLIP off, tier-2 on, dotenv neutralized, vault = tests/fixtures) so the
live schemas are deterministic, and writes them in the shape the test reads. It mirrors
`tests/test_mcp_schema_fidelity.py::_build_server` / `_live_schemas` — keep them in sync.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


def _build_server(vault_root: Path, state_root: Path):
    """Build the server exactly as the fidelity test does (deterministic env)."""
    from exomem import server as server_module

    server_module.load_dotenv = lambda *a, **k: None  # never read a real .env
    os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    os.environ["EXOMEM_DISABLE_RELEVANCE_CHECK"] = "1"
    os.environ["EXOMEM_DISABLE_MEDIA_EXTRACTION"] = "1"
    os.environ["EXOMEM_DISABLE_CLIP"] = "1"
    os.environ.pop("EXOMEM_DISABLE_TIER2", None)  # tier-2 ON
    os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(state_root)
    os.environ["EXOMEM_VAULT_PATH"] = str(vault_root)
    return server_module.build_server(require_auth=False)


def _live_schemas(mcp) -> dict[str, dict]:
    """The wire-level {name: {description, inputSchema}} for every registered tool."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict] = {}
    for t in tools:
        mt = t.to_mcp_tool().model_dump(mode="json")
        out[t.name] = {"description": mt["description"], "inputSchema": mt["inputSchema"]}
    return out


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="exomem-schema-") as temp_dir:
        temp_root = Path(temp_dir)
        vault_root = temp_root / "schema_vault"
        shutil.copytree(FIXTURE_VAULT, vault_root)
        schemas = _live_schemas(_build_server(vault_root, temp_root / "writer-lease"))
    # Preserve the established coordination-first baseline, then store product tool
    # keys alphabetically. Nested inputSchema property order is signature-order and
    # load-bearing, so only the outer mapping is normalized.
    coordination = schemas.pop("coordination_status", None)
    schemas = dict(sorted(schemas.items()))
    if coordination is not None:
        schemas = {"coordination_status": coordination, **schemas}
    FIXTURE_PATH.write_text(
        json.dumps(schemas, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",  # keep the committed fixture LF even when run on Windows
    )
    print(f"wrote {len(schemas)} tool schemas to {FIXTURE_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
