"""Process-wide admission for optional passage matrices and replacement scratch."""

from __future__ import annotations

import os
import threading
import weakref

import numpy as np

from . import runtime_resources


class AdmissionDenied(RuntimeError):
    """Optional residency cannot fit; disk selection remains available."""


def ceiling() -> int:
    """The operator's numerical byte ceiling; optional heavy work defaults off."""
    try:
        return max(0, int(os.environ.get("EXOMEM_PASSAGE_MATRIX_BYTES", "0")))
    except ValueError:
        return 0


def metadata_bytes(rows: int, path_bytes: int) -> int:
    """Estimate array/list headers, row objects and worst-case Unicode path storage."""
    return 256 + rows * 192 + path_bytes * 4


class MatrixBudget:
    """One numerical counter, including arrays held after their index retires."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bytes = 0
        self._pending_headroom = 0

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def reserve(self, numerical_bytes: int, headroom_bytes: int) -> _Reservation:
        """Reserve before allocation, atomically against other unmaterialized work.

        Denial prevents optional arrays from exhausting memory. A false denial
        costs passage latency only, and never blocks disk service.
        """
        with self._lock:
            readings = [
                value for value in (
                    runtime_resources.available_memory(), runtime_resources.cgroup_memory_headroom()
                ) if value is not None
            ]
            if (
                not ceiling() or self._bytes + numerical_bytes > ceiling()
                or not readings or self._pending_headroom + headroom_bytes > min(readings)
            ):
                raise AdmissionDenied("optional passage matrix exceeds capacity")
            self._bytes += numerical_bytes
            self._pending_headroom += headroom_bytes
        return _Reservation(self, numerical_bytes, headroom_bytes)

    def _release(self, numerical_bytes: int, headroom_bytes: int = 0) -> None:
        with self._lock:
            self._bytes -= numerical_bytes
            self._pending_headroom -= headroom_bytes


class _Reservation:
    def __init__(self, budget: MatrixBudget, numerical_bytes: int, headroom_bytes: int) -> None:
        self._budget = budget
        self._bytes = numerical_bytes
        self._headroom = headroom_bytes

    def __enter__(self) -> _Reservation:
        return self

    def retain(self, matrix: np.ndarray) -> None:
        """Transfer the final array's charge to its lifetime, without retaining it."""
        size = matrix.nbytes
        if size > self._bytes or not matrix.flags.owndata:
            raise ValueError("passage matrix must own its reserved allocation")
        weakref.finalize(matrix, self._budget._release, size)
        self._bytes -= size

    def __exit__(self, *_exc) -> None:
        self._budget._release(self._bytes, self._headroom)
        self._bytes = self._headroom = 0


_BUDGET = MatrixBudget()
