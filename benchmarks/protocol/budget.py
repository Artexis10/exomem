"""Crash-visible, file-backed budget reservations for benchmark operations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

from .models import BudgetLedgerEntry


class BudgetExceeded(RuntimeError):
    pass


class UnpricedModelError(ValueError):
    pass


class CapImmutableError(ValueError):
    pass


class LedgerLocked(RuntimeError):
    pass


class BudgetLedger:
    """A shared cap ledger whose reservation is written before any spend."""

    def __init__(self, run_dir: Path | str, *, caps: dict[str, float] | None = None, pricing_path: Path | str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.budget_path = self.run_dir / "budget.json"
        self.ledger_path = self.run_dir / "ledger.jsonl"
        self.stop_path = self.run_dir / "STOP"
        self.lock_path = self.run_dir / ".budget.lock"
        self.pricing_path = Path(pricing_path) if pricing_path else Path(__file__).with_name("pricing.yaml")
        if self.budget_path.exists():
            existing = json.loads(self.budget_path.read_text(encoding="utf-8"))["caps"]
            if caps is not None and existing != caps:
                raise CapImmutableError("budget caps are immutable once written")
            self.caps = {key: float(value) for key, value in existing.items()}
        else:
            if caps is None:
                raise ValueError("caps are required when creating a budget ledger")
            if any(value < 0 for value in caps.values()):
                raise ValueError("budget caps must be non-negative")
            self.caps = {key: float(value) for key, value in caps.items()}
            self.budget_path.write_text(json.dumps({"caps": self.caps}, sort_keys=True) + "\n", encoding="utf-8")

    def _lock(self) -> int:
        """Acquire with bounded retry, recovering only demonstrably stale locks."""

        for attempt in range(20):
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"{os.getpid()} {time.time()}\n".encode("utf-8"))
                return descriptor
            except FileExistsError:
                if self._break_stale_lock():
                    continue
                if attempt < 19:
                    time.sleep(0.25)
        raise LedgerLocked("budget ledger is locked")

    def _break_stale_lock(self) -> bool:
        try:
            pid_text, timestamp_text = self.lock_path.read_text(encoding="utf-8").split()[:2]
            pid, timestamp = int(pid_text), float(timestamp_text)
        except (OSError, ValueError, IndexError):
            return False
        if time.time() - timestamp <= 600 or _pid_alive(pid):
            return False
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return False
        self._append(
            ts=timestamp_text, seq=max((entry.seq for entry in self._entries()), default=-1) + 1,
            actor="budget-ledger", op="stale-lock-recovery", kind="approval", units=0,
            running_total=self._running_total(), decision="stale-lock-recovered",
        )
        return True

    def _unlock(self, descriptor: int) -> None:
        os.close(descriptor)
        self.lock_path.unlink(missing_ok=True)

    def _entries(self) -> list[BudgetLedgerEntry]:
        if not self.ledger_path.exists():
            return []
        return [BudgetLedgerEntry.model_validate_json(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines() if line]

    def _running_total(self) -> float:
        total = 0.0
        for entry in self._entries():
            if entry.kind == "reserve" and entry.decision != "refused-cap":
                total += entry.units
            elif entry.kind == "release":
                total -= entry.units
        return max(0.0, total)

    def _append(self, *, ts: str, seq: int, actor: str, op: str, kind: str, units: float, running_total: float, decision: str) -> BudgetLedgerEntry:
        entry = BudgetLedgerEntry(ts=ts, seq=seq, actor=actor, op=op, kind=kind, units=units, running_total=running_total, decision=decision)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(entry.model_dump_json() + "\n")
        return entry
    def _require_priced(self, model_id: str) -> None:
        pricing = yaml.safe_load(self.pricing_path.read_text(encoding="utf-8")) or {}
        item = (pricing.get("models") or {}).get(model_id)
        if not item or any(value is None for value in item.values() if isinstance(value, (int, float)) or value is None):
            raise UnpricedModelError(f"model or operation {model_id!r} has no usable price")

    def reserve(self, *, ts: str, seq: int, actor: str, op: str, units: float, cap: str = "usd", model_id: str | None = None) -> BudgetLedgerEntry:
        if units < 0:
            raise ValueError("reservation units must be non-negative")
        if model_id is not None:
            self._require_priced(model_id)
        descriptor = self._lock()
        try:
            if self.stop_path.exists():
                raise BudgetExceeded("STOP sentinel already exists")
            if cap not in self.caps:
                raise ValueError(f"unknown budget cap {cap!r}")
            total = self._running_total()
            if total + units > self.caps[cap]:
                self._append(ts=ts, seq=seq, actor=actor, op=op, kind="reserve", units=units, running_total=total, decision="refused-cap")
                self.stop_path.write_text("budget exceeded\n", encoding="utf-8")
                raise BudgetExceeded("reservation exceeds approved cap")
            return self._append(ts=ts, seq=seq, actor=actor, op=op, kind="reserve", units=units, running_total=total + units, decision="approved")
        finally:
            self._unlock(descriptor)

    def commit(self, *, ts: str, seq: int, actor: str, op: str, units: float) -> BudgetLedgerEntry:
        descriptor = self._lock()
        try:
            return self._append(ts=ts, seq=seq, actor=actor, op=op, kind="commit", units=units, running_total=self._running_total(), decision="recorded")
        finally:
            self._unlock(descriptor)

    def release(self, *, ts: str, seq: int, actor: str, op: str, units: float) -> BudgetLedgerEntry:
        descriptor = self._lock()
        try:
            total = self._running_total()
            if units > total:
                raise ValueError("cannot release more than reserved")
            return self._append(ts=ts, seq=seq, actor=actor, op=op, kind="release", units=units, running_total=total - units, decision="released")
        finally:
            self._unlock(descriptor)

    def approve(self, *, ts: str, seq: int, actor: str, op: str, units: float = 0) -> BudgetLedgerEntry:
        descriptor = self._lock()
        try:
            return self._append(ts=ts, seq=seq, actor=actor, op=op, kind="approval", units=units, running_total=self._running_total(), decision="approved")
        finally:
            self._unlock(descriptor)


def _pid_alive(pid: int) -> bool:
    """Whether the process that wrote a lock is still running.

    `os.kill(pid, 0)` is a POSIX liveness probe and nothing like one on
    Windows, where Python emulates `os.kill` with `TerminateProcess`: for a
    dead pid it raises `OSError` with WinError 87 rather than
    `ProcessLookupError`, so the stale-lock breaker crashed and the ledger
    stayed locked forever -- and for a *live* pid it would have killed the
    process it was asking about.

    Mirrors `exomem.media_jobs.pid_alive`, deliberately rather than by
    import: this harness carries no dependency on the package under
    measurement.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Ask the kernel for the process state; never signal it."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means the process exists and is not ours to inspect.
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
