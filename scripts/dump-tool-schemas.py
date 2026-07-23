#!/usr/bin/env python
"""Regenerate the MCP schema baseline and packaged discovery fingerprint.

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

The ChatGPT Personal Plugin attestation is intentionally separate and is never updated
here. A changed fingerprint must remain release-blocking until that external consumer is
refreshed and verified explicitly.
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
TOOL_SURFACE_CONTRACT_PATH = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from exomem import tool_surface  # noqa: E402 - src path is inserted above


def _build_server(vault_root: Path, state_root: Path):
    """Build the server exactly as the fidelity test does (deterministic env)."""
    from exomem import server as server_module

    server_module.load_dotenv = lambda *a, **k: None  # never read a real .env
    os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    os.environ["EXOMEM_DISABLE_RELEVANCE_CHECK"] = "1"
    os.environ["EXOMEM_DISABLE_MEDIA_EXTRACTION"] = "1"
    os.environ["EXOMEM_DISABLE_CLIP"] = "1"
    # Tool discovery is independent of lexical serving. Keep the fixture
    # capture free of background sidecar repair so its temporary vault can be
    # closed deterministically on Windows.
    os.environ["EXOMEM_LEXICAL_BACKEND"] = "python"
    os.environ["EXOMEM_DISABLE_FILE_WATCHER"] = "1"
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


def _discovery_contract(mcp) -> dict[str, object]:
    """Hash every client-visible field that can affect connector routing/policy."""
    tools = asyncio.run(mcp.list_tools())
    wires = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    return tool_surface.discovery_contract(wires)


def main() -> None:
    # Windows can retain a short-lived SQLite/file handle after lifecycle
    # shutdown. The fixture capture is already complete at that point, so a
    # delayed best-effort cleanup must not prevent writing deterministic output.
    with tempfile.TemporaryDirectory(
        prefix="exomem-schema-", ignore_cleanup_errors=True
    ) as temp_dir:
        temp_root = Path(temp_dir)
        vault_root = temp_root / "schema_vault"
        shutil.copytree(FIXTURE_VAULT, vault_root)
        try:
            mcp = _build_server(vault_root, temp_root / "writer-lease")
            schemas = _live_schemas(mcp)
            discovery_contract = _discovery_contract(mcp)
        finally:
            # Building discovery starts the normal lease lifecycle. Close its
            # SQLite handle before TemporaryDirectory removes the fixture on
            # Windows; otherwise schema generation can fail after a successful
            # capture with a sharing violation.
            from exomem import writer_lease

            writer_lease.reset_managers_for_tests()
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
    TOOL_SURFACE_CONTRACT_PATH.write_text(
        json.dumps(discovery_contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(schemas)} tool schemas to {FIXTURE_PATH.relative_to(REPO_ROOT)}")
    print(
        "wrote MCP discovery fingerprint "
        f"{discovery_contract['sha256']} to "
        f"{TOOL_SURFACE_CONTRACT_PATH.relative_to(REPO_ROOT)}"
    )
    print(
        "connector attestation is intentionally NOT updated: refresh/recreate and "
        "verify external connectors before updating deploy/chatgpt/personal-plugin-contract.json"
    )


if __name__ == "__main__":
    main()
