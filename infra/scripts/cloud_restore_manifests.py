"""Restore resources for the Cloud operator procedures, rendered by cellctl.

The export runbook (docs/runbooks/cloud-operator-export.md), the restore
runbook (docs/runbooks/cloud-operator-restore.md) and
infra/scripts/cloud_restore.sh all render through this file. It uses cellctl's
own `render_cell_manifests` and `render_restore_job` from the same release
tree, so a restore applies what that release's cellctl would. It reaches the
cluster through `kubectl --context CONTEXT`, takes the source Secret into
process memory only and never prints it.

    scratch                restore a snapshot into a new scratch namespace beside the cell
    in-place               restore a snapshot onto the stopped cell's own volume
    idle                   exit 0 only when the cell runs no Job and carries no hold
    outside-backup-window  exit 0 only outside the live cellctl's nightly backup window
    backup-paths           print the paths a backup covers and a restore rewrites
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import ipaddress
import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# The renderer comes from the same release tree as this file, never from an
# installed cellctl, so one reviewed commit fixes both.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cellctl" / "src"))

from cellctl.backup_window import (  # noqa: E402
    DEFAULT_BACKUP_WINDOW,
    parse_backup_window,
    within_backup_window,
)
from cellctl.manifests import (  # noqa: E402
    BACKUP_PATHS,
    HOLD_ANNOTATION,
    SNAPSHOT_ID_RE,
    CellManifestSpec,
    namespace_name,
    render_cell_manifests,
    render_restore_job,
)
from cellctl.storage_config import LEGACY_CLASS  # noqa: E402

CELL_ID_RE = re.compile(r"[a-z2-7]{16}")  # shape check: a cell ID is 16 base32 characters
DIGEST_IMAGE_RE = re.compile(r"@sha256:[a-f0-9]{64}$")
RUNTIME_CONTAINER = "exomem"  # the runtime container cellctl's render_statefulset names
CONTROL_NAMESPACE, CELLCTL_DEPLOYMENT = "exomem-cloud", "cellctl"
# The cell documents a scratch restore needs: the volume, the credentials the
# Job reads, and the policies that confine it. Never the StatefulSet, Service
# or quota, so the scratch namespace can never start a runtime.
SCRATCH_DOCUMENTS = {
    ("NetworkPolicy", "default-deny"),
    ("NetworkPolicy", "job-egress"),
    ("Secret", "cell-credentials"),
    ("PersistentVolumeClaim", "cell-data"),
}
# Pod phases in which a pod no longer runs and so no longer uses the volume.
FINISHED_PHASES = {"Succeeded", "Failed"}


@dataclass(frozen=True)
class PlatformSettings:
    """What the live cellctl Deployment hands its own backup and restore Jobs."""

    bucket: str
    endpoint: str
    job_egress_except: tuple[str, ...]


def _kubectl(context: str, *args: str, payload: str | None = None, quiet: bool = False) -> str:
    result = subprocess.run(
        ["kubectl", "--context", context, *args], input=payload, text=True, capture_output=True, check=False
    )
    if result.returncode:
        # A Secret apply can echo its payload in an error; say only what failed.
        detail = "" if quiet else ": " + (result.stderr.strip().splitlines() or [""])[-1]
        raise RuntimeError(f"kubectl {' '.join(args)} failed{detail}")
    return result.stdout


def _get(context: str, kind: str, name: str, namespace: str | None = None) -> dict:
    args = (["-n", namespace] if namespace else []) + ["get", kind, name, "-o", "json"]
    return json.loads(_kubectl(context, *args))


def _apply(context: str, document: dict, field_manager: str) -> None:
    _kubectl(
        context, "apply", "--server-side", f"--field-manager={field_manager}", "-f", "-",
        payload=json.dumps(document), quiet=document["kind"] == "Secret",
    )


def check_arguments(cell_id: str, snapshot: str, scratch: str | None = None) -> None:
    if not CELL_ID_RE.fullmatch(cell_id):
        raise ValueError("not a cell ID")
    if not SNAPSHOT_ID_RE.fullmatch(snapshot):
        raise ValueError("not a restic snapshot ID")
    if scratch is not None and not re.fullmatch(rf"exo-scratch-{cell_id}-[0-9a-f]{{8}}", scratch):
        raise ValueError("the scratch namespace must be exo-scratch-<cell ID>-<8 hex>")


def runtime_of(statefulset: dict) -> tuple[str, dict]:
    """The digest-pinned image and the placement of a cell's runtime pod.

    The image comes from the StatefulSet, not a running pod, so a restore also
    works for a cell whose runtime cannot start.
    """

    pod = statefulset["spec"]["template"]["spec"]
    image = next((c["image"] for c in pod["containers"] if c["name"] == RUNTIME_CONTAINER), "")
    if not DIGEST_IMAGE_RE.search(image):
        raise ValueError("the cell image is not pinned by digest")
    # cellctl spreads a cell's placement into the pod spec as exactly these
    # two Kubernetes fields (manifests.dedicated_placement and
    # reconcile.workload_for_cell); its own restore Job carries the same.
    placement = {key: pod[key] for key in ("nodeSelector", "tolerations") if pod.get(key)}
    return image, placement


def scratch_documents(
    *, cell_id: str, scratch: str, snapshot: str, image: str, storage_gib: int,
    credential: dict[str, str], settings: PlatformSettings, started_at: str,
) -> list[dict]:
    """The scratch namespace's documents and the restore Job, in apply order.

    The namespace carries `exomem.io/scratch-of` and no `exomem.io/cloud-cell`
    label, so cellctl never treats it as a cell.
    """

    spec = CellManifestSpec(
        cell_id=cell_id, image=image, replicas=0, read_only=False,
        storage_gib=storage_gib, bearer_current=secrets.token_urlsafe(32),
        backup_password=credential["backup-password"],
        b2_key_id=credential["b2-key-id"], b2_key_secret=credential["b2-key-secret"],
        hold_kind="restore", hold_started_at=started_at,
        job_egress_except=settings.job_egress_except,
    )
    documents = []
    for document in render_cell_manifests(spec):
        if document["kind"] == "Namespace":
            document["metadata"]["name"] = scratch
            labels = document["metadata"]["labels"]
            labels.pop("exomem.io/cloud-cell")
            labels["exomem.io/scratch-of"] = cell_id
            documents.append(document)
            continue
        document["metadata"]["namespace"] = scratch
        if (document["kind"], document["metadata"]["name"]) in SCRATCH_DOCUMENTS:
            if document["kind"] == "Secret":
                document["data"].pop("cell-token")
            documents.append(document)
    if len(documents) != len(SCRATCH_DOCUMENTS) + 1 or documents[0]["kind"] != "Namespace":
        raise RuntimeError("cellctl no longer renders the documents a scratch restore needs")
    job = render_restore_job(spec, bucket_name=settings.bucket, endpoint=settings.endpoint, snapshot_id=snapshot)
    job["metadata"]["namespace"] = scratch
    return [*documents, job]


def in_place_job(
    *, cell_id: str, statefulset: dict, snapshot: str, settings: PlatformSettings, started_at: str,
) -> dict:
    """The restore Job for the cell's own namespace and volume, placed where
    the cell's runtime runs. The Job reads the cell's Secret by reference, so
    no credential passes through here."""

    image, placement = runtime_of(statefulset)
    spec = CellManifestSpec(
        cell_id=cell_id, image=image, replicas=0, read_only=False, placement=placement,
        hold_kind="restore", hold_started_at=started_at,
    )
    return render_restore_job(spec, bucket_name=settings.bucket, endpoint=settings.endpoint, snapshot_id=snapshot)


def _cellctl(context: str) -> dict:
    return _get(context, "deployment", CELLCTL_DEPLOYMENT, CONTROL_NAMESPACE)


def _cellctl_env(deployment: dict) -> dict[str, str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}


def backup_window_of(deployment: dict) -> tuple[int, int]:
    """The window the live cellctl backs up in, as cellctl's main reads it."""

    raw = _cellctl_env(deployment).get("CELLCTL_BACKUP_WINDOW")
    return parse_backup_window(raw) if raw is not None else DEFAULT_BACKUP_WINDOW


