#!/usr/bin/env python
"""Measure what the MCP tool surface costs a client that resends it every turn.

Builds the server under the same deterministic environment as
`scripts/dump-tool-schemas.py`, then sizes every registered tool's wire object
(name, title, description, inputSchema, outputSchema, annotations, _meta) as
compact UTF-8 JSON, split into the parts a description author controls:

    PYTHONPATH=src python scripts/measure-tool-schema-bytes.py [--json]

Columns: total, tool description, input schema, of which parameter descriptions,
output schema. `TOTAL` is the number a schema-size budget pins.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import scratch_root  # noqa: E402


def _load_dumper():
    spec = importlib.util.spec_from_file_location("dump_tool_schemas", HERE / "dump-tool-schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def size(value: object) -> int:
    """Compact UTF-8 JSON byte length: what a client receives, minus framing."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _description_bytes(node: object) -> Iterator[int]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield len(value.encode("utf-8"))
            else:
                yield from _description_bytes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _description_bytes(value)


def wire_tools() -> list[dict]:
    dumper = _load_dumper()
    with scratch_root.scratch_root("exomem-measure-") as temp_root:
        vault_root = temp_root / "schema_vault"
        shutil.copytree(dumper.FIXTURE_VAULT, vault_root)
        try:
            mcp = dumper._build_server(vault_root, temp_root / "writer-lease")
            tools = asyncio.run(mcp.list_tools())
            return [
                tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in tools
            ]
        finally:
            from exomem import writer_lease

            writer_lease.reset_managers_for_tests()


def measure(wires: list[dict]) -> list[dict]:
    rows = []
    for wire in wires:
        input_schema = wire.get("inputSchema", {})
        rows.append(
            {
                "name": wire["name"],
                "total": size(wire),
                "description": len(wire.get("description", "").encode("utf-8")),
                "input": size(input_schema),
                "input_descriptions": sum(_description_bytes(input_schema)),
                "output": size(wire["outputSchema"]) if wire.get("outputSchema") else 0,
            }
        )
    return sorted(rows, key=lambda row: (-row["total"], row["name"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit machine-readable rows")
    args = parser.parse_args()
    rows = measure(wire_tools())
    total = sum(row["total"] for row in rows)
    if args.json:
        print(json.dumps({"total": total, "tools": rows}, indent=2))
        return
    columns = ("total", "description", "input", "input_descriptions", "output")
    print(f"{'tool':<26}" + "".join(f"{c:>20}" for c in columns))
    for row in rows:
        print(f"{row['name']:<26}" + "".join(f"{row[c]:>20,}" for c in columns))
    print(f"{'TOTAL':<26}{total:>20,}" + "".join(f"{sum(r[c] for r in rows):>20,}" for c in columns[1:]))


if __name__ == "__main__":
    main()
