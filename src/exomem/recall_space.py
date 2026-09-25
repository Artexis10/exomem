"""Which vector space a recall sidecar holds, and which encoder may read it.

Vectors from two encoders are not comparable, even at one width. So every
store of recall vectors records the encoder that wrote it: the model, the
encoder's own fingerprint when a profiled encoder was resident (for a served
model that names the exact bytes it ran), and the width. A reader compares
that record with the encoder it is about to use, and refuses to mix.

Two fingerprints are compared exactly only when both are known. A record with
no fingerprint was written before one existed, or by a substitute encoder with
no profile, and matches by model name; the record's width still has to match
the vectors. A sidecar with rows and no record at all was written before the
record existed, by the only encoder recall ever shipped: `LEGACY_MODEL` at 768
dimensions.

Encoding for a sidecar goes through `EmbeddingIndex.encoding`, which selects
the encoder that serves the sidecar's space here (`selected_model`) and checks
it before anything is encoded. With nothing selected the encoder is the
configured recall model, exactly as before.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

#: The encoder every sidecar without a record was written by.
LEGACY_MODEL = "BAAI/bge-base-en-v1.5"
LEGACY_DIM = 768

#: Widths of the models recall declares, so an empty store answers at the width
#: its encoder will produce without loading the encoder. Anything else is
#: learned from the first vectors written.
DECLARED_DIMS: dict[str, int] = {LEGACY_MODEL: 768, "BAAI/bge-m3": 1024}

META_MODEL = "embedding_model"
META_FINGERPRINT = "embedding_fingerprint"
META_DIM = "embedding_dim"


class VectorSpaceMismatch(RuntimeError):
    """Vectors of one space were about to meet a store or encoder of another."""

    reason = "vector_space_mismatch"


@dataclass(frozen=True, slots=True)
class SpaceIdentity:
    """The encoder a store's vectors came from, and their width."""

    model: str
    fingerprint: str | None
    dim: int

    def accepts(self, model: str, fingerprint: str | None) -> bool:
        """Whether vectors from `model` (with `fingerprint`, when known) belong here."""
        if model != self.model:
            return False
        return self.fingerprint is None or fingerprint is None or fingerprint == self.fingerprint


LEGACY_IDENTITY = SpaceIdentity(LEGACY_MODEL, None, LEGACY_DIM)

_SELECTED: ContextVar[str | None] = ContextVar("exomem_recall_encoder", default=None)


def recall_model() -> str:
    """The model recall encodes with when no sidecar selects another."""
    from . import embeddings

    return embeddings.MODEL_NAME


def selected_model() -> str | None:
    """The model an enclosing `selecting` block named, or None."""
    return _SELECTED.get()


@contextlib.contextmanager
def selecting(model: str | None) -> Iterator[None]:
    """Encode with `model` inside the block; None keeps the recall model."""
    token = _SELECTED.set(model)
    try:
        yield
    finally:
        _SELECTED.reset(token)


def encoding_for(index: object) -> contextlib.AbstractContextManager[None]:
    """`index.encoding()`: encode for that sidecar, or refuse before encoding.

    An index adapter that keeps no vector-space record (a narrow third-party or
    in-memory index) has no `encoding`, and nothing to check.
    """
    encoding = getattr(index, "encoding", None)
    return encoding() if callable(encoding) else contextlib.nullcontext()


def declared_dim(model: str) -> int:
    """The width `model` produces, or the legacy width when recall does not know it."""
    return DECLARED_DIMS.get(model, LEGACY_DIM)


def encoding_model() -> str:
    """The model an encode made now would use."""
    return selected_model() or recall_model()


def resident_fingerprint(model: str) -> str | None:
    """The fingerprint of `model`'s resident profiled encoder, or None.

    None when the model is not resident, or the resident object carries no
    profile (a substitute). Never loads anything.
    """
    from . import embedding_backend, embeddings

    if model != embeddings.MODEL_NAME:
        return None
    profile = getattr(embeddings._MODEL, "profile", None)
    if isinstance(profile, embedding_backend.EncoderProfile) and profile.model == model:
        return profile.fingerprint()
    return None


def current_identity(dim: int, model: str | None = None) -> SpaceIdentity:
    """The identity vectors of width `dim` from `model` (default: the encoder in use) carry."""
    chosen = model or encoding_model()
    return SpaceIdentity(chosen, resident_fingerprint(chosen), int(dim))


def current_dim(model: str | None = None) -> int:
    """The width the encoder in use produces (declared, or the legacy width)."""
    return declared_dim(model or encoding_model())


# ------------------------------------------------------------------ the record


def read_identity(conn: sqlite3.Connection, *, tables: tuple[str, ...]) -> SpaceIdentity | None:
    """The store's recorded space; the legacy space for rows with no record; else None.

    `tables` hold the store's vector blobs, first row consulted for the legacy
    width. Read-only.
    """
    rows = dict(
        conn.execute(
            "SELECT key, value FROM meta WHERE key IN (?, ?, ?)",
            (META_MODEL, META_FINGERPRINT, META_DIM),
        ).fetchall()
    )
    model = rows.get(META_MODEL)
    if isinstance(model, str) and model:
        fingerprint = rows.get(META_FINGERPRINT)
        return SpaceIdentity(
            model,
            fingerprint if isinstance(fingerprint, str) and fingerprint else None,
            int(rows.get(META_DIM) or 0),
        )
    for table in tables:
        row = conn.execute(f"SELECT length(vector) FROM {table} LIMIT 1").fetchone()
        if row is not None and row[0]:
            return SpaceIdentity(LEGACY_MODEL, None, int(row[0]) // 4)
    return None


def write_identity(conn: sqlite3.Connection, identity: SpaceIdentity) -> None:
    """Record `identity` inside the caller's open write transaction."""
    conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (META_MODEL, META_FINGERPRINT, META_DIM))
    rows = [(META_MODEL, identity.model), (META_DIM, int(identity.dim))]
    if identity.fingerprint:
        rows.append((META_FINGERPRINT, identity.fingerprint))
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", rows)


def clear_identity(conn: sqlite3.Connection) -> None:
    """Forget the record inside the caller's open write transaction (an emptied store)."""
    conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (META_MODEL, META_FINGERPRINT, META_DIM))


def admit(
    conn: sqlite3.Connection,
    recorded: SpaceIdentity | None,
    dim: int,
) -> SpaceIdentity:
    """The identity rows of width `dim` are written under, recording it if new.

    Call inside the write transaction, before any row is written. A store with
    no record takes the encoder in use; a recorded store refuses another width.
    """
    if recorded is None:
        identity = current_identity(dim)
        write_identity(conn, identity)
        return identity
    if int(dim) != recorded.dim:
        raise VectorSpaceMismatch(
            f"{dim}-dimension vectors cannot join a {recorded.dim}-dimension store "
            f"of {recorded.model}"
        )
    identity = recorded
    if (
        recorded.fingerprint is None
        and recorded.model == encoding_model()
        and (fingerprint := resident_fingerprint(recorded.model))
    ):
        # The first write under a profiled encoder names the space exactly.
        identity = SpaceIdentity(recorded.model, fingerprint, recorded.dim)
    stored = conn.execute("SELECT value FROM meta WHERE key = ?", (META_MODEL,)).fetchone()
    if identity != recorded or stored is None:
        # A legacy store's inferred identity is recorded with its first write,
        # so emptying it later can never hand it to another encoder.
        write_identity(conn, identity)
    return identity
