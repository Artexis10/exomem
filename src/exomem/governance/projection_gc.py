"""Owner-only exact-tuple garbage collection for projection namespaces."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from . import projection_runtime, projection_store, schema_v4, store


class ProjectionCollectionUnavailable(RuntimeError):
    """The complete namespace retention snapshot cannot be proven."""


def _namespace_ids(values: Iterable[str]) -> frozenset[str]:
    normalized = frozenset(values)
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in normalized
    ):
        raise ProjectionCollectionUnavailable(
            "projection namespace retention cannot be verified"
        )
    return normalized


def _durable_namespace_snapshot(
    vault_root: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_active_governance_read_connection(vault_root)
        connection.execute("BEGIN")
        registered = schema_v4.projection_namespace_ids(connection)
        pinned = schema_v4.projection_namespace_pins(connection)
        connection.commit()
        return registered, pinned
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
    ) as error:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise ProjectionCollectionUnavailable(
            "projection namespace retention cannot be verified"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def collect_unpinned_projection_namespaces(
    vault_root: Path,
    *,
    retained_namespace_ids: Iterable[str],
) -> tuple[str, ...]:
    """Collect registered history outside every durable and process-local pin.

    The explicit retained set is the control-plane boundary for receipt, backup,
    and rollback pins.  This module has no public command route and never runs on
    a request thread.
    """

    root = Path(vault_root)
    retained = _namespace_ids(retained_namespace_ids)
    registered, durable = _durable_namespace_snapshot(root)
    runtime = projection_runtime.projection_namespace_runtime_pins(root)
    eligible = registered - durable - runtime - retained
    return projection_store.collect_projection_namespaces(
        root,
        eligible_namespace_ids=eligible,
    )


__all__ = [
    "ProjectionCollectionUnavailable",
    "collect_unpinned_projection_namespaces",
]
