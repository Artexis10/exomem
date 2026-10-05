#!/usr/bin/env python3
"""Delete K3s's records of S3 etcd snapshots that the bucket has already expired.

K3s keeps an ETCDSnapshotFile object for every snapshot it uploads and prunes
those objects only by listing the bucket. It cannot list this one: K3s lists
with the bare folder as prefix (`etcd-snapshot`), while the node's write-only
key is restricted to `etcd-snapshot/`, so B2 answers `not entitled`. The
bucket's lifecycle rule deletes the snapshots themselves, so without this the
records grow by 48 a day and point at objects that no longer exist.

Deleting a record never touches S3: K3s's removal handler only drops the
snapshot's ConfigMap entry.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess

KUBECTL = ("/usr/local/bin/k3s", "kubectl")
RESOURCE = "etcdsnapshotfiles.k3s.cattle.io"
# An upload still in flight is not ready either; give it a day before calling it failed.
FAILED_UPLOAD_GRACE = datetime.timedelta(days=1)


def expired(records: list[dict], now: datetime.datetime, max_age_days: int) -> list[str]:
    """Names of S3 records older than the bucket keeps, or failed uploads."""
    names = []
    for record in records:
        if not record.get("spec", {}).get("s3"):
            continue
        created = datetime.datetime.fromisoformat(record["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
        age = now - created
        ready = record.get("status", {}).get("readyToUse") is True
        if age > datetime.timedelta(days=max_age_days) or (not ready and age > FAILED_UPLOAD_GRACE):
            names.append(record["metadata"]["name"])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-days", type=int, required=True)
    args = parser.parse_args()
    listing = subprocess.run(
        [*KUBECTL, "get", RESOURCE, "-o", "json"], check=True, capture_output=True, text=True
    ).stdout
    names = expired(json.loads(listing)["items"], datetime.datetime.now(datetime.timezone.utc), args.max_age_days)
    for start in range(0, len(names), 100):
        subprocess.run([*KUBECTL, "delete", RESOURCE, "--ignore-not-found", "--wait=false", *names[start : start + 100]], check=True)
    print(f"deleted {len(names)} expired S3 etcd snapshot records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
