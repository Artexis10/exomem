"""Catch rehearsal false passes at the storage and stored-data boundaries."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from cellctl.capacity import GIB
from cellctl.imports import ImportRow

from cloud_rehearsal.local_import_growth import _check_gate, _check_staged
from cloud_rehearsal.report import StepFailure


@pytest.mark.parametrize("missing", ["capacity", "generation", "condition", "reservation"])
def test_recipient_cannot_pass_with_incomplete_growth_receipt(missing):
    """A recipient or prepare Job must fail the drill before every growth receipt agrees."""
    row = {"generation": 2, "observed_storage_generation": 2,
           "observed_storage_capacity_bytes": 5 * GIB}
    claim = {"status": {"capacity": {"storage": "5Gi"}, "conditions": []}}
    volume = {"status": {"physicalUUID": "fixture-physical", "reservationTargetResult": {
        "physicalUUID": "fixture-physical", "requestedBytes": 5 * GIB,
        "reservedBytes": 5 * GIB, "code": 0}}}
    if missing == "capacity":
        claim["status"]["capacity"]["storage"] = "4Gi"
    elif missing == "generation":
        row["observed_storage_generation"] = 1
    elif missing == "condition":
        claim["status"]["conditions"] = [{"type": "FileSystemResizePending", "status": "True"}]
    else:
        volume["status"]["reservationTargetResult"]["physicalUUID"] = "previous-physical"
    with pytest.raises(StepFailure, match="before the growth receipts agreed"):
        _check_gate(row, claim, volume, opened=True, required=5)


def test_done_does_not_hide_wrong_persisted_staged_counts():
    """Correct files and a successful receipt cannot excuse a broken C5 count write."""
    item = ImportRow(import_id="00000000-0000-4000-8000-000000000001", cell_id="a" * 16,
                     kind="adopt", declared_bytes=7, declared_files=1, requested_state="uploaded",
                     created_at=datetime.now(UTC), ciphertext_sha256="0" * 64, ciphertext_bytes=100,
                     observed_state="done", staged_files=1, staged_bytes=6)
    truth = {"sample.txt": {"bytes": 7, "sha256": "1" * 64}}
    observed = {"files": copy.deepcopy(truth), "receipt": {
        "request": item.request(), "result": {"ok": True, "files": 1, "bytes": 7}},
        "receipt_mode": 0o600, "receipt_directory_mode": 0o700,
        "identity_absent": True, "staging_absent": True}
    with pytest.raises(StepFailure, match="C5 staged counts differ"):
        _check_staged(item, observed, truth)
