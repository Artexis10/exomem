"""Crash-visible, file-backed budget reservations for benchmark operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
        try:
            return os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LedgerLocked("budget ledger is locked") from exc

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
            if entry.kind == "reserve":
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
