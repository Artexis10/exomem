"""The server node prunes K3s's records of S3 etcd snapshots the bucket has expired."""

from __future__ import annotations

import datetime
import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K3S_ROLE = ROOT / "infra/ansible/roles/k3s"
SCRIPT = K3S_ROLE / "files/exomem-prune-etcd-snapshot-records.py"


def _script():
    spec = importlib.util.spec_from_file_location("prune_etcd_snapshot_records", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(name: str, created: str, *, s3: bool = True, ready: bool = True) -> dict:
    spec = {"snapshotName": name, "location": "s3://bucket/" + name if s3 else "file:///" + name}
    if s3:
        spec["s3"] = {"bucket": "bucket"}
    return {"metadata": {"name": name, "creationTimestamp": created}, "spec": spec, "status": {"readyToUse": ready}}


def test_only_expired_or_failed_s3_records_are_selected() -> None:
    now = datetime.datetime(2026, 10, 6, tzinfo=datetime.timezone.utc)
    records = [
        _record("s3-expired", "2026-09-01T00:00:00Z"),
        _record("s3-current", "2026-10-01T00:00:00Z"),
        _record("s3-failed-upload", "2026-10-04T00:00:00Z", ready=False),
        _record("s3-uploading-now", "2026-10-05T23:30:00Z", ready=False),
        _record("local-old", "2026-07-30T00:00:00Z", s3=False),
    ]
    assert _script().expired(records, now, max_age_days=31) == ["s3-expired", "s3-failed-upload"]


def test_record_window_matches_the_etcd_bucket_lifecycle() -> None:
    # The bucket deletes a snapshot after hide + delete days; a record older than
    # that names an object that no longer exists. Keep the two in step.
    storage = (ROOT / "infra/terraform/durability/storage.tf").read_text(encoding="utf-8")
    bucket = storage.split('resource "b2_bucket" "etcd_snapshot"', 1)[1].split("\nresource ", 1)[0]
    hide = int(re.search(r"days_from_uploading_to_hiding\s*=\s*(\d+)", bucket).group(1))
    delete = int(re.search(r"days_from_hiding_to_deleting\s*=\s*(\d+)", bucket).group(1))
    defaults = yaml.safe_load((K3S_ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert defaults["k3s_etcd_s3_record_max_age_days"] == hide + delete
