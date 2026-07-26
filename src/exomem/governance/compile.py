"""Compiled-policy snapshot writer/reader for the governance sidecar.

A normalized, JSON-serialized mirror of an in-process `Policy` for
cross-process inspection/doctor tooling (design decisions D1/D6) — never
read back into the live `policy.load` cache, and never the enforcement
authority.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import store
from .policy import Policy


def _normalize(policy: Policy) -> dict[str, Any]:
    return {
        "fingerprint": policy.fingerprint,
        "scopes": {sid: asdict(scope) for sid, scope in sorted(policy.scopes.items())},
        "rules": [asdict(rule) for rule in policy.rules],
        "grants": [asdict(grant) for grant in policy.grants],
        "findings": list(policy.findings),
    }


def write_snapshot(vault_root: Path, policy: Policy) -> None:
    """Persist `policy` into the sidecar, keyed by its own fingerprint.

    Refuses `policy.empty`/`policy.blocked` outright, before ever opening the
    sidecar: neither is a real compile — `EMPTY_POLICY` has nothing to
    inspect (a stable "missing" fingerprint shared by every ungoverned
    vault), and a `.blocked` refusal is a fail-closed floor, not a snapshot
    of authored policy. Persisting either would leave a misleading row a
    doctor/inspection tool could mistake for genuine compiled policy.
    """
    if policy.empty or policy.blocked:
        state = "blocked" if policy.blocked else "empty"
        raise ValueError(
            f"CANNOT_SNAPSHOT_POLICY: refusing to persist a {state} policy "
            f"(fingerprint={policy.fingerprint!r}) to the governance sidecar"
        )
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {store.DATA_TABLE} (fingerprint, snapshot, compiled_at) "
            "VALUES (?, ?, ?)",
            (policy.fingerprint, json.dumps(_normalize(policy), sort_keys=True), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def read_snapshot(vault_root: Path, fingerprint: str) -> dict[str, Any] | None:
    """Return the normalized snapshot last written under `fingerprint`, or `None`."""
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            f"SELECT snapshot FROM {store.DATA_TABLE} WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None
    finally:
        conn.close()
