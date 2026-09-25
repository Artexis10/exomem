"""Tests for the D8 backup/restore Job manifests (task 3.6)."""

from __future__ import annotations

import re

import pytest

from cellctl.manifests import (
    RUNTIME_GID,
    RUNTIME_UID,
    hold_job_name,
    render_backup_job,
    render_restore_job,
)
from tests.test_manifests import _spec  # reuse the fixture builder

ENDPOINT = "https://s3.us-west-002.backblazeb2.com"
HOLD_STARTED_AT = "2026-01-01T00:00:00+00:00"
SNAPSHOT_ID = "a" * 64
DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _held_spec(**overrides):
    # A Job only ever runs inside a hold; its name derives from the hold start.
    return _spec(**{"hold_kind": "backup", "hold_started_at": HOLD_STARTED_AT, **overrides})


def test_backup_job_runs_as_uid_10001_with_fsgroup() -> None:
    job = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["fsGroup"] == RUNTIME_GID
    container = pod_spec["containers"][0]
    assert container["securityContext"]["runAsUser"] == RUNTIME_UID


def test_backup_job_mounts_the_volume_read_only_with_a_cache_emptydir() -> None:
    job = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    pod_spec = job["spec"]["template"]["spec"]
    data_volume = next(v for v in pod_spec["volumes"] if v["name"] == "data")
    assert data_volume["persistentVolumeClaim"]["readOnly"] is True
    cache_volume = next(v for v in pod_spec["volumes"] if v["name"] == "cache")
    assert "emptyDir" in cache_volume