def platform_settings(deployment: dict) -> PlatformSettings:
    env = _cellctl_env(deployment)
    bucket, endpoint = env["CELLCTL_B2_BUCKET_NAME"], env["CELLCTL_B2_ENDPOINT"]
    if not bucket or urlparse(endpoint).scheme != "https" or not urlparse(endpoint).hostname:
        raise ValueError("cellctl's object store settings are incomplete")
    except_cidrs = tuple(env["CELLCTL_JOB_EGRESS_EXCEPT"].split(","))
    for cidr in except_cidrs:
        ipaddress.ip_network(cidr)
    return PlatformSettings(bucket=bucket, endpoint=endpoint, job_egress_except=except_cidrs)


def _check_cell_namespace(context: str, cell_id: str) -> str:
    source = namespace_name(cell_id)
    if _get(context, "namespace", source)["metadata"]["labels"].get("exomem.io/cloud-cell") != cell_id:
        raise ValueError(f"{source} is not the namespace of cell {cell_id}")
    return source


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def apply_scratch(context: str, cell_id: str, snapshot: str, scratch: str, field_manager: str) -> None:
    check_arguments(cell_id, snapshot, scratch)
    if _kubectl(context, "get", "namespace", scratch, "--ignore-not-found", "-o", "name").strip():
        raise ValueError(f"{scratch} already exists")
    source = _check_cell_namespace(context, cell_id)
    pvc = _get(context, "pvc", "cell-data", source)
    if pvc["spec"]["storageClassName"] != LEGACY_CLASS:
        raise ValueError(f"the cell's volume is not in the {LEGACY_CLASS} class")
    size = pvc["spec"]["resources"]["requests"]["storage"]
    if not re.fullmatch(r"[1-9][0-9]*Gi", size):
        raise ValueError("the cell's volume size is not whole GiB")
    image, _ = runtime_of(_get(context, "statefulset", "cell", source))
    data = _get(context, "secret", "cell-credentials", source)["data"]
    credential = {
        key: base64.b64decode(data[key], validate=True).decode()
        for key in ("backup-password", "b2-key-id", "b2-key-secret")
    }
    documents = scratch_documents(
        cell_id=cell_id, scratch=scratch, snapshot=snapshot, image=image, storage_gib=int(size[:-2]),
        credential=credential, settings=platform_settings(_cellctl(context)), started_at=_now(),
    )
    for document in documents:
        _apply(context, document, field_manager)
    print(f"scratch={scratch} restore_job={documents[-1]['metadata']['name']} image={image}")


