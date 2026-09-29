"""A guarded append or an inspect must not do per-item governance work.

The Records write path used to reload the governance policy and rescan the
tombstone generation once per item, so one append cost O(N) filesystem probes.
These are structural counts, not latency thresholds: the number of policy loads
and tombstone scans in one operation must be the same at 3 items as at 12.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from record_fixtures import ledger_item, setup_ledger_collection

from exomem import record_governance, records
from exomem.governance import lifecycle
from exomem.governance import policy as policy_module

COLLECTION = "Knowledge Base/Records/Publications/_collection.md"


def _seed(vault: Path, count: int) -> None:
    setup_ledger_collection(vault)
    for index in range(count):
        records.append_record(
            vault,
            COLLECTION,
            item=ledger_item(slug=f"seed-{index:03d}"),
            why="seed",
        )


class _Counts:
    def __init__(self) -> None:
        self.policy_loads = 0
        self.tombstone_scans = 0


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> _Counts:
    seen = _Counts()
    real_load = policy_module.load
    real_scan = lifecycle._tombstone_generation

    def load(*args, **kwargs):
        seen.policy_loads += 1
        return real_load(*args, **kwargs)

    def scan(*args, **kwargs):
        seen.tombstone_scans += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(policy_module, "load", load)
    monkeypatch.setattr(lifecycle, "_tombstone_generation", scan)
    return seen


def _measure(vault: Path, counts: _Counts, operation) -> tuple[int, int]:
    counts.policy_loads = 0
    counts.tombstone_scans = 0
    operation()
    return counts.policy_loads, counts.tombstone_scans


def _append_counts(tmp_path: Path, counts: _Counts, size: int) -> tuple[int, int]:
    vault = tmp_path / f"append-{size}"
    vault.mkdir()
    _seed(vault, size)
    return _measure(
        vault,
        counts,
        lambda: records.append_record(
            vault, COLLECTION, item=ledger_item(slug="measured"), why="measure"
        ),
    )


def _inspect_counts(tmp_path: Path, counts: _Counts, size: int) -> tuple[int, int]:
    vault = tmp_path / f"inspect-{size}"
    vault.mkdir()
    _seed(vault, size)
    return _measure(
        vault, counts, lambda: record_governance.inspect_collection(vault, COLLECTION)
    )


def test_append_governance_work_does_not_grow_with_item_count(
    tmp_path: Path, counts: _Counts
) -> None:
    small = _append_counts(tmp_path, counts, 3)
    large = _append_counts(tmp_path, counts, 12)
    assert large == small, f"per-append governance work grew with items: {small} -> {large}"


def test_inspect_governance_work_does_not_grow_with_item_count(
    tmp_path: Path, counts: _Counts
) -> None:
    small = _inspect_counts(tmp_path, counts, 3)
    large = _inspect_counts(tmp_path, counts, 12)
    assert large == small, f"per-inspect governance work grew with items: {small} -> {large}"
