"""The one classification rule for media originals: text, offloadable artifact or resident artifact (D12).

The rule is pure arithmetic over configured bounds, so the CLI, the adoption
inventory, offload and eviction all import it and no caller keeps a copy.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

# These three classes are the closed set that design D12 fixes; no caller extends them.
TEXT = "text"
OFFLOADABLE = "offloadable"
RESIDENT = "resident"

# age's STREAM format fixes the chunk and tag sizes (age v1 specification).
AGE_CHUNK_BYTES = 64 * 1024
AGE_TAG_BYTES = 16
# An upper bound on the header and nonce: one X25519 recipient measures 184 bytes.
# An underestimate would encrypt a file that the store then refuses by size.
AGE_HEADER_BYTES = 1024
# The store policy's default volume fraction (D12, choice 3); cellctl may render another.
DEFAULT_VOLUME_FRACTION = 0.25
UPLOAD_SPACE_BUSY = "MEDIA_SPACE_BUSY"
_COMMIT_CREDIT: ContextVar[str | None] = ContextVar("upload_commit_credit", default=None)


@dataclass(frozen=True, slots=True)
class UploadRoom:
    free: int
    floor: int
    allocation_unit: int
    credits: sqlite3.Connection | None = None
    reserved: int = 0
    store_pages: tuple[Path, int] | None = None

    def allocated(self, size: int) -> int:
        return -(-size // self.allocation_unit) * self.allocation_unit

    def require(self, needed: int, *, upload_size: int | None = None, offset: int = 0) -> None:
        """Fence the bytes still to allocate on the vault's volume."""
        physical = self.allocated(offset + needed) - self.allocated(offset)
        self._require_physical(physical, needed if upload_size is None else upload_size)

    def reserve_commit(self, length: int, copies: int, *, incoming: int, offset: int,
                       extra_sizes: tuple[int, ...] = ()) -> None:
        """Reserve this session's known simultaneous commit copies before its last part grows."""
        session = _COMMIT_CREDIT.get()
        assert self.credits is not None and session is not None
        peak = self.allocated(length) * copies + sum(self.allocated(size) for size in extra_sizes)
        growth = self.allocated(offset + incoming) - self.allocated(offset)
        self._require_physical(peak + growth, length)
        _set_credit(self.credits, session, peak)

    def reserve_store_pages(
        self, database: Path, *, target_bytes: int, wal_bytes: int, upload_size: int,
    ) -> None:
        """Fence the WAL append and the database growth it can require at checkpoint."""
        self.set_store_pages(database, self.require_store_pages(
            database, target_bytes=target_bytes, wal_bytes=wal_bytes, upload_size=upload_size,
        ))

    def require_store_pages(
        self, database: Path, *, target_bytes: int, wal_bytes: int, upload_size: int,
    ) -> int:
        """Refuse store growth beyond the floor without recording it; return the checkpoint target.

        A writer that already recorded a target in this room checks a later estimate in memory,
        and records the real page count once it commits.
        """
        prior = self.store_pages[1] if self.store_pages is not None and self.store_pages[0] == database else 0
        target = max(target_bytes, prior)
        current = _file_size(database)
        extra = max(0, target - max(prior, current))
        self._require_physical(self.allocated(wal_bytes) + self.allocated(extra), upload_size)
        return target

    def set_store_pages(self, database: Path, target_bytes: int) -> None:
        """Remember checkpoint growth even when a reader keeps the WAL after commit."""
        assert self.credits is not None
        with self.credits:
            self.credits.execute(
                "INSERT INTO store_pages VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE "
                "SET database_path = excluded.database_path, target_bytes = excluded.target_bytes",
                (str(database), target_bytes),
            )

    def _require_physical(self, physical: int, size: int) -> None:
        if physical > 0 and self.free - self.reserved - physical < self.floor:
            from .cli_ops import OpError

            held = f" Other uploads have reserved {self.reserved:,} bytes." if self.reserved else ""
            raise OpError(UPLOAD_SPACE_BUSY,
                          f"The vault has {self.free:,} free bytes and a {self.floor:,}-byte floor.{held} "
                          f"The {size:,}-byte original needs {physical:,} bytes of allocation. Retry after eviction frees space.",
                          "Retry after eviction frees space.")