def apply_in_place(context: str, cell_id: str, snapshot: str, field_manager: str) -> None:
    check_arguments(cell_id, snapshot)
    source = _check_cell_namespace(context, cell_id)
    statefulset = _get(context, "statefulset", "cell", source)
    deployment = _cellctl(context)
    # restic's --delete under a running runtime would rewrite a live vault, and
    # a running cellctl would start the runtime again mid-restore. Refuse
    # unless both are scaled to zero and no pod still runs beside the volume.
    if statefulset["spec"].get("replicas") != 0 or deployment["spec"].get("replicas") != 0:
        raise ValueError("the cell and cellctl must both be scaled to zero")
    pods = json.loads(_kubectl(context, "-n", source, "get", "pods", "-o", "json"))["items"]
    if any(pod["status"].get("phase") not in FINISHED_PHASES for pod in pods):
        raise ValueError("a pod still runs in the cell namespace")
    job = in_place_job(
        cell_id=cell_id, statefulset=statefulset, snapshot=snapshot,
        settings=platform_settings(deployment), started_at=_now(),
    )
    _apply(context, job, field_manager)
    print(job["metadata"]["name"])


def check_idle(context: str, cell_id: str) -> None:
    if not CELL_ID_RE.fullmatch(cell_id):
        raise ValueError("not a cell ID")
    source = _check_cell_namespace(context, cell_id)
    annotations = _get(context, "statefulset", "cell", source)["metadata"].get("annotations") or {}
    if HOLD_ANNOTATION in annotations:
        raise ValueError(f"the cell carries a {annotations[HOLD_ANNOTATION]} hold")
    jobs = json.loads(_kubectl(context, "-n", source, "get", "jobs", "-o", "json"))["items"]
    if any(job.get("status", {}).get("active") for job in jobs):
        raise ValueError("a Job is running in the cell namespace")


def check_outside_backup_window(context: str) -> None:
    start, end = backup_window_of(_cellctl(context))
    if within_backup_window(dt.datetime.now(dt.UTC).hour, (start, end)):
        raise ValueError(f"inside cellctl's backup window, {start:02d}:00-{end:02d}:00 UTC")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scratch", "in-place", "idle", "outside-backup-window"):
        command = commands.add_parser(name)
        command.add_argument("--context", required=True)
        if name == "outside-backup-window":
            continue
        command.add_argument("--cell-id", required=True)
        if name != "idle":
            command.add_argument("--snapshot", required=True)
            command.add_argument("--field-manager", required=True)
        if name == "scratch":
            command.add_argument("--scratch", required=True)
    commands.add_parser("backup-paths", help="print what a restore rewrites, one line, space-separated")
    args = parser.parse_args(argv)
    try:
        if args.command == "scratch":
            apply_scratch(args.context, args.cell_id, args.snapshot, args.scratch, args.field_manager)
        elif args.command == "in-place":
            apply_in_place(args.context, args.cell_id, args.snapshot, args.field_manager)
        elif args.command == "idle":
            check_idle(args.context, args.cell_id)
        elif args.command == "outside-backup-window":
            check_outside_backup_window(args.context)
        else:
            print(" ".join(BACKUP_PATHS))
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"{args.command}: {error}; stop", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
