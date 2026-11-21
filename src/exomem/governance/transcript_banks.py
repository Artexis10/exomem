"""Content-addressed passage measurements for selected projection variants."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from collections.abc import Sequence
from pathlib import Path

from .. import private_state, state_paths
from ..transcript_passages import TranscriptMatch

_PROJECTIONS = ".authorization-projections"
STORE = f"{_PROJECTIONS}/transcript-banks"
_BankLocations = Sequence[tuple[int, TranscriptMatch]]


class TranscriptBankUnavailable(RuntimeError):
    """An admitted variant's exact passage measurements could not be read."""


def publish(root: Path, candidates: _BankLocations, vectors) -> str:
    import numpy as np

    matrix = np.asarray(vectors, dtype=">f4")
    if matrix.ndim != 2 or len(matrix) != len(candidates) or not np.isfinite(matrix).all():
        raise TranscriptBankUnavailable("invalid transcript measurements")
    locations = b"".join(struct.pack(">IQ", part, match.start) for part, match in candidates)
    payload = locations + matrix.tobytes()
    digest = hashlib.sha256(payload).hexdigest()
    private_state.store(root, _PROJECTIONS)
    directory = private_state.store(root, STORE)
    try:
        private_state.write_record(
            directory, digest, {"data": base64.b64encode(payload).decode("ascii")},
            temp_suffix=".part", durable=True, create_only=True,
        )
    except FileExistsError:
        pass
    private_state.sync_directory(directory)
    return digest


def load(root: Path, digest: str, candidates: _BankLocations, dimension: int):
    """Read a single admitted bank and verify its root and published coordinates."""
    import numpy as np

    payload_size = len(candidates) * (12 + dimension * 4)
    location_size = len(candidates) * 12
    encoded_size = ((payload_size + 2) // 3) * 4
    path = _bank_path(root, digest)
    try:
        with path.open("rb") as source:
            raw = source.read(encoded_size + 33)
        payload = base64.b64decode(json.loads(raw)["data"], validate=True)
        expected = b"".join(struct.pack(">IQ", part, match.start) for part, match in candidates)
        if (
            len(payload) != payload_size or payload[:location_size] != expected
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise ValueError("bank binding changed")
        return np.frombuffer(payload, dtype=">f4", offset=location_size).reshape(len(candidates), dimension)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        raise TranscriptBankUnavailable("the projected transcript bank could not be verified") from None


def _bank_path(root: Path, digest: str) -> Path:
    return state_paths.vault_state_dir(root) / STORE / f"{digest}.json"


def available(root: Path, digest: str) -> bool:
    return _bank_path(root, digest).is_file()
