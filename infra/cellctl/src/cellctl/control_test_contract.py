"""Shared SQL setup for disposable controller and infrastructure checks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def control_test_sql(fixtures: Path) -> tuple[str, str]:
    """Load the baseline and pinned companion migration/grants without fetching."""
    contract = json.loads((fixtures / "substrate-contract.json").read_text(encoding="utf-8"))
    checkout = os.environ["CELLCTL_SUBSTRATE_CONTRACT_ROOT"]

    def committed_sql(key: str) -> str:
        # Committed blobs keep dirty or newer checkout files outside the fixture contract.
        return subprocess.run(
            ["git", "-C", checkout, "show", f"{contract['ref']}:{contract[key]}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    schema = (fixtures / "exomem_cloud_schema.sql").read_text(encoding="utf-8")
    # Earlier migrations own entitlements; the companion migration extends this minimal stand-in.
    schema += "\nCREATE TABLE exomem_entitlements (tenant_id uuid PRIMARY KEY);\n"
    return schema + committed_sql("migration"), committed_sql("grants")