def upload_floor(vault: Path) -> int | None:
    """The configured physical vault-volume floor, if the owner set one."""
    from . import originals

    config = originals.configuration(vault)
    floor = config.get("upload_floor_bytes") if config is not None else None
    if floor is not None and (type(floor) is not int or floor < 0):
        raise ValueError("upload_floor_bytes must be a nonnegative integer")
    return floor


def managed_allocations(vault: Path) -> bool:
    """Keep allocations in the gate when an owner can set a floor while they run."""
    from . import originals

    return originals.uses_local_configuration() or upload_floor(vault) is not None


@contextmanager
def upload_room(vault: Path, *, device: int | None = None, reserve: bool = False) -> Iterator[UploadRoom | None]:
    """Serialize managed vault-volume allocations with the current owner policy."""
    if device is not None and device != vault.stat().st_dev or not managed_allocations(vault):
        yield None
        return
    with allocation_policy(vault) as directory:
        floor = upload_floor(vault)
        if floor is None:
            yield None
            return
        credits_path = directory / "upload-commit-credit.sqlite"
        store = closing(_open_credits(credits_path, create=reserve)) if reserve or credits_path.exists() else nullcontext()
        with store as credits:
            reserved = 0
            store_pages = None
            if credits is not None:
                reserved = credits.execute("SELECT peak FROM credit_total").fetchone()[0]
                reserved -= _credit_for(credits, _COMMIT_CREDIT.get())
                store_pages = _store_pages(credits)
            allocation_unit = _allocation_unit(vault)
            if store_pages is not None:
                database, target = store_pages
                # Read the pending growth before free space. An intervening checkpoint can
                # over-reserve this sample, but cannot make both its unspent credit and its bytes available.
                pending = max(0, target - _file_size(database))
                reserved += -(-pending // allocation_unit) * allocation_unit
            yield UploadRoom(shutil.disk_usage(vault).free, floor, allocation_unit, credits, reserved, store_pages)


@contextmanager
def allocation_policy(vault: Path) -> Iterator[Path]:
    """Hold the same short lock for an allocation or a local policy change."""
    from .state_paths import ensure_vault_state_dir

    directory = ensure_vault_state_dir(vault)
    with _allocation_lock(directory):
        yield directory


@contextmanager
def _allocation_lock(directory: Path) -> Iterator[None]:
    from filelock import FileLock, Timeout

    from .cli_ops import OpError

    try:
        with FileLock(directory / "upload-space.lock", timeout=5):
            yield
    except Timeout as error:
        raise OpError(UPLOAD_SPACE_BUSY, "Another write is allocating vault space. Retry shortly.", "Retry shortly.") from error


def _open_credits(path: Path, *, create: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    if create or path.stat().st_size == 0:
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE IF NOT EXISTS credits (session TEXT PRIMARY KEY, peak INTEGER NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS credit_total (id INTEGER PRIMARY KEY CHECK(id=1), peak INTEGER NOT NULL)")
                conn.execute("INSERT OR IGNORE INTO credit_total VALUES (1, 0)")
                conn.execute("CREATE TABLE IF NOT EXISTS store_pages ("
                             "id INTEGER PRIMARY KEY CHECK(id=1), database_path TEXT NOT NULL, target_bytes INTEGER NOT NULL)")
        except BaseException:
            conn.close()
            raise
    return conn


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _store_pages(conn: sqlite3.Connection) -> tuple[Path, int] | None:
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'store_pages'").fetchone() is None:
        return None  # Earlier upload-credit ledgers acquired the table on their next reservation.
    row = conn.execute("SELECT database_path, target_bytes FROM store_pages WHERE id=1").fetchone()
    return (Path(row[0]), row[1]) if row is not None else None


def _credit_for(conn: sqlite3.Connection, session: str | None) -> int:
    row = conn.execute("SELECT peak FROM credits WHERE session = ?", (session,)).fetchone()
    return 0 if row is None else row[0]


def _set_credit(conn: sqlite3.Connection, session: str, peak: int) -> None:
    previous = _credit_for(conn, session)
    if peak == previous:
        return
    with conn:
        if peak:
            conn.execute("INSERT INTO credits VALUES (?, ?) ON CONFLICT(session) DO UPDATE SET peak=excluded.peak",
                         (session, peak))
        else:
            conn.execute("DELETE FROM credits WHERE session = ?", (session,))
        conn.execute("UPDATE credit_total SET peak = peak + ? WHERE id=1", (peak - previous,))


@contextmanager
def commit_credit(session: str) -> Iterator[None]:
    token = _COMMIT_CREDIT.set(session)
    try:
        yield
    finally:
        _COMMIT_CREDIT.reset(token)


def release_commit_credit(vault: Path, session: str) -> None:
    """Retire the reservation after the session's held bytes are gone."""
    from .state_paths import vault_state_dir

    directory = vault_state_dir(vault)
    path = directory / "upload-commit-credit.sqlite"
    if path.exists():
        with _allocation_lock(directory), closing(_open_credits(path)) as conn:
            _set_credit(conn, session, 0)


def _allocation_unit(vault: Path) -> int:
    if hasattr(os, "statvfs"):
        geometry = os.statvfs(vault)
        return geometry.f_frsize or geometry.f_bsize
    from ctypes import WinDLL, WinError, byref, create_unicode_buffer, c_wchar_p
    from ctypes.wintypes import DWORD

    kernel = WinDLL("kernel32", use_last_error=True)
    root = create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(c_wchar_p(str(vault.resolve())), root, len(root)):
        raise WinError()
    sectors, bytes_per_sector, free, total = (DWORD() for _ in range(4))
    if not kernel.GetDiskFreeSpaceW(root, byref(sectors), byref(bytes_per_sector), byref(free), byref(total)):
        raise WinError()
    return sectors.value * bytes_per_sector.value


def metered_chunks(vault: Path, device: int, chunks: Iterator[bytes], *, upload_size: int | None = None) -> Iterator[bytes]:
    """Keep the allocation lock through one synchronous write, with no read or network wait under it."""
    offset = 0
    for chunk in chunks:
        with upload_room(vault, device=device) as room:
            if room is not None:
                room.require(len(chunk), offset=offset, upload_size=upload_size)
            yield chunk
        offset += len(chunk)


def append_disk_spool(vault: Path, sink: BinaryIO, chunk: bytes, *, device: int, rolling: bool) -> None:
    """Account for the in-memory prefix when the spool first moves to disk."""
    offset = sink.tell()
    needs = offset + len(chunk) if rolling else len(chunk)
    with upload_room(vault, device=device) as room:
        if room is not None:
            room.require(needs, offset=0 if rolling else offset, upload_size=offset + len(chunk))
        sink.write(chunk)
        sink.flush()


class MeteredSink:
    def __init__(self, vault: Path, sink: BinaryIO, *, device: int, size: int) -> None:
        self.vault = vault
        self.sink = sink
        self.device = device
        self.size = size
        self.offset = 0

    def write(self, chunk: bytes) -> int:
        with upload_room(self.vault, device=self.device) as room:
            if room is not None:
                room.require(len(chunk), offset=self.offset, upload_size=self.size)
            count = self.sink.write(chunk)
            self.sink.flush()
        self.offset += count
        return count

    def flush(self) -> None:
        self.sink.flush()


def age_ciphertext_size(plaintext_bytes: int) -> int:
    """The largest `age` ciphertext that `plaintext_bytes` of plaintext can produce."""
    if plaintext_bytes < 0:
        raise ValueError("a plaintext size cannot be negative")
    chunks = max(1, -(-plaintext_bytes // AGE_CHUNK_BYTES))
    return AGE_HEADER_BYTES + plaintext_bytes + AGE_TAG_BYTES * chunks


def _largest_plaintext_within(ciphertext_bound: int) -> int:
    """The largest plaintext whose `age_ciphertext_size` is at most `ciphertext_bound`, or -1."""
    available = ciphertext_bound - AGE_HEADER_BYTES
    if available < AGE_TAG_BYTES:
        return -1
    full, rest = divmod(available, AGE_CHUNK_BYTES + AGE_TAG_BYTES)
    return full * AGE_CHUNK_BYTES + max(0, rest - AGE_TAG_BYTES)


def classify(plaintext_bytes: int, largest_offloadable: int, *, text: bool = False) -> str:
    """The rule itself: text stays text, and an artifact offloads only up to the largest offloadable file.

    A sender that reads `largest_offloadable` from the cell's status applies the rule without the bounds.
    """
    if text:
        return TEXT
    return OFFLOADABLE if plaintext_bytes <= largest_offloadable else RESIDENT


@dataclass(frozen=True, slots=True)
class Bounds:
    """The rule's inputs. A missing object bound means the store sets none."""

    object_bytes: int | None
    hydration_bytes: int
    volume_bytes: int
    volume_fraction: float = DEFAULT_VOLUME_FRACTION

    def largest_offloadable(self) -> int:
        """The largest plaintext that classifies as offloadable, or -1 when none does."""
        ciphertext_bound = self.hydration_bytes if self.object_bytes is None else min(self.object_bytes, self.hydration_bytes)
        return min(_largest_plaintext_within(ciphertext_bound), int(self.volume_fraction * self.volume_bytes))

    def classify(self, plaintext_bytes: int, *, text: bool = False) -> str:
        """Classify one member; the caller applies the importer's text rule."""
        return classify(plaintext_bytes, self.largest_offloadable(), text=text)

    def hydration_fits(self, plaintext_bytes: int) -> bool:
        """Hydration reserves plaintext bytes only (D12, choice 3)."""
        return plaintext_bytes <= self.hydration_bytes


@dataclass(frozen=True, slots=True)
class HydrationLimits:
    """The store policy's hydration capacity: temporary bytes in use at once, and concurrent hydrations."""

    temporary_bytes: int
    hydrations: int


# The store policy keys of the hydration capacity (D12, choice 3); the policy schema fixes them.
_HYDRATION_KEYS = ("max_temporary_bytes", "max_hydrations")


def hydration_policy_problem(config: dict[str, object]) -> str | None:
    """Why the policy's hydration capacity is invalid, or None when it is valid or not configured."""
    values = {key: config.get(key) for key in _HYDRATION_KEYS}
    if all(value is None for value in values.values()):
        return None
    try:
        if min(int(value) for value in values.values()) > 0:  # int(None) raises TypeError.
            return None
    except (TypeError, ValueError):
        pass
    shown = ", ".join(f"{key}={value!r}" for key, value in values.items())
    return f"the media store policy needs both {' and '.join(_HYDRATION_KEYS)} as positive whole numbers; it sets {shown}"


def hydration_limits(config: dict[str, object]) -> HydrationLimits | None:
    """The policy's hydration capacity, or None when the policy configures none or an invalid one (D12, choice 3).

    With none, offload stores an original without a reservation, as before the capacity
    existed, and nothing classifies or evicts, because no hydration could be bounded. An
    invalid capacity counts as none, so it never stops the claims of local work; the
    chart and the cellctl render are where a policy fails closed.
    """
    problem = hydration_policy_problem(config)
    if problem is not None:
        _warn_once(problem)
        return None
    if config.get("max_temporary_bytes") is None:
        return None
    return HydrationLimits(int(config["max_temporary_bytes"]), int(config["max_hydrations"]))


def growth_threshold(config: dict[str, object]) -> float | None:
    """The fraction of the volume that the cell grows at, or None when the policy sets none or an invalid one (D12)."""
    value = config.get("growth_threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        return None
    return float(value)


@functools.cache
def _warn_once(problem: str) -> None:
    # Every claim reads the policy, so the cache keeps one warning per problem in each process.
    log.warning("media hydration capacity is off: %s", problem)


def bounds_for(config: dict[str, object], vault: Path) -> Bounds | None:
    """The rule's inputs for a store policy, or None when the policy sets no hydration capacity."""
    limits = hydration_limits(config)
    if limits is None:
        return None
    bound = config.get("max_object_bytes")
    fraction = config.get("volume_fraction")
    return Bounds(
        object_bytes=None if bound is None else int(bound),
        hydration_bytes=limits.temporary_bytes,
        volume_bytes=shutil.disk_usage(vault).total,
        volume_fraction=DEFAULT_VOLUME_FRACTION if fraction is None else float(fraction),
    )
