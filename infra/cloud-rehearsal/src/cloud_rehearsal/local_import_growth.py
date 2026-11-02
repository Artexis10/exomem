"""A synthetic C5 request exercises guarded local growth and real import Jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
from cellctl.capacity import GIB, quantity
from cellctl.imports import ImportRow, job_name, open_uploads
from cellctl.manifests import JOB_KIND_LABEL
from cellctl.storage.topolvm import target_reserved

from .infra import STAGING_BUCKET, docker_labels
from .native_import import CELL_PROBE, _storage
from .report import StepFailure
from .shell import run

if TYPE_CHECKING:
    from .drill import Drill
    from .report import StepRecord

# Exercise the existing CLI archive and selection code, including age, in its shipped image.
ENCRYPT = """import sys
from pathlib import Path
from exomem.cloud_import_client import _inventory, _ciphertext
from exomem.cli_progress import Progress
source = Path('/fixture/source')
progress = Progress()
plan = _inventory(source, None, include_hidden=False, use_gitignore=True, progress=progress)
_ciphertext(source, Path('/fixture/archive.age'), sys.argv[1], None, plan, progress)
"""


def _check_gate(row, claim, volume, *, opened, required):
    status = claim.get("status") or {}
    # Kubernetes fixes these two resize conditions; they are protocol tokens.
    pending = any(c.get("status") == "True" and c.get("type") in ("Resizing", "FileSystemResizePending")
                  for c in status.get("conditions") or [])
    capacity = int(quantity((status.get("capacity") or {}).get("storage", "0")))
    accepted, _ = target_reserved(volume, (volume.get("status") or {}).get("physicalUUID", ""), required * GIB)
    ready = (capacity >= required * GIB and not pending and accepted
             and (row.get("observed_storage_capacity_bytes") or 0) >= required * GIB
             and row.get("observed_storage_generation") == row["generation"])
    if opened and not ready:
        raise StepFailure("the import opened before the growth receipts agreed")
    return ready


def _check_staged(item, observed, truth):
    files, size = len(truth), sum(entry["bytes"] for entry in truth.values())
    if observed["files"] != truth or observed["receipt"]["request"] != item.request():
        raise StepFailure("the staged files or immutable receipt differ from source/C5 truth")
    if (item.declared_files, item.declared_bytes) != (files, size):
        raise StepFailure("C5 declared counts differ from the original source")
    if observed["receipt"]["result"] != {"ok": True, "files": files, "bytes": size}:
        raise StepFailure("the import receipt counts differ from source truth")
    if (item.staged_files, item.staged_bytes) != (files, size):
        raise StepFailure("C5 staged counts differ from the stored files")
    if observed["receipt_mode"] != 0o600 or observed["receipt_directory_mode"] != 0o700:
        raise StepFailure("the import receipt is not private")
    if not observed["identity_absent"] or not observed["staging_absent"]:
        raise StepFailure("temporary import identities or staging remain")


async def _app_sql(drill, query, *args):
    """Use the pinned grants for trusted fixture requests and client acknowledgements."""
    connection = await asyncpg.connect(drill.stack.postgres.dsn("substrate_app", from_host=True))
    try:
        return await connection.execute(query, *args)
    finally:
        await connection.close()


async def check_import_growth(drill: Drill, record: StepRecord) -> None:
    """Prove CSI growth precedes key creation, then verify a real staged C5 import.

    The storage requirement is synthetic. This does not exercise the HTTP admission
    calculator, authentication or Home. Volume samples cannot qualify an exact peak.
    """
    from .drill import FILESYSTEM_USE, pod_identity, serving

    if drill.provider_tag is None:
        raise StepFailure("the import-growth check requires the guarded provider")
    required = 5
    cell = await drill.add_cell("import-growth", owner=False)
    initial = await drill.wait_row(cell, lambda row: serving(row) and row.get("import_limits") is not None,
                                   timeout=900, description="the import cell to serve with published limits")
    first_pod = pod_identity(drill.cell_pod(cell))
    initial_claim = drill.kube_json("get", "pvc", "cell-data", "--namespace", cell.namespace)
    if initial["storage_gib"] != 4 or quantity(initial_claim["status"]["capacity"]["storage"]) != 4 * GIB:
        raise StepFailure("the import fixture did not start with a 4 GiB claim")
    binding = drill.claim_volume(cell)
    before_use = drill.filesystem(cell)
    import_id = str(uuid.uuid4())
    evidence = record.evidence
    evidence.update({"scope": "synthetic C5 requirement; real CSI growth, age, multipart and import Jobs",
                     "request_role": "substrate_app", "required_storage_gib": required,
                     "claim_before": initial_claim["status"], "pod_before": first_pod,
                     "binding": binding, "filesystem_before": before_use,
                     "observations": [], "observation_interval_seconds": 0.5})
    fixture = Path(tempfile.mkdtemp(prefix="import-growth-", dir=drill.stack.workdir))
    fixture.chmod(0o700)
    try:
        source = fixture / "source"
        source.mkdir(mode=0o700)
        # Invented fixture bytes; the small payload deliberately does not imply the 5 GiB requirement.
        (source / "sample.txt").write_bytes(b"A synthetic import preserves these bytes.\n" * 32768)
        truth = {path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in source.iterdir()}
        declared_bytes = sum(entry["bytes"] for entry in truth.values())
        limits = initial["import_limits"]
        if isinstance(limits, str):
            limits = json.loads(limits)
        evidence.update(source_files=truth, declared_bytes=declared_bytes, import_id=import_id)
        drill.pause_cellctl()
        try:
            connection = await asyncpg.connect(drill.stack.postgres.dsn("substrate_app", from_host=True))
            try:
                async with connection.transaction():
                    await connection.execute("UPDATE exomem_cloud_cells SET storage_gib = $2 WHERE cell_id = $1",
                                             cell.cell_id, required)
                    await connection.execute(
                        "INSERT INTO exomem_cloud_imports "
                        "(import_id, cell_id, kind, declared_bytes, declared_files, required_storage_gib) "
                        "VALUES ($1::uuid, $2, 'adopt', $3, $4, $5)",
                        import_id, cell.cell_id, declared_bytes, len(truth), required)
            finally:
                await connection.close()
        finally:
            drill.resume_cellctl()

        samples: list[int] = []
        sample_failures = 0
        pod_history = {first_pod["uid"]: first_pod}
        last_observation = None
        growth_observed = False
        key_seen = False
        import_baseline = None
        phases: dict[str, dict] = {}

        async def wait_import(predicate, description):
            nonlocal sample_failures, last_observation, growth_observed
            deadline = time.monotonic() + 1200
            while True:
                item_row = (await drill.fetch("SELECT * FROM exomem_cloud_imports WHERE import_id = $1::uuid", import_id))[0]
                # asyncpg returns JSON as text; C5 fixes these transport columns.
                for key in ("part_urls", "part_etags"):
                    if isinstance(item_row.get(key), str):
                        item_row[key] = json.loads(item_row[key])
                # As cellctl's db.read_import does, ImportRow takes the canonical UUID text.
                item_row["import_id"] = str(item_row["import_id"])
                row = await drill.row(cell)
                claim = drill.kube_json("get", "pvc", "cell-data", "--namespace", cell.namespace)
                volumes = drill.kube_json("get", "logicalvolumes.topolvm.io")["items"]
                matches = [v for v in volumes if (v.get("status") or {}).get("volumeID") == binding["volume_id"]]
                if len(matches) != 1:
                    raise StepFailure("the import claim's physical volume identity is unavailable or ambiguous")
                volume = matches[0]
                resources = drill.kube_json("get", "jobs,pods", "--namespace", cell.namespace,
                                            "--selector", f"{JOB_KIND_LABEL}=import")["items"]
                opened = bool(item_row.get("recipient")) or row.get("hold_kind") == "import" or bool(resources)
                if not key_seen:
                    ready = _check_gate(row, claim, volume, opened=opened, required=required)
                    if ready and not growth_observed:
                        growth_observed = True
                        evidence["growth_receipt"] = {"claim": claim["status"], "generation": row["generation"],
                            "observed_storage_generation": row["observed_storage_generation"],
                            "observed_storage_capacity_bytes": row["observed_storage_capacity_bytes"],
                            "logical_volume": volume}
                pod = drill.cell_pod(cell)
                if pod:
                    identity = pod_identity(pod)
                    pod_history[identity["uid"]] = identity
                for resource in resources:
                    if resource["kind"] != "Pod":
                        continue
                    owner = next((o["name"] for o in resource["metadata"].get("ownerReferences", []) if o["kind"] == "Job"), None)
                    if owner:
                        phases[owner] = {"pod": resource["metadata"]["name"], "node": resource["spec"].get("nodeName"),
                                         "phase": resource["status"].get("phase")}
                    if owner == job_name(import_id, "import") and any(
                            s.get("state", {}).get("running") for s in resource["status"].get("containerStatuses", [])):
                        result = drill.kubectl("exec", "--namespace", cell.namespace, resource["metadata"]["name"],
                                               "--container", "import", "--", "python3", "-c", FILESYSTEM_USE, check=False)
                        if result.returncode == 0:
                            used, _ = map(int, result.stdout.split())
                            samples.append(used)
                        else:
                            sample_failures += 1
                observation = {"state": item_row.get("observed_state"), "hold": row.get("hold_kind"),
                    "generation": row["generation"], "observed_storage_generation": row.get("observed_storage_generation"),
                    "observed_storage_capacity_bytes": row.get("observed_storage_capacity_bytes"),
                    "claim_capacity": (claim.get("status") or {}).get("capacity"),
                    "claim_conditions": (claim.get("status") or {}).get("conditions", []),
                    "target_ack": (volume.get("status") or {}).get("reservationTargetResult"),
                    "family": (volume.get("status") or {}).get("backupFamily"),
                    "pod": pod_identity(pod) if pod else None}
                if observation != last_observation:
                    evidence["observations"].append(observation)
                    last_observation = observation
                evidence.update(runtime_pods=list(pod_history.values()), job_pods=phases,
                    volume_use={"status": "lower_bound" if samples else "unknown", "sample_count": len(samples),
                        "sample_failures": sample_failures, "max_observed_used_bytes": max(samples) if samples else None,
                        "baseline_used_bytes": import_baseline["used_bytes"] if import_baseline else None,
                        "baseline_phase": "after growth and prepare; before upload",
                        "max_observed_increase_bytes": max(samples) - import_baseline["used_bytes"]
                            if samples and import_baseline else None,
                        "space_factor": limits["space_factor"],
                        "declared_bytes": declared_bytes,
                        "space_factor_times_declared_bytes": limits["space_factor"] * declared_bytes,
                        "source": "statvfs(/data) via exec in this import Job's running container",
                        "gap": "samples can miss the peak; this run does not qualify the peak-volume bound"})
                # These failure states are fixed by C5, not inferred from Job log text.
                if item_row.get("observed_state") in ("failed", "expired", "discarded"):
                    raise StepFailure(f"the growth import ended as {item_row['observed_state']}: {item_row.get('error_code')}")
                if predicate(item_row):
                    return item_row
                if time.monotonic() >= deadline:
                    raise StepFailure(f"timed out waiting for {description}; C5 state={item_row.get('observed_state')}")
                await asyncio.sleep(0.5)

        key = await wait_import(lambda row: row.get("observed_state") == "key_ready" and row.get("recipient"), "key_ready")
        if not growth_observed:
            raise StepFailure("the growth import reached key_ready without a growth receipt")
        key_seen = True
        evidence["key_ready"] = True
        await drill.wait_row(cell, serving, timeout=900, description="the prepared cell to resume before upload")
        import_baseline = drill.filesystem(cell)
        await asyncio.to_thread(run, ["docker", "run", "--rm", *docker_labels(drill.stack.run_id), "--network=none",
            "--user", f"{os.getuid()}:{os.getgid()}", "--mount", f"type=bind,source={fixture},target=/fixture",
            "--env", "EXOMEM_CLOUD_RESOURCE_POLICY=legacy", "--entrypoint=python3", drill.cell_tag,
            "-c", ENCRYPT, key["recipient"]], timeout=180)
        ciphertext = fixture / "archive.age"
        digest, size = hashlib.sha256(ciphertext.read_bytes()).hexdigest(), ciphertext.stat().st_size
        await _app_sql(drill, "UPDATE exomem_cloud_imports SET ciphertext_sha256 = $2, ciphertext_bytes = $3 "
                       "WHERE import_id = $1::uuid", import_id, digest, size)
        upload = await wait_import(lambda row: bool(row.get("part_urls")), "multipart URLs")
        etags = []
        async with httpx.AsyncClient(timeout=120, trust_env=False,
                                     verify=str(drill.stack.object_store.ca_path) if drill.stack.object_store.ca_path else True) as client:
            with ciphertext.open("rb") as stream:
                for ordinal, part in enumerate(upload["part_urls"], 1):
                    payload = stream.read(part["part_size_bytes"])
                    if part["part_number"] != ordinal or len(payload) != part["part_size_bytes"]:
                        raise StepFailure("C5 multipart URLs do not cover the immutable ciphertext")
                    response = await client.put(part["url"], content=payload, headers={"Content-Length": str(len(payload))})
                    response.raise_for_status()
                    etags.append({"part_number": ordinal, "etag": response.headers["etag"]})
                if stream.read(1):
                    raise StepFailure("C5 multipart URLs omit ciphertext bytes")
        await _app_sql(drill, "UPDATE exomem_cloud_imports SET part_etags = $2::jsonb, requested_state = 'uploaded' "
                       "WHERE import_id = $1::uuid", import_id, json.dumps(etags))
        done = await wait_import(lambda row: row.get("observed_state") == "done", "done with staged counts")
        await drill.wait_row(cell, serving, timeout=900, description="the imported cell to resume ready")
        item = ImportRow(**{field.name: done[field.name] for field in fields(ImportRow)})
        observed = json.loads(drill.cell_exec(cell, "python3", "-c", CELL_PROBE, import_id).stdout)
        _check_staged(item, observed, truth)
        resources = drill.kube_json("get", "jobs,pods", "--namespace", cell.namespace,
                                    "--selector", f"{JOB_KIND_LABEL}=import")["items"]
        storage = _storage(drill.stack.object_store)
        versions = [entry for page in storage.get_paginator("list_object_versions").paginate(Bucket=STAGING_BUCKET, Prefix=item.key)
                    for entry in page.get("Versions", []) + page.get("DeleteMarkers", []) if entry["Key"] == item.key]
        if resources or versions or open_uploads(storage, STAGING_BUCKET, item.key):
            raise StepFailure("import Jobs, Pods or encrypted transport remain after done")
        evidence.update(done=True, staged_files=item.staged_files, staged_bytes=item.staged_bytes,
                        ciphertext_sha256=digest, ciphertext_bytes=size, source_and_staged_hashes_match=True,
                        temporary_resources_absent=True, filesystem_after=drill.filesystem(cell),
                        pod_after=pod_identity(drill.cell_pod(cell)))
    finally:
        shutil.rmtree(fixture)

