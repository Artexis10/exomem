"""How fast the runner's CPU is, recorded so that rehearsal runs on different hosts compare.

Two runs of the same scenario and seed on GitHub runners differed by 2 to 2.4 times in CPU-bound
work, so a single run cannot say whether a code change or a slower host moved a timing. The report
records the host's facts (`cpu_facts`) and the seconds of a fixed workload (`calibrate`). A timing
divided by the calibration ratio (`per_calibration`) is what the same work would take on a host as
fast as the reference, if the work is CPU-bound. The raw timing is always kept next to it.

The ratio rests on the zlib part of the workload alone. The SHA-256 part runs about 6 times faster
on a CPU with SHA-NI, so two runners of equal general speed can differ by that much in it. Its
seconds are recorded as a fact, to tell an instruction-set difference from a general-speed one.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import zlib
from collections.abc import Mapping
from typing import Any

from .shell import CommandFailed, run

# The workload is one pass of `SHA256_ROUNDS` hashes of one buffer, then `ZLIB_ROUNDS` compressions of
# another, each part timed on its own. Both run in the calling thread, so the numbers do not depend on
# the runner's core count. Changing any value below changes what the seconds mean: change WORKLOAD_ID
# and REFERENCE_ZLIB_SECONDS with it.
WORKLOAD_ID = "sha256-zlib-v2"
SHA256_BUFFER_BYTES = 1 << 20
SHA256_ROUNDS = 192
ZLIB_BUFFER_BYTES = 1 << 20
ZLIB_FRESH_BLOCK_BYTES = 16 << 10  # each block of random letters is followed by a copy of itself
ZLIB_LEVEL = 6
ZLIB_ROUNDS = 24
PASSES = 3  # each part's calibration is its fastest pass, so a neighbour's burst on a shared runner does not inflate it
SEED = b"exomem-cloud-rehearsal-host-calibration-v1"
# The zlib seconds that one pass took when WORKLOAD_ID was defined. Only the ratio of two runs' calibrations
# matters, so this fixes a scale and says nothing about any runner.
REFERENCE_ZLIB_SECONDS = 0.32

# The keys of the calibration in `environment`. The writer (`calibrate`) and the reader
# (`recorded_zlib_seconds`) both take them from here, so a rename cannot turn every scaled value into None.
ZLIB_SECONDS_KEY = "host_calibration_zlib_seconds"
SHA256_SECONDS_KEY = "host_calibration_sha256_seconds"
PASS_SECONDS_KEY = "host_calibration_pass_seconds"
WORKLOAD_KEY = "host_calibration_workload"

# Sixteen letters, so the compressor has a real entropy-coding and matching job rather than a copy.
_LETTERS = bytes((byte & 0x0F) | 0x60 for byte in range(256))


def definition() -> dict[str, Any]:
    """The workload as the report records it. The Python and zlib builds are named because a build can change the speed."""
    return {
        "workload_id": WORKLOAD_ID,
        "sha256": {"buffer_bytes": SHA256_BUFFER_BYTES, "rounds": SHA256_ROUNDS},
        "zlib": {"buffer_bytes": ZLIB_BUFFER_BYTES, "fresh_block_bytes": ZLIB_FRESH_BLOCK_BYTES,
                 "level": ZLIB_LEVEL, "rounds": ZLIB_ROUNDS},
        "passes": PASSES, "reference_zlib_seconds": REFERENCE_ZLIB_SECONDS,
        "python": ".".join(str(part) for part in sys.version_info[:3]), "zlib_library": zlib.ZLIB_VERSION,
        "what": "single-threaded, run before the scenario; each part's calibration is the seconds of its fastest "
                "pass; the ratio that scales timings rests on the zlib part alone, and the SHA-256 part is a recorded "
                "fact because SHA-NI changes it about 6 times",
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
    """Run the fixed workload; returns the best zlib and SHA-256 seconds, every pass's seconds and the definition."""
    hashed, compressible = _hash_buffer(), _compressible_buffer()
    passes = []
    for _ in range(PASSES):
        started = time.perf_counter()
        for _ in range(SHA256_ROUNDS):
            hashlib.sha256(hashed).digest()
        hashed_at = time.perf_counter()
        for _ in range(ZLIB_ROUNDS):
            zlib.compress(compressible, ZLIB_LEVEL)
        passes.append({"sha256": hashed_at - started, "zlib": time.perf_counter() - hashed_at})
    return {ZLIB_SECONDS_KEY: round(min(each["zlib"] for each in passes), 3),
            SHA256_SECONDS_KEY: round(min(each["sha256"] for each in passes), 3),
            PASS_SECONDS_KEY: [{part: round(seconds, 3) for part, seconds in each.items()} for each in passes],
            WORKLOAD_KEY: definition()}


def recorded_zlib_seconds(environment: Mapping[str, Any]) -> float | None:
    """The calibration that scales timings, as `calibrate` recorded it in the report's `environment`; None when absent."""
    return environment.get(ZLIB_SECONDS_KEY)


def calibration_ratio(zlib_seconds: float | None, reference_seconds: float = REFERENCE_ZLIB_SECONDS) -> float | None:
    """How many times slower than the reference this host ran the zlib workload; None when it was not measured."""
    if zlib_seconds is None or zlib_seconds <= 0:
        return None
    return zlib_seconds / reference_seconds


def per_calibration(seconds: float | None, zlib_seconds: float | None,
                    reference_seconds: float = REFERENCE_ZLIB_SECONDS) -> float | None:
    """`seconds` divided by the calibration ratio; None when either the timing or the calibration is unknown."""
    ratio = calibration_ratio(zlib_seconds, reference_seconds)
    if seconds is None or ratio is None:
        return None
    return round(seconds / ratio, 2)


def parse_lscpu(text: str) -> dict[str, str]:
    """The `Key: value` lines of `lscpu` in the C locale; the first line wins where a key repeats."""
    # Closed key set fixed by util-linux; format, not meaning.
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
