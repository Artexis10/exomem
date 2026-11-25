"""The Cloud restore tools' own logic: the backup digest the drill and the
in-place restore trust for their verdict, the move and rollback of a cell's
contents, and the restore resources they render with cellctl
(infra/scripts/cloud_vault_digest.py, cloud_restore_volume.sh and
cloud_restore_manifests.py, driven by infra/scripts/cloud_restore.sh).

The digest runs exactly as the tools run it: as a script, writing a hash list
to a file and comparing two lists."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = ROOT / "infra" / "scripts" / "cloud_vault_digest.py"
MANIFESTS = ROOT / "infra" / "scripts" / "cloud_restore_manifests.py"
VOLUME = ROOT / "infra" / "scripts" / "cloud_restore_volume.sh"
CELL_ID = "abcdefghijklmnop"
SNAPSHOT = "0123456789abcdef" * 4
IMAGE = "ghcr.io/example/exomem@sha256:" + "a" * 64

posix_trees = pytest.mark.skipif(
    os.name == "nt", reason="the restore tools run on the Linux node; these trees use POSIX names and symlinks"
)


def _digest(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(DIGEST), *map(str, args)], capture_output=True, text=True, check=False
    )


def _hash(root: Path, out: Path) -> Path:
    result = _digest("hash", root)
    assert result.returncode == 0, result.stderr
    out.write_text(result.stdout, encoding="utf-8")
    return out


def _vault(root: Path) -> Path:
    (root / "Knowledge Base" / "Projects").mkdir(parents=True)
    (root / "Knowledge Base" / "Projects" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (root / "Knowledge Base" / "inbox.md").write_text("first\n", encoding="utf-8")
    (root / "attachments").mkdir()
    (root / "attachments" / "scan.pdf").write_bytes(bytes(range(256)) * 64)
    return root


@posix_trees
def test_a_restore_identical_to_its_reference_passes(tmp_path: Path) -> None:
    reference = _vault(tmp_path / "reference")
    # Names a line-based list would split or mangle.
    (reference / "Knowledge Base" / "two\nlines.md").write_text("x\n", encoding="utf-8")
    (reference / os.fsdecode(b"latin-\xe9.md")).write_text("y\n", encoding="utf-8")
    restored = tmp_path / "restored"
    shutil.copytree(reference, restored, symlinks=True)

    result = _digest(
        "compare", _hash(restored, tmp_path / "r.list"), _hash(reference, tmp_path / "f.list"), tmp_path
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "restored=5 reference=5 identical=5 differ=0 only_restored=0 only_reference=0"
    )


@posix_trees
@pytest.mark.parametrize(
    ("change", "listed_in"),
    [
        ("edit", "differ.txt"),
        ("remove", "only_reference.txt"),
        ("add", "only_restored.txt"),
        ("empty", None),
    ],
)
def test_a_restore_that_differs_from_its_reference_fails(tmp_path: Path, change: str, listed_in: str | None) -> None:
    reference = _vault(tmp_path / "reference")
    restored = tmp_path / "restored"
    shutil.copytree(reference, restored)
    inbox = restored / "Knowledge Base" / "inbox.md"
    if change == "edit":
        inbox.write_text("second\n", encoding="utf-8")
    elif change == "remove":
        inbox.unlink()
    elif change == "add":
        (restored / "Knowledge Base" / "stray.md").write_text("z\n", encoding="utf-8")
    else:
        # Two empty vaults agree on every file, and still prove nothing.
        shutil.rmtree(reference)
        shutil.rmtree(restored)
        reference.mkdir()
        restored.mkdir()
    out = tmp_path / "names"
    out.mkdir()

    result = _digest("compare", _hash(restored, tmp_path / "r.list"), _hash(reference, tmp_path / "f.list"), out)

    assert result.returncode == 3, result.stdout + result.stderr
    # The run log carries counts; the names stay in the private hash directory.
    assert ".md" not in result.stdout
    if listed_in:
        assert (out / listed_in).read_text(encoding="utf-8").count(".md") == 1


@posix_trees
@pytest.mark.parametrize("empty_host", [False, True])
def test_scratch_and_in_place_restores_match_both_backup_roots(tmp_path: Path, empty_host: bool) -> None:
    reference = tmp_path / "reference"
    _vault(reference / "vault")
    (reference / "host").mkdir()
    if not empty_host:
        (reference / "host" / "custody.db").write_bytes(b"committed custody")
    scratch = tmp_path / "scratch"
    in_place = tmp_path / "in-place"
    shutil.copytree(reference, scratch, symlinks=True)
    shutil.copytree(scratch, in_place, symlinks=True)

    for backup_path in _manifests().BACKUP_PATHS:
        name = Path(backup_path).name
        reference_list = _hash(reference / name, tmp_path / f"reference-{name}.list")
        scratch_list = _hash(scratch / name, tmp_path / f"scratch-{name}.list")
        in_place_list = _hash(in_place / name, tmp_path / f"in-place-{name}.list")
        options = ["--allow-empty"] if name == "host" else []
        for restored, expected in ((scratch_list, reference_list), (in_place_list, scratch_list)):
            result = _digest("compare", restored, expected, tmp_path, *options)
            assert result.returncode == 0, result.stdout + result.stderr
        if name == "host" and empty_host:
            assert "restored=0 reference=0 identical=0" in result.stdout


@posix_trees
@pytest.mark.parametrize("change", ["edit", "remove", "add", "retarget"])
def test_unchanged_vault_does_not_hide_changed_host_state(tmp_path: Path, change: str) -> None:
    reference = tmp_path / "reference"
    _vault(reference / "vault")
    (reference / "host").mkdir()
    (reference / "host" / "custody.db").write_bytes(b"committed custody")
    (reference / "host" / "current").symlink_to("custody.db")
    restored = tmp_path / "restored"
    shutil.copytree(reference, restored, symlinks=True)
    if change == "edit":
        (restored / "host" / "custody.db").write_bytes(b"lost custody")
    elif change == "remove":
        (restored / "host" / "custody.db").unlink()
    elif change == "add":
        (restored / "host" / "stray.db").write_bytes(b"extra state")
    else:
        (restored / "host" / "current").unlink()
        (restored / "host" / "current").symlink_to("stray.db")

    vault_result = _digest(
        "compare", _hash(restored / "vault", tmp_path / "r-vault.list"),
        _hash(reference / "vault", tmp_path / "f-vault.list"), tmp_path,
    )
    host_result = _digest(
        "compare", _hash(restored / "host", tmp_path / "r-host.list"),
        _hash(reference / "host", tmp_path / "f-host.list"), tmp_path, "--allow-empty",
    )

    assert vault_result.returncode == 0, vault_result.stderr
    assert host_result.returncode == 3, host_result.stdout + host_result.stderr
    assert "custody.db" not in host_result.stdout and "current" not in host_result.stdout


@posix_trees
@pytest.mark.parametrize("root_kind", ["missing", "symlink"])
def test_missing_or_linked_host_root_cannot_pass_as_empty(tmp_path: Path, root_kind: str) -> None:
    host = tmp_path / "host"
    if root_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private.db").write_bytes(b"outside backup roots")
        host.symlink_to(outside, target_is_directory=True)

    spellings = [str(host)]
    if root_kind == "symlink":
        spellings.extend((str(host) + "/", str(host) + "/."))
    for root in spellings:
        result = _digest("hash", root)

        assert result.returncode == 1
        assert not result.stdout
        assert "private.db" not in result.stderr


@posix_trees
def test_a_symlink_is_recorded_by_its_target_and_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("not in the vault\n", encoding="utf-8")
    reference = _vault(tmp_path / "reference")
    (reference / "copy.md").write_text("# Plan\n", encoding="utf-8")
    restored = _vault(tmp_path / "restored")
    (restored / "copy.md").symlink_to(restored / "Knowledge Base" / "Projects" / "plan.md")
    (restored / "elsewhere").symlink_to(outside, target_is_directory=True)

    restored_list = _hash(restored, tmp_path / "r.list")
    result = _digest("compare", restored_list, _hash(reference, tmp_path / "f.list"), tmp_path)

    assert result.returncode == 3
    assert "differ=1 only_restored=1" in result.stdout
    assert "secret.md" not in restored_list.read_text(encoding="utf-8")


@posix_trees
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_directory_stops_the_hash(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    locked = vault / "Knowledge Base" / "Projects"
    locked.chmod(0)
    try:
        result = _digest("hash", vault)
    finally:
        locked.chmod(0o700)

    assert result.returncode == 1
    # Only the error class reaches the log, never the path.
    assert result.stderr.strip() == "hash failed: PermissionError"


@posix_trees
@pytest.mark.parametrize("moved", [(), ("vault",), ("vault", "host")])
def test_a_rollback_brings_back_exactly_what_the_cell_held(tmp_path: Path, moved: tuple[str, ...]) -> None:
    # `moved` is how far the move got before the run stopped: not at all,
    # partway, or fully, after which the restore Job wrote part of a vault.
    data = tmp_path / "data"
    _vault(data / "vault")
    (data / "host" / ".cache").mkdir(parents=True)
    (data / "host" / ".cache" / "index").write_bytes(b"derived state")
    (data / "lost+found").mkdir()
    before = _digest("hash", data).stdout
    paths = [data / "vault", data / "host"]

    def volume(action: str, *targets: Path) -> None:
        with VOLUME.open("rb") as script:
            result = subprocess.run(
                ["sh", "-s", action, "0123abcd", *map(str, targets)], stdin=script, capture_output=True, check=False
            )
        assert result.returncode == 0, result.stderr

    if moved:
        volume("move", *(data / name for name in moved))
        if moved == ("vault", "host"):
            (data / "vault").mkdir()
            (data / "vault" / "partial.md").write_text("half a restore\n", encoding="utf-8")

    volume("rollback", *paths)

    assert _digest("hash", data).stdout == before
    assert not list(data.glob(".restore-prior-*"))


def _manifests():
    spec = importlib.util.spec_from_file_location("cloud_restore_manifests", MANIFESTS)
    module = importlib.util.module_from_spec(spec)
    # Its dataclass resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings(manifests, job_egress_except: tuple[str, ...]):
    """Two distinct stores, so a test sees which one a restore Job reaches."""
    return manifests.PlatformSettings(
        object_storage_bucket="founder-backups", object_storage_endpoint="https://s3.founder.example",
        cell_data_backup_bucket="cell-data-backups", cell_data_endpoint="https://s3.cell-data.example",
        job_egress_except=job_egress_except,
    )


def test_a_scratch_restore_never_creates_a_runtime_or_a_cell(tmp_path: Path) -> None:
    manifests = _manifests()
    scratch = f"exo-scratch-{CELL_ID}-0123abcd"

    documents = manifests.scratch_documents(
        cell_id=CELL_ID, scratch=scratch, snapshot=SNAPSHOT, image=IMAGE, storage_gib=10,
        credential={"backup-password": "p", "b2-key-id": "i", "b2-key-secret": "s"},
        settings=_settings(manifests, ("10.0.0.0/8",)),
        started_at="2026-10-09T10:00:00+00:00", storage_class="exomem-cloud-encrypted",
    )

    assert sorted(d["kind"] for d in documents) == [
        "Job", "Namespace", "NetworkPolicy", "NetworkPolicy", "PersistentVolumeClaim", "Secret",
    ]
    namespace = documents[0]
    assert namespace["metadata"]["name"] == scratch
    # cellctl reconciles every namespace labelled as a cell; the scratch one must never be.
    assert "exomem.io/cloud-cell" not in namespace["metadata"]["labels"]
    assert namespace["metadata"]["labels"]["exomem.io/scratch-of"] == CELL_ID
    assert all(d["metadata"]["namespace"] == scratch for d in documents[1:])
    secret = next(d for d in documents if d["kind"] == "Secret")
    assert "cell-token" not in secret["data"]


def test_a_scratch_restore_of_a_custodian_cell_reads_the_copied_custodian_key() -> None:
    # A cell the key custodian provisioned has no key in cell-credentials; its restore Job must
    # read a copy of the custodian's Secret in the scratch namespace, or restic cannot reach B2.
    manifests = _manifests()
    scratch = f"exo-scratch-{CELL_ID}-0123abcd"
    custodian_key = {"type": "Opaque", "metadata": {"name": "cell-object-store-key", "uid": "u-1"},
                     "data": {"keyId": "a2V5", "applicationKey": "c2VjcmV0"}}

    documents = manifests.scratch_documents(
        cell_id=CELL_ID, scratch=scratch, snapshot=SNAPSHOT, image=IMAGE, storage_gib=10,
        credential={"backup-password": "p"},
        settings=_settings(manifests, ("10.0.0.0/8",)),
        started_at="2026-10-09T10:00:00+00:00", storage_class="exomem-cloud-encrypted",
        object_store_key=custodian_key,
    )

    copied = next(d for d in documents if d["kind"] == "Secret" and d["metadata"]["name"] == "cell-object-store-key")
    assert copied["metadata"]["namespace"] == scratch
    assert copied["data"] == custodian_key["data"]
    assert "uid" not in copied["metadata"]
    job = documents[-1]
    assert job["kind"] == "Job"
    env = {e["name"]: e for c in job["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    assert env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {"name": "cell-object-store-key", "key": "keyId"}
    assert env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "cell-object-store-key"
    # The custodian's key opens only the locked cell-data bucket, where the cell's snapshots live.
    assert env["RESTIC_REPOSITORY"]["value"] == f"s3:https://s3.cell-data.example/cell-data-backups/cells/{CELL_ID}"


def test_a_scratch_volume_keeps_the_source_class_and_its_passphrase() -> None:
    # A per-cell encrypted class formats a blank volume as plain ext4 without a passphrase,
    # so the scratch copy of a customer's vault must carry the source's passphrase.
    manifests = _manifests()

    documents = manifests.scratch_documents(
        cell_id=CELL_ID, scratch=f"exo-scratch-{CELL_ID}-0123abcd", snapshot=SNAPSHOT, image=IMAGE,
        storage_gib=10, credential={"backup-password": "p", "encryption-passphrase": "vp",
                                    "b2-key-id": "i", "b2-key-secret": "s"},
        settings=_settings(manifests, ("10.0.0.0/8",)),
        started_at="2026-10-09T10:00:00+00:00", storage_class="exomem-cloud-per-cell",
    )

    claim = next(d for d in documents if d["kind"] == "PersistentVolumeClaim")
    assert claim["spec"]["storageClassName"] == "exomem-cloud-per-cell"
    secret = next(d for d in documents if d["kind"] == "Secret")
    assert secret["data"]["encryption-passphrase"] == "dnA="



@pytest.mark.parametrize(("stored_key_id", "key_secret", "repository"), [
    ("stored-id", "cell-credentials", "s3:https://s3.founder.example/founder-backups/cells/"),
    ("", "cell-object-store-key", "s3:https://s3.cell-data.example/cell-data-backups/cells/"),
])
def test_an_in_place_restore_reaches_the_store_its_cell_backs_up_to(stored_key_id, key_secret, repository) -> None:
    # An old cell's stored key opens the founder bucket; a custodian cell's key opens only the
    # locked cell-data bucket. A restore Job with the wrong pair cannot read the cell's snapshots.
    manifests = _manifests()
    statefulset = {"spec": {"replicas": 0, "template": {"spec": {"containers": [{"name": "exomem", "image": IMAGE}]}}}}

    job = manifests.in_place_job(
        cell_id=CELL_ID, statefulset=statefulset, snapshot=SNAPSHOT, settings=_settings(manifests, ()),
        started_at="2026-10-09T10:00:00+00:00", stored_key_id=stored_key_id,
    )

    env = {e["name"]: e for c in job["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    assert env["RESTIC_REPOSITORY"]["value"] == repository + CELL_ID
    assert env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == key_secret


def test_the_in_place_restore_and_its_helper_run_where_the_cell_runs() -> None:
    manifests = _manifests()
    placement = {
        "nodeSelector": {"exomem.io/dedicated-cell": CELL_ID},
        "tolerations": [{"key": "exomem.io/dedicated-cell", "operator": "Equal", "value": CELL_ID,
                         "effect": "NoSchedule"}],
    }
    statefulset = {"spec": {"replicas": 0, "template": {"spec": {
        **placement,
        "containers": [{"name": "exomem", "image": IMAGE}],
    }}}}

    job = manifests.in_place_job(
        cell_id=CELL_ID, statefulset=statefulset, snapshot=SNAPSHOT,
        settings=_settings(manifests, ()),
        started_at="2026-10-09T10:00:00+00:00",
    )

    helper = manifests.helper_pod(cell_id=CELL_ID, statefulset=statefulset, name="cell-restore-helper")
    verify = manifests.helper_pod(
        cell_id=CELL_ID, statefulset=statefulset, name="restore-verify", scratch=f"exo-scratch-{CELL_ID}-0123abcd"
    )

    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == helper["metadata"]["namespace"] == f"exo-cell-{CELL_ID}"
    assert pod["containers"][0]["image"] == helper["spec"]["containers"][0]["image"] == IMAGE
    # Without them, a dedicated or shared-profile cell's Job waits Pending while
    # the cell is down, and its helper moves the volume to another node.
    for spec in (pod, helper["spec"]):
        assert spec["nodeSelector"] == placement["nodeSelector"]
        assert spec["tolerations"] == placement["tolerations"]
    # The scratch verify pod follows the unplaced scratch Job, off the live cell's node.
    assert "nodeSelector" not in verify["spec"] and "tolerations" not in verify["spec"]
    assert verify["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] is True


@pytest.mark.parametrize("writable_pod", [False, True])
def test_in_place_restore_guards_distinguish_the_read_only_verifier(monkeypatch, capsys, writable_pod) -> None:
    manifests = _manifests()
    from cellctl.manifests import render_restic_verify_job

    statefulset = {"metadata": {"uid": "statefulset-uid"}, "spec": {"replicas": 0, "template": {"spec": {
        "containers": [{"name": "exomem", "image": IMAGE}],
    }}}}
    # A verifier admitted before an image rollback still uses its already pinned image.
    verifier_image = IMAGE.rpartition(":")[0] + ":" + "b" * 64
    spec = manifests.CellManifestSpec(cell_id=CELL_ID, image=verifier_image, replicas=1, read_only=False)
    job = render_restic_verify_job(
        spec, bucket_name="bucket", endpoint="https://s3.example.test", attempt_id="fixture-attempt",
        part=1, total=24, download_kib_per_second=8192, deadline_seconds=300, start_before=1_800_000_000,
        statefulset_uid="statefulset-uid",
    )
    job["metadata"]["uid"] = "job-uid"
    job["status"] = {"active": 1}
    template = job["spec"]["template"]
    name = job["metadata"]["name"]
    pod = {"metadata": {"name": name + "-test", "labels": {**template["metadata"]["labels"],
            "batch.kubernetes.io/job-name": name, "batch.kubernetes.io/controller-uid": "job-uid"},
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": name,
                                 "uid": "job-uid", "controller": True} ]},
           "spec": {**template["spec"], "volumes": list(template["spec"]["volumes"])}, "status": {"phase": "Running"}}
    if writable_pod:
        pod["spec"]["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": "cell-data"}})
    workloads = {"jobs": [job], "pods": [pod]}
    monkeypatch.setattr(manifests, "_kubectl", lambda _context, *args, **_kw: json.dumps({"items": workloads[args[3]]}))
    monkeypatch.setattr(manifests, "_check_cell_namespace", lambda _context, cell: manifests.namespace_name(cell))
    monkeypatch.setattr(manifests, "_get", lambda _context, kind, *_rest: {"data": {}} if kind == "secret" else statefulset)
    monkeypatch.setattr(manifests, "_cellctl", lambda _context: {"spec": {"replicas": 0}})
    monkeypatch.setattr(manifests, "platform_settings", lambda _deployment: _settings(manifests, ()))
    applied = []
    monkeypatch.setattr(manifests, "_apply", lambda _context, document, _manager: applied.append(document))

    if writable_pod:
        assert manifests.main(["idle", "--context", "fixture", "--cell-id", CELL_ID]) == 1
        with pytest.raises(ValueError, match="a pod still runs"):
            manifests.apply_in_place("fixture", CELL_ID, SNAPSHOT, "restore-manager")
        assert not applied
    else:
        assert manifests.main(["idle", "--context", "fixture", "--cell-id", CELL_ID]) == 0
        manifests.apply_in_place("fixture", CELL_ID, SNAPSHOT, "restore-manager")
        assert applied[0]["metadata"]["labels"][manifests.JOB_KIND_LABEL] == manifests.JOB_KIND_RESTORE

    # The shell treats an orphan the same way once the Job and owner reference are gone.
    workloads["jobs"] = []
    pod["metadata"].pop("ownerReferences")
    capsys.readouterr()
    assert manifests.main(["running-pods", "--context", "fixture", "--cell-id", CELL_ID]) == 0
    assert capsys.readouterr().out.splitlines() == ([pod["metadata"]["name"]] if writable_pod else [])


def test_verifier_statefulset_lookup_has_both_request_and_subprocess_deadlines(monkeypatch, capsys) -> None:
    manifests = _manifests()
    pod = {"metadata": {"name": "fixture-pod", "labels": {manifests.JOB_KIND_LABEL: manifests.JOB_KIND_RESTIC_VERIFY}},
           "status": {"phase": "Running"}}
    monkeypatch.setattr(manifests, "_list_workloads", lambda *_args: [pod])

    def timed_out(command, *, timeout, **_kw):
        assert command[-6:] == ["get", "statefulset", "cell", "-o", "json", "--request-timeout=10s"]
        assert timeout == 12
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(manifests.subprocess, "run", timed_out)

    assert manifests.main(["running-pods", "--context", "fixture", "--cell-id", CELL_ID]) == 1
    assert "timed out" in capsys.readouterr().err
