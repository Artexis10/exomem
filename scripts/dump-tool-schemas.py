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
here. A changed fingerprint leaves that external consumer's acceptance pending until
refreshed and verified; it does not block independently verified product surfaces.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"
CLOUD_ONLY_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_cloud_only_tool_schemas.json"
TOOL_SURFACE_CONTRACT_PATH = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from exomem import tool_surface  # noqa: E402 - src path is inserted above


def _build_server(vault_root: Path, state_root: Path, *, cloud: bool = False):
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
    if cloud:
        os.environ["EXOMEM_CLOUD_CELL"] = "1"
        os.environ["EXOMEM_CLOUD_CELL_ID"] = "schema-fixture"
        os.environ["EXOMEM_CLOUD_CELL_TOKEN"] = "x" * 40
    else:
        os.environ.pop("EXOMEM_CLOUD_CELL", None)
    return server_module.build_server(require_auth=False)


def _live_schemas(mcp) -> dict[str, dict]:
    """The wire-level {name: {description, inputSchema}} for every registered tool."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict] = {}
    for t in tools:
        mt = t.to_mcp_tool().model_dump(mode="json", by_alias=True)
        out[t.name] = {"description": mt["description"], "inputSchema": mt["inputSchema"]}
    return out


def _cloud_only_schemas(mcp) -> dict[str, dict]:
    """The Cloud-only control-plane tools, with the annotations a client reads.

    These tools are absent from every other surface, so the baseline above cannot hold
    them; the Cloud gateway lane syncs its tool names and action enums from this file.
    """
    from exomem import commands as commands_module

    names = {c.public_name for c in commands_module.PRODUCT_COMMANDS if c.cloud_only}
    out: dict[str, dict] = {}
    for tool in asyncio.run(mcp.list_tools()):
        if tool.name in names:
            wire = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
            out[tool.name] = {
                "description": wire["description"],
                "inputSchema": wire["inputSchema"],
                "annotations": wire["annotations"],
            }
    return dict(sorted(out.items()))


def _discovery_contract(mcp) -> dict[str, object]:
    """Hash every client-visible field that can affect connector routing/policy."""
    tools = asyncio.run(mcp.list_tools())
    wires = [tool.to_mcp_tool().model_dump(mode="json", by_alias=True) for tool in tools]
    return tool_surface.discovery_contract(wires)


def main() -> None:
    # Windows can retain a short-lived SQLite/file handle after lifecycle
    # shutdown, and the fixture capture is already complete at that point, so a
    # delayed cleanup must not prevent writing deterministic output. That used
    # to be spelled `ignore_cleanup_errors=True`, which does not wait for the
    # handle -- it gives up on the first error and says nothing, leaving a
    # quarter-gigabyte tree behind every run (#579).
    with scratch_root.scratch_root("exomem-schema-") as temp_root:
        vault_root = temp_root / "schema_vault"
        shutil.copytree(FIXTURE_VAULT, vault_root)
        try:
            mcp = _build_server(vault_root, temp_root / "writer-lease")
            schemas = _live_schemas(mcp)
            discovery_contract = _discovery_contract(mcp)
            from exomem import writer_lease

            writer_lease.reset_managers_for_tests()
            cloud_only = _cloud_only_schemas(
                _build_server(vault_root, temp_root / "cloud-writer-lease", cloud=True)
            )
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
    CLOUD_ONLY_FIXTURE_PATH.write_text(
        json.dumps(cloud_only, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    TOOL_SURFACE_CONTRACT_PATH.write_text(
        json.dumps(discovery_contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(schemas)} tool schemas to {FIXTURE_PATH.relative_to(REPO_ROOT)}")
    print(f"wrote {len(cloud_only)} Cloud-only tool schemas to {CLOUD_ONLY_FIXTURE_PATH.relative_to(REPO_ROOT)}")
    print(
        "wrote MCP discovery fingerprint "
        f"{discovery_contract['sha256']} to "
        f"{TOOL_SURFACE_CONTRACT_PATH.relative_to(REPO_ROOT)}"
    )
    print(
        "connector rollout metadata is not updated here. Before release, record this "
        "digest as pending in deploy/chatgpt/personal-plugin-contract.json. After "
        "deployment and a verified connector refresh/recreation, promote only that "
        "verified digest to registered acceptance."
    )


if __name__ == "__main__":
    main()
