"""Content-addressed passage measurements for selected projection variants."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import threading
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

from .. import private_state, state_paths
from ..transcript_passages import TranscriptMatch
from .projections import ProjectionNamespaceKey

DIRECTORY = "transcript-banks"
_BAD_LIMIT = 256
_bad: OrderedDict[tuple[Path, str], None] = OrderedDict()
_bad_lock = threading.Lock()
_BankLocations = Sequence[tuple[int, TranscriptMatch]]


class TranscriptBankUnavailable(RuntimeError):
    """An admitted variant's exact passage measurements could not be read."""


def publish(root: Path, key: ProjectionNamespaceKey, candidates: _BankLocations, vectors) -> str:
    import numpy as np

    matrix = np.asarray(vectors, dtype=">f4")
    if matrix.ndim != 2 or len(matrix) != len(candidates) or not np.isfinite(matrix).all():
        raise TranscriptBankUnavailable("invalid transcript measurements")
    locations = b"".join(struct.pack(">IQ", part, match.start) for part, match in candidates)
    payload = locations + matrix.tobytes()
    digest = hashlib.sha256(payload).hexdigest()
    directory = _directory(root, key, create=True)
    record = {"data": base64.b64encode(payload).decode("ascii")}
    try:
        private_state.write_record(directory, digest, record, temp_suffix=".part", durable=True, create_only=True)
    except FileExistsError:
        try:
            load(root, key, digest, candidates, int(matrix.shape[1]))
        except TranscriptBankUnavailable:
            # Replace the bad inode. A pinned namespace that still links to
            # that inode will never see unverified bytes from this replacement.
            private_state.write_record(directory, digest, record, temp_suffix=".part", durable=True)
    private_state.sync_directory(directory)
    with _bad_lock:
        _bad.pop((root, digest), None)
    return digest


def load(
    root: Path, key: ProjectionNamespaceKey, digest: str, candidates: _BankLocations, dimension: int,
):
    """Read a single admitted bank and verify its root and published coordinates."""
    import numpy as np

    payload_size = len(candidates) * (12 + dimension * 4)
    location_size = len(candidates) * 12
    encoded_size = ((payload_size + 2) // 3) * 4
    path = _directory(root, key) / f"{digest}.json"
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
        with _bad_lock:
            _bad[(root, digest)] = None
            _bad.move_to_end((root, digest))
            if len(_bad) > _BAD_LIMIT:
                _bad.popitem(last=False)
        raise TranscriptBankUnavailable("the projected transcript bank could not be verified") from None


def _directory(root: Path, key: ProjectionNamespaceKey, *, create: bool = False) -> Path:
    from .projection_store import variant_store_path

    namespace = variant_store_path(root, key).parent
    directory = namespace / DIRECTORY
    if create:
        state = state_paths.vault_state_dir(root)
        for path in (namespace.parent, namespace, directory):
            private_state.store(root, path.relative_to(state).as_posix())
    return directory


def carry(root: Path, before: ProjectionNamespaceKey, after: ProjectionNamespaceKey, digest: str) -> bool:
    """Reuse a bank's immutable bytes under the target namespace's own lifetime."""
    with _bad_lock:
        if (root, digest) in _bad:
            return False
    old = _directory(root, before) / f"{digest}.json"
    target = _directory(root, after, create=True) / f"{digest}.json"
    if old == target:
        return old.is_file()
    try:
        os.link(old, target)
    except FileNotFoundError:
        return False
    except FileExistsError:
        pass
    return True


def sync(root: Path, key: ProjectionNamespaceKey) -> None:
    directory = _directory(root, key)
    if directory.is_dir():
        private_state.sync_directory(directory)
