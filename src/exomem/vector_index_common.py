"""Shared policy for sqlite-vec backed vector sidecars."""

from __future__ import annotations

import logging
import sqlite3
from typing import Protocol

from . import vecstore

log = logging.getLogger(__name__)


class VectorIndexState(Protocol):
    path: object
    _vec: vecstore.SqliteVecStore
    _vec_ready: bool | None
    _vec_quant_synced: bool
    _vec_failed: bool


def vec_gate(index: VectorIndexState, conn: sqlite3.Connection) -> bool:
    """Shared sqlite-vec policy ladder for vector sidecar indexes.

    Duck-typed over the index's vec state: backend gate -> extension loadable on
    this connection -> tables created and blob/vec counts synced. Any sync
    failure retires vec for this instance; the numpy scan serves from then on.
    """
    if index._vec_failed or vecstore.backend() == "numpy":
        return False
    if not index._vec.try_load(conn):
        return False
    quant = vecstore.quant_mode() == "binary"
    if index._vec_ready is None or (quant and not index._vec_quant_synced):
        try:
            index._vec.ensure_synced(conn, quant=quant)
            index._vec_ready = True
            if quant:
                index._vec_quant_synced = True
        except sqlite3.Error as e:
            log.warning(
                "vec sync failed for %s (%s); in-memory scan serves this process",
                index.path,
                e,
            )
            index._vec_failed = True
            return False
    return True
