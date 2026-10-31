"""How fast the runner's CPU is, recorded so that rehearsal runs on different hosts compare.

Two runs of the same scenario and seed on GitHub runners differed by 2 to 2.4 times in CPU-bound
work, so a single run cannot say whether a code change or a slower host moved a timing. The report
records the host's facts (`cpu_facts`) and the seconds of a fixed workload (`calibrate`). A timing
divided by the calibration ratio (`per_calibration`) is what the same work would take on a host as
fast as the reference. The raw timing is always kept next to it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import zlib
from typing import Any

from .shell import CommandFailed, run

# The workload is one pass of `SHA256_ROUNDS` hashes of one buffer, then `ZLIB_ROUNDS` compressions of
# another. Both run in the calling thread, so the number does not depend on the runner's core count.
# Changing any value below changes what the seconds mean: change WORKLOAD_ID and REFERENCE_SECONDS with it.
WORKLOAD_ID = "sha256-zlib-v1"
SHA256_BUFFER_BYTES = 1 << 20
SHA256_ROUNDS = 768
ZLIB_BUFFER_BYTES = 1 << 20
ZLIB_FRESH_BLOCK_BYTES = 16 << 10  # each block of random letters is followed by a copy of itself
ZLIB_LEVEL = 6
ZLIB_ROUNDS = 24
PASSES = 3  # the calibration is the fastest pass, so a neighbour's burst on a shared runner does not inflate it
SEED = b"exomem-cloud-rehearsal-host-calibration-v1"
# The seconds that one pass took when WORKLOAD_ID was defined. Only the ratio of two runs' calibrations
# matters, so this fixes a scale and says nothing about any runner.
REFERENCE_SECONDS = 0.5

# Sixteen letters, so the compressor has a real entropy-coding and matching job rather than a copy.
_LETTERS = bytes((byte & 0x0F) | 0x60 for byte in range(256))


def definition() -> dict[str, Any]:
    """The workload as the report records it. The Python and zlib builds are named because a build can change the speed."""
    return {
        "workload_id": WORKLOAD_ID,
        "sha256": {"buffer_bytes": SHA256_BUFFER_BYTES, "rounds": SHA256_ROUNDS},
        "zlib": {"buffer_bytes": ZLIB_BUFFER_BYTES, "fresh_block_bytes": ZLIB_FRESH_BLOCK_BYTES,
                 "level": ZLIB_LEVEL, "rounds": ZLIB_ROUNDS},
        "passes": PASSES, "reference_seconds": REFERENCE_SECONDS,
        "python": ".".join(str(part) for part in sys.version_info[:3]), "zlib_library": zlib.ZLIB_VERSION,
        "what": "single-threaded; the calibration is the seconds of the fastest pass, run before the scenario",
    }


def _hash_buffer() -> bytes:
    return bytes(range(256)) * (SHA256_BUFFER_BYTES // 256)


def _compressible_buffer() -> bytes:
    digest, blocks = hashlib.sha256(SEED).digest(), []
    for _ in range(ZLIB_BUFFER_BYTES // (2 * ZLIB_FRESH_BLOCK_BYTES)):
        fresh = bytearray()
        while len(fresh) < ZLIB_FRESH_BLOCK_BYTES:
            digest = hashlib.sha256(digest).digest()
            fresh += digest
        letters = bytes(fresh).translate(_LETTERS)
        blocks += [letters, letters]
    return b"".join(blocks)


def calibrate() -> dict[str, Any]:
    """Run the fixed workload; returns `host_calibration_seconds`, each pass's seconds and the workload's definition."""
    hashed, compressible = _hash_buffer(), _compressible_buffer()
    passes = []
    for _ in range(PASSES):
        started = time.perf_counter()
        for _ in range(SHA256_ROUNDS):
            hashlib.sha256(hashed).digest()
        for _ in range(ZLIB_ROUNDS):
            zlib.compress(compressible, ZLIB_LEVEL)
        passes.append(time.perf_counter() - started)
    return {"host_calibration_seconds": round(min(passes), 3),
            "host_calibration_pass_seconds": [round(seconds, 3) for seconds in passes],
            "host_calibration": definition()}


def calibration_ratio(calibration_seconds: float | None, reference_seconds: float = REFERENCE_SECONDS) -> float | None:
    """How many times slower than the reference this host ran the workload; None when it was not measured."""
    if calibration_seconds is None or calibration_seconds <= 0:
        return None
    return calibration_seconds / reference_seconds


def per_calibration(seconds: float | None, calibration_seconds: float | None,
                    reference_seconds: float = REFERENCE_SECONDS) -> float | None:
    """`seconds` divided by the calibration ratio; None when either the timing or the calibration is unknown."""
    ratio = calibration_ratio(calibration_seconds, reference_seconds)
    if seconds is None or ratio is None:
        return None
    return round(seconds / ratio, 2)


def parse_lscpu(text: str) -> dict[str, str]:
    """The `Key: value` lines of `lscpu` in the C locale; the first line wins where a key repeats."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and value.strip():
            fields.setdefault(key.strip(), value.strip())
    return fields


def _number(text: str | None, kind: type[int] | type[float]) -> int | float | None:
    try:
        return kind(text) if text is not None else None
    except ValueError:
        return None


def cpu_facts() -> dict[str, Any]:
    """The runner's CPU as `lscpu` reports it. A fact that `lscpu` does not give stays None, never 0."""
    try:
        fields = parse_lscpu(run(["lscpu"], env={**os.environ, "LC_ALL": "C"}, timeout=30).stdout)
    except (OSError, CommandFailed, subprocess.TimeoutExpired):
        fields = {}
    return {"cpu_model": fields.get("Model name"), "cpu_count_lscpu": _number(fields.get("CPU(s)"), int),
            "cpu_mhz": _number(fields.get("CPU MHz"), float)}
