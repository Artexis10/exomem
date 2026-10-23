"""The owner API and real export controller share one disposable C6 workflow.

Versity supplies transport, versions and cleanup here. This stage does not prove
provider encryption or per-cell key isolation; their dedicated checks do that.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from cellctl.exports import job_name
from cellctl.manifests import namespace_name

from .infra import BACKUP_BUCKET, docker_labels
from .report import StepFailure
from .shell import run
from .substrate import PUBLIC_BASE_URL

if TYPE_CHECKING:
    from .scenarios import Context


# Invented fixture bytes establish truth before the export reads the cell.
SOURCE_BODY = b"# Export fixture\n\nThe archive must preserve this original.\n"
SEED_SOURCE = """import pathlib, sys
path = pathlib.Path('/data/vault') / sys.argv[1]
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('xb') as target:
    target.write(bytes.fromhex(sys.argv[2]))
"""
VERIFY_ARCHIVE = """import json, sys
from exomem.vault_portability import verify_export_archive
archive = verify_export_archive('/fixture/export.zip', expected_cell_id=sys.argv[1])
print(json.dumps({'sha256': archive.archive_sha256, 'files': len(archive.manifest['files'])}))
"""


async def run_export(ctx: Context, verifier_image: str, *, timeout_seconds: float = 600) -> dict:
    """Request through the owner API, verify the bytes, then observe expiry."""
    owner = ctx.a
    if not owner.cell_id or not owner.session:
        raise StepFailure("owner export requires the provisioned synthetic owner")
    cell_id = owner.cell_id
    export_id = str(uuid.uuid4())
    source_path = f"Knowledge Base/Sources/Export fixture {ctx.stack.run_id}.md"
    deadline = time.monotonic() + timeout_seconds
    output = ctx.stack.workdir / "owner-export.json"
    record = {
        "state": "RUNNING",
        "cell_id": cell_id,
        "export_id": export_id,
        "verifier_image": verifier_image,
        "checks": {},
        "provider_encryption_proven": False,
        "provider_key_isolation_proven": False,
    }

    def save() -> None:
        output.write_text(json.dumps(record, indent=2))
        output.chmod(0o600)
        ctx.report.stages["owner_export"]["evidence"] = record

    async def until(check):
        while time.monotonic() < deadline:
            value = await check()
            if time.monotonic() >= deadline:
                break
            if value:
                return value
            await asyncio.sleep(1)
        raise StepFailure("the owner export observation deadline elapsed")

    async def row():
        return await ctx.fetchrow(
            "SELECT * FROM exomem_cloud_exports WHERE export_id=$1 AND cell_id=$2",
            uuid.UUID(export_id),
            cell_id,
        )

    async def ready():
        cell = await ctx.cell(cell_id)
        return (
            cell["hold_kind"] is None
            and cell["observed_state"] == "running"
            and cell["ready"]
            and ctx.pod_ready(cell_id)
        )

    async def display():
        async with ctx.resolver.async_client(cookies=owner.session.cookies) as client:
            response = await client.get(f"{PUBLIC_BASE_URL}/api/exomem/cloud/exports")
        if response.status_code != 200:
            raise StepFailure(f"export status answered HTTP {response.status_code}")
        return response.json()["export"]

    save()
    try:
        if not await ready():
            raise StepFailure("the synthetic owner must start Ready without a hold")
        pod = ctx.runtime_pod(cell_id)
        if pod is None:
            raise StepFailure("the synthetic runtime is absent")
        ctx.exec_in(
            namespace_name(cell_id),
            pod["metadata"]["name"],
            "python3",
            "-c",
            SEED_SOURCE,
            source_path,
            SOURCE_BODY.hex(),
        )
        started = await ctx.browser.post_with_session(
            owner.session, "/api/exomem/cloud/exports", {"export_id": export_id}
        )
        if started.status_code != 201:
            raise StepFailure(f"export request answered HTTP {started.status_code}")
        if started.json()["export"]["export_id"] != export_id:
            raise StepFailure("the export response identifies a different request")
        record["checks"]["authenticated_request_created"] = True
        save()

        async def packed():
            observed = await row()
            if observed and observed["observed_state"] == "failed":
                raise StepFailure("the real export Job failed")
            return observed if observed and observed["observed_state"] == "ready" else None

        packed_row = await until(packed)
        await until(ready)
        jobs = ctx.kube_json("get", "jobs", "--namespace", namespace_name(cell_id))["items"]
        if any(job["metadata"]["name"] == job_name(export_id) for job in jobs):
            raise StepFailure("the completed export still has a Job")
        pods = ctx.kube_json(
            "get",
            "pods",
            "--namespace",
            namespace_name(cell_id),
            "--selector",
            f"batch.kubernetes.io/job-name={job_name(export_id)}",
        )["items"]
        if pods:
            raise StepFailure("the completed export still has pods")
        shown = await display()
        if shown.get("export_id") != export_id or shown.get("state") != "ready":
            raise StepFailure("the owner API does not report this export ready before expiry")
        record["checks"]["ready_before_archive_expiry"] = True

        async def issued():
            response = await ctx.browser.post_with_session(
                owner.session, f"/api/exomem/cloud/exports/{export_id}/link", {}
            )
            if response.status_code == 202:
                return None
            if response.status_code != 200:
                raise StepFailure(f"export link request answered HTTP {response.status_code}")
            return response.json()

        link = await until(issued)
        version = packed_row["archive_version_id"]
        if not version or parse_qs(urlsplit(link["url"]).query).get("versionId") != [str(version)]:
            raise StepFailure("the owner link is not pinned to the recorded archive version")
        record["checks"]["recorded_version_pinned"] = True
        with tempfile.TemporaryDirectory(prefix="owner-export-", dir=ctx.stack.workdir) as folder:
            archive = Path(folder) / "export.zip"
            # Storage receives no owner cookies, CSRF token or bearer.
            async with ctx.resolver.async_client(follow_redirects=False) as client:
                async with client.stream("GET", link["url"]) as response:
                    if response.status_code != 200:
                        raise StepFailure(f"archive download answered HTTP {response.status_code}")
                    with archive.open("wb") as sink:
                        async for chunk in response.aiter_bytes():
                            sink.write(chunk)
            archive.chmod(0o600)
            # The selected Cloud image owns the canonical verifier and its dependencies.
            verified = json.loads(
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        *docker_labels(ctx.stack.run_id),
                        "--network=none",
                        "--memory=256m",
                        "--tmpfs",
                        "/data",
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        "--mount",
                        f"type=bind,source={folder},target=/fixture,readonly",
                        "--entrypoint",
                        "python3",
                        verifier_image,
                        "-c",
                        VERIFY_ARCHIVE,
                        cell_id,
                    ],
                    timeout=60,
                ).stdout
            )
            if verified["sha256"] != packed_row["archive_sha256"]:
                raise StepFailure(
                    "the downloaded archive digest differs from the controller receipt"
                )
            with zipfile.ZipFile(archive) as contents:
                if (
                    hashlib.sha256(contents.read(source_path)).digest()
                    != hashlib.sha256(SOURCE_BODY).digest()
                ):
                    raise StepFailure("the synthetic source bytes changed during export")
            record["checks"].update(archive_verified=True, synthetic_source_preserved=True)
            record["archive_sha256"] = verified["sha256"]
        save()

        # Time injection changes only this fixture's row, as in the real K3s test.
        await ctx.execute(
            "UPDATE exomem_cloud_exports SET expires_at=now()-interval '1 second' "
            "WHERE export_id=$1 AND cell_id=$2",
            uuid.UUID(export_id),
            cell_id,
        )

        async def expired():
            observed = await row()
            return observed if observed and observed["observed_state"] == "expired" else None

        expired_row = await until(expired)
        if expired_row["download_url"] is not None or (await display())["state"] != "expired":
            raise StepFailure("the owner API or row still exposes an unexpired export")
        storage = ctx.s3()
        prefix = f"cells/{cell_id}/exports/{export_id}.zip"
        versions = [
            v
            for page in storage.get_paginator("list_object_versions").paginate(
                Bucket=BACKUP_BUCKET, Prefix=prefix
            )
            for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        uploads = [
            u
            for page in storage.get_paginator("list_multipart_uploads").paginate(
                Bucket=BACKUP_BUCKET, Prefix=prefix
            )
            for u in page.get("Uploads", [])
        ]
        if versions or uploads:
            raise StepFailure("the expired archive still has versions or unfinished uploads")
        await until(ready)
        record["checks"].update(
            expired_api=True, versions_remaining=0, uploads_remaining=0, ready_after_cleanup=True
        )
        record["state"] = "PASS"
    except BaseException as error:  # noqa: BLE001 - interrupted work must retain a sanitized receipt
        record["state"] = "PARTIAL"
        record["error_type"] = type(error).__name__
        # Shared reports include exception text; keep provider URLs and errors out.
        raise StepFailure("owner export failed; inspect the sanitized receipt") from None
    finally:
        save()
    return record
