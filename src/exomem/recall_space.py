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
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: The encoder every sidecar without a record was written by.
LEGACY_MODEL = "BAAI/bge-base-en-v1.5"
LEGACY_DIM = 768

#: Mirror `hosted_runtime.HOSTED_MODE_ENV` and `cloud_cell.CLOUD_MODE_ENV`. Read
#: directly: `embeddings` imports this module and must stay cheap to import.
_CELL_FLAGS = ("EXOMEM_HOSTED_CELL", "EXOMEM_CLOUD_CELL")
_FALSE = frozenset({"", "0", "false", "no", "off"})

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


def cell_mode(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process is a hosted or cloud cell.

    A malformed flag counts as a cell, the way content-private logging reads
    it: such a cell refuses to start anyway.
    """
    values = os.environ if env is None else env
    return any(str(values.get(flag, "")).strip().lower() not in _FALSE for flag in _CELL_FLAGS)


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


def encoding_for(index: object, *, load: bool = False) -> contextlib.AbstractContextManager[None]:
    """`index.encoding()`: encode for that sidecar, or refuse before encoding.

    `load` lets a batch caller (the CLI's incremental index, never a request)
    load the encoder that serves the sidecar when it is not resident. An index
    adapter that keeps no vector-space record (a narrow third-party or
    in-memory index) has no `encoding`, and nothing to check.
    """
    encoding = getattr(index, "encoding", None)
    if not callable(encoding):
        return contextlib.nullcontext()
    return encoding(load=True) if load else encoding()


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

    if model == embeddings.MODEL_NAME:
        resident = embeddings._MODEL
    else:
        resident = previous_resident(model)
    profile = getattr(resident, "profile", None)
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


# ------------------------------------------------------- the serving encoder


class ServingEncoderCold(RuntimeError):
    """The encoder that serves the sidecar is not resident; requests never load it."""

    reason = "model_warming"


#: The encoder of a sidecar recall still serves from while a new space is built
#: (`recall_migration`): at most one, loaded by warm-up or the migration job and
#: never by a request, and released at the cutover. It has an execution slot of
#: its own, so a query for the serving sidecar never waits behind a batch of the
#: build, which encodes with the recall encoder.
_PREVIOUS_LOCK = threading.Lock()
_PREVIOUS: tuple[str, Any] | None = None
_PREVIOUS_GENERATION = 0
_PREVIOUS_INFLIGHT = 0
_PREVIOUS_GATE: Any = None


def previous_encoder(model: str) -> Any:
    """The serving sidecar's encoder, loading it: warm-up and background threads only."""
    global _PREVIOUS, _PREVIOUS_GENERATION
    from . import embedding_backend

    with _PREVIOUS_LOCK:
        if _PREVIOUS is not None and _PREVIOUS[0] == model:
            return _PREVIOUS[1]
    encoder = embedding_backend.load_encoder(model)
    with _PREVIOUS_LOCK:
        if _PREVIOUS is not None and _PREVIOUS[0] == model:
            return _PREVIOUS[1]
        replaced, _PREVIOUS = _PREVIOUS, (model, encoder)
        _PREVIOUS_GENERATION += 1
    if replaced is not None:
        _release(replaced[1])
    return encoder


def previous_resident(model: str) -> Any | None:
    """The serving sidecar's encoder when it is resident; never loads."""
    with _PREVIOUS_LOCK:
        if _PREVIOUS is not None and _PREVIOUS[0] == model:
            return _PREVIOUS[1]
    return None


def unload_previous() -> bool:
    """Release the serving sidecar's encoder. True when one was resident.

    An encode in flight keeps its own reference and finishes on it; the runtime
    memory goes back when that reference does.
    """
    global _PREVIOUS, _PREVIOUS_GENERATION
    with _PREVIOUS_LOCK:
        if _PREVIOUS is None:
            return False
        released, _PREVIOUS = _PREVIOUS, None
        _PREVIOUS_GENERATION += 1
        in_flight = _PREVIOUS_INFLIGHT
    if not in_flight:
        _release(released[1])
    return True


def _release(encoder: Any) -> None:
    release = getattr(encoder, "release", None)
    if release is not None:
        with contextlib.suppress(Exception):  # an unload must never raise
            release()


def _previous_gate() -> Any:
    global _PREVIOUS_GATE
    from . import runtime_resources

    with _PREVIOUS_LOCK:
        if _PREVIOUS_GATE is None:
            _PREVIOUS_GATE = runtime_resources.ModelAdmissionGate(
                runtime_resources.resolve_policy().model_admission
            )
        return _PREVIOUS_GATE


@contextlib.contextmanager
def _in_flight() -> Iterator[None]:
    global _PREVIOUS_INFLIGHT
    with _PREVIOUS_LOCK:
        _PREVIOUS_INFLIGHT += 1
    try:
        yield
    finally:
        with _PREVIOUS_LOCK:
            _PREVIOUS_INFLIGHT -= 1


def encode_with_previous(model: str, texts: list[str], *, is_query: bool) -> Any:
    """Encode for a sidecar written by `model`, with its resident encoder."""
    from . import embeddings

    encoder = previous_resident(model)
    if encoder is None:
        raise ServingEncoderCold(f"{model} is not resident")
    query_prefix, passage_prefix = embeddings._prefixes(encoder, model)
    prefix = query_prefix if is_query else passage_prefix
    gate = _previous_gate()
    with _in_flight():
        return embeddings._encode_in_turns(
            encoder,
            [prefix + text for text in texts] if prefix else list(texts),
            admission=gate.admission,
            execution=gate.execution,
        )


def previous_space(model: str) -> str:
    """The passage-memo space of the serving sidecar's encoder, with its load."""
    from . import embedding_backend

    return f"{resident_fingerprint(model) or embedding_backend.fingerprint(model)}#{_PREVIOUS_GENERATION}"