def test_backup_job_backs_up_vault_and_host_with_retention() -> None:
    job = render_backup_job(
        _held_spec(cell_id="aaaaaaaaaaaaaaaa"), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    joined = " ".join(container["command"])
    repository = next(e["value"] for e in container["env"] if e["name"] == "RESTIC_REPOSITORY")
    assert repository.endswith("/cells/aaaaaaaaaaaaaaaa")
    assert "/data/vault" in joined and "/data/host" in joined
    assert "--keep-daily 7" in joined
    assert "--keep-weekly 4" in joined


def test_backup_job_uses_the_s3_compatible_restic_repo() -> None:
    # D8 amendment: restic reaches B2 through its S3-compatible endpoint, not
    # the native "b2:" backend.
    job = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    container = job["spec"]["template"]["spec"]["containers"][0]
    repository = next(e["value"] for e in container["env"] if e["name"] == "RESTIC_REPOSITORY")
    assert repository.startswith(f"s3:{ENDPOINT}/exomem-cloud-backups/cells/")
    assert "b2:" not in " ".join(container["command"]) + repository
    env_names = {e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= env_names
    assert "B2_ACCOUNT_ID" not in env_names and "B2_ACCOUNT_KEY" not in env_names


def test_backup_job_writes_the_real_snapshot_id_to_its_termination_message() -> None:
    # D8 amendment: cellctl reads the real restic snapshot id from the Job
    # pod's termination message, never a hardcoded "latest".
    job = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    joined = " ".join(job["spec"]["template"]["spec"]["containers"][0]["command"])
    assert "snapshot_id" in joined
    assert "/dev/termination-log" in joined


def test_backup_job_has_a_bounded_deadline() -> None:
    job = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    assert job["spec"]["activeDeadlineSeconds"] == 900


def test_restore_job_uses_delete_and_writes_the_volume() -> None:
    job = render_restore_job(
        _held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID
    )
    pod_spec = job["spec"]["template"]["spec"]
    data_volume = next(v for v in pod_spec["volumes"] if v["name"] == "data")
    assert data_volume["persistentVolumeClaim"]["readOnly"] is False
    command = " ".join(pod_spec["containers"][0]["command"])
    assert f"restore {SNAPSHOT_ID}" in command
    assert "--delete" in command
    # Regression: restic (0.17+) rejects `--delete` with no scoping filter
    # ("must be combined with an include or exclude filter") -- the restore
    # Job failed every single time live in 3.10 until this was added.
    assert "--include /data" in command


def test_restore_job_runs_as_uid_10001() -> None:
    job = render_restore_job(
        _held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["runAsUser"] == RUNTIME_UID


def test_job_pods_carry_the_job_kind_label_for_the_egress_policy() -> None:
    backup = render_backup_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    restore = render_restore_job(
        _held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID
    )
    assert backup["spec"]["template"]["metadata"]["labels"]["exomem.io/cell-job"] == "backup"
    assert restore["spec"]["template"]["metadata"]["labels"]["exomem.io/cell-job"] == "restore"


def test_each_hold_gets_its_own_job_name() -> None:
    # Regression: under a fixed name, a new hold's apply was a no-op against
    # the previous hold's finished (TTL-retained) Job, and cellctl read that
    # stale result back as the new hold's backup/restore outcome.
    first = _held_spec(hold_started_at="2026-01-01T00:00:00+00:00")
    second = _held_spec(hold_started_at="2026-01-01T00:04:00+00:00")
    backup_names = {
        render_backup_job(spec, bucket_name="b", endpoint=ENDPOINT)["metadata"]["name"]
        for spec in (first, second)
    }
    restore_names = {
        render_restore_job(spec, bucket_name="b", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID)["metadata"]["name"]
        for spec in (first, second)
    }
    assert len(backup_names) == 2
    assert len(restore_names) == 2
    assert not backup_names & restore_names


def test_job_name_is_stable_for_one_hold_and_dns_safe() -> None:
    # Every reconcile pass within one hold re-renders the Job; the name must
    # not move, or each pass would start a fresh Job.
    spec = _held_spec()
    backup = render_backup_job(spec, bucket_name="b", endpoint=ENDPOINT)["metadata"]["name"]
    again = render_backup_job(spec, bucket_name="b", endpoint=ENDPOINT)["metadata"]["name"]
    restore = render_restore_job(spec, bucket_name="b", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID)["metadata"]["name"]
    assert backup == again == hold_job_name("cell-backup", HOLD_STARTED_AT)
    assert restore == hold_job_name("cell-restore", HOLD_STARTED_AT)
    for name in (backup, restore):
        # 63, not 253: the Job controller copies the name into the pods'
        # batch.kubernetes.io/job-name label, which is capped at 63.
        assert DNS_1123_LABEL.match(name) and len(name) <= 63, name
        assert HOLD_STARTED_AT not in name


def test_a_job_is_never_rendered_outside_a_hold() -> None:
    with pytest.raises(ValueError):
        render_backup_job(_spec(), bucket_name="b", endpoint=ENDPOINT)
    with pytest.raises(ValueError):
        render_restore_job(_spec(), bucket_name="b", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID)


def test_restore_job_rejects_a_snapshot_id_that_is_not_real_restic_hex() -> None:
    # SR-L12: validated in Python before it can ever reach the shell string,
    # not merely trusted from whatever the caller happens to pass.
    for bad in ("snap-1", "latest", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "; rm -rf /"):
        with pytest.raises(ValueError):
            render_restore_job(_held_spec(), bucket_name="b", endpoint=ENDPOINT, snapshot_id=bad)


def test_restore_and_backup_are_scoped_to_vault_and_host_leaving_the_volume_root_alone() -> None:
    # D6 step 4.3 / D8: `--include /data` let `--delete` reach lost+found,
    # which restic 0.19.1 running as UID 10001 cannot touch (exit 1). The
    # restore and the backup cover exactly the same two paths.
    spec = _held_spec(cell_id="aaaaaaaaaaaaaaaa")
    repo = f"s3:{ENDPOINT}/exomem-cloud-backups/cells/aaaaaaaaaaaaaaaa"
    restore = render_restore_job(spec, bucket_name="exomem-cloud-backups", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID)
    restore_container = restore["spec"]["template"]["spec"]["containers"][0]
    assert restore_container["command"] == [
        "sh",
        "-c",
        f"restic restore {SNAPSHOT_ID} --target / --delete --include /data/vault --include /data/host",
    ]
    assert {"name": "RESTIC_REPOSITORY", "value": repo} in restore_container["env"]
    backup = render_backup_job(spec, bucket_name="exomem-cloud-backups", endpoint=ENDPOINT)
    script = backup["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "restic backup --json /data/vault /data/host > /tmp/backup.json || exit 1" in script.split(" && ")


def test_the_repository_never_reaches_the_job_shell() -> None:
    # The endpoint and bucket are chart values; they reach restic through
    # RESTIC_REPOSITORY, never through the `sh -c` string.
    endpoint = "https://s3.example/$(touch /tmp/owned);x"
    spec = _held_spec(cell_id="aaaaaaaaaaaaaaaa")
    for job in (
        render_backup_job(spec, bucket_name="b`id`", endpoint=endpoint),
        render_restore_job(spec, bucket_name="b`id`", endpoint=endpoint, snapshot_id=SNAPSHOT_ID),
    ):
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert "s3.example" not in container["command"][2] and "`id`" not in container["command"][2]
        assert {"name": "RESTIC_REPOSITORY", "value": f"s3:{endpoint}/b`id`/cells/aaaaaaaaaaaaaaaa"} in container["env"]


def test_a_snapshot_id_with_a_trailing_newline_never_reaches_the_restore_shell() -> None:
    with pytest.raises(ValueError):
        render_restore_job(_held_spec(), bucket_name="exomem-cloud-backups", endpoint=ENDPOINT, snapshot_id=SNAPSHOT_ID + "\n")
