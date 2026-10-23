"""One native CLI folder import through the real rehearsal control plane."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
from cellctl.imports import ImportRow
from cellctl.manifests import JOB_KIND_LABEL, namespace_name

from .infra import BACKUP_BUCKET, ObjectStore, docker_labels
from .report import StepFailure, guarded
from .shell import run, wait_for
from .substrate import MCP_URL, PUBLIC_BASE_URL

if TYPE_CHECKING:
    from .scenarios import Context


# BROWSER is Python's standard browser hook; it changes only the human side of OAuth.
BROWSER_SCRIPT = """import os, sys
with os.fdopen(os.open('/fixture/authorization-url', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
    output.write(sys.argv[1])
"""

CALLBACK_SCRIPT = """import sys, urllib.request
with urllib.request.urlopen(sys.stdin.read().strip(), timeout=10) as response:
    print(response.status)
"""

LISTENER_SCRIPT = """import socket, sys
socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=1).close()
"""

# RFC 5737 reserves this address; no real service receives the reachability probe.
EGRESS_SCRIPT = """import errno, socket
try:
    socket.create_connection(('192.0.2.1', 443), timeout=3).close()
    print('connected')
except OSError as error:
    print('sealed' if error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH) else 'unverified')
"""

PAYLOAD_SCRIPT = """import hashlib, json
from pathlib import Path
digest = hashlib.sha256(); count = total = 0
for root in (Path('/app/.venv'), Path('/opt/exomem-models')):
    for path in sorted(root.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            digest.update(str(path).encode() + b'\\0' + len(data).to_bytes(8, 'big') + hashlib.sha256(data).digest())
            count += 1; total += len(data)
print(json.dumps({'sha256': digest.hexdigest(), 'files': count, 'bytes': total}))
"""

CELL_PROBE = """import hashlib, json, stat, sys
from pathlib import Path
from exomem.state_paths import vault_state_dir
vault = Path('/data/vault'); import_id = sys.argv[1]
folder = vault / '_Imports' / import_id
files = {}
for path in sorted(folder.rglob('*')):
    if path.is_symlink():
        raise RuntimeError('import contains a link')
    if path.is_file():
        data = path.read_bytes()
        files[path.relative_to(folder).as_posix()] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
receipt = vault_state_dir(vault) / 'cloud-imports' / (import_id + '.json')
print(json.dumps({'files': files, 'receipt': json.loads(receipt.read_text()),
    'receipt_mode': stat.S_IMODE(receipt.stat().st_mode),
    'receipt_directory_mode': stat.S_IMODE(receipt.parent.stat().st_mode),
    'identity_absent': not (vault.parent / '.import-keys' / import_id).exists(),
    'staging_absent': not (vault.parent / ('.import-' + import_id)).exists()}))
"""


def image_payload(image: str, *, run_id: str | None = None) -> dict:
    labels = docker_labels(run_id) if run_id else []
    return json.loads(run(["docker", "run", "--rm", *labels, "--network=none", "--tmpfs", "/data", "--entrypoint=python3",
                           image, "-c", PAYLOAD_SCRIPT]).stdout)


def _storage(store: ObjectStore):
    return boto3.client("s3", endpoint_url=store.endpoint(from_host=True), region_name="us-east-1",
                        aws_access_key_id=store.access_key, aws_secret_access_key=store.secret_key,
                        verify=str(store.ca_path), config=Config(s3={"addressing_style": "path"}, proxies={}))


def enable_versioning(store: ObjectStore) -> None:
    client = _storage(store)
    wait_for(lambda: client.list_buckets() is not None, timeout=60, description="verified TLS storage")
    client.put_bucket_versioning(Bucket=BACKUP_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    if client.get_bucket_versioning(Bucket=BACKUP_BUCKET).get("Status") != "Enabled":
        raise StepFailure("the rehearsal storage did not enable versioning")


async def import_folder(ctx: Context, image: str) -> dict:
    """Retain native CLI evidence before teardown, including early failures."""
    try:
        return await _import_folder(ctx, image)
    finally:
        guarded(ctx.report, "native_cli_diagnostics", lambda: _retain_cli_diagnostics(ctx))


def _retain_cli_diagnostics(ctx: Context) -> None:
    name = f"exo-rehearsal-cli-{ctx.stack.run_id}"
    if name not in ctx.stack.containers:
        return
    fixture = ctx.stack.workdir / "native-import"
    evidence = {}
    for filename, command in (
        ("cli.log", ["docker", "logs", name]),
        ("cli-state.json", ["docker", "inspect", "--format", "{{json .State}}", name]),
    ):
        result = run(command, check=False, timeout=10)
        path = fixture / filename
        path.write_text(result.stdout + result.stderr)
        path.chmod(0o600)
        evidence[filename] = {"returncode": result.returncode, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    ctx.report.stages["cloud_import"]["cli_diagnostics"] = evidence


async def _import_folder(ctx: Context, image: str) -> dict:
    """Keep CLI auth, encryption, multipart reports and polling entirely native."""
    if not ctx.a.cell_id or not ctx.a.session:
        raise StepFailure("native import requires the provisioned owner cell and browser session")
    fixture = ctx.stack.workdir / "native-import"
    fixture.mkdir(mode=0o700)
    source = ctx.stack.workdir / "native-import-source"
    source.mkdir(mode=0o700)
    (source / "notes").mkdir()
    # These are invented fixture contents, not a tenant vocabulary or classification rule.
    examples = {"plan.md": b"# Example plan\n\nPrepare the demonstration.\n",
                "notes/checklist.txt": b"Review the source.\nConfirm the imported files.\n"}
    truth = {}
    for name, content in examples.items():
        (source / name).write_bytes(content)
        truth[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (fixture / "browser.py").write_text(BROWSER_SCRIPT)
    name = f"exo-rehearsal-cli-{ctx.stack.run_id}"
    ctx.stack.containers.append(name)
    run(["docker", "run", "--detach", "--name", name, "--network", ctx.substrate.sealed_network,
         "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "exomem",
         "--mount", f"type=bind,source={source},target=/input,readonly",
         "--mount", f"type=bind,source={fixture},target=/fixture",
         "--mount", f"type=bind,source={ctx.resolver.ca_path},target=/ca.pem,readonly",
         # This process is a tenant CLI; the image's service profile requires Cloud cell mode.
         "--env", "EXOMEM_CLOUD_RESOURCE_POLICY=legacy",
         "--env", "SSL_CERT_FILE=/ca.pem", "--env", "BROWSER=python3 /fixture/browser.py %s",
         image, "cloud", "import", "/input", "--checkpoint", "/fixture/checkpoint",
         "--mcp-url", MCP_URL, "--api-base-url", PUBLIC_BASE_URL])
    egress = run(["docker", "exec", name, "python3", "-c", EGRESS_SCRIPT]).stdout.strip()
    if egress != "sealed":
        raise StepFailure("native CLI external egress is unverified")

    authorization = fixture / "authorization-url"
    await asyncio.to_thread(wait_for, lambda: authorization.exists(), timeout=60, description="the native browser request")
    url = authorization.read_text()
    query = parse_qs(urlsplit(url).query)
    redirect_uri = query["redirect_uri"][0]
    parsed_redirect = urlsplit(redirect_uri)
    if (parsed_redirect.scheme, parsed_redirect.hostname, parsed_redirect.path) != ("http", "127.0.0.1", "/callback"):
        raise StepFailure("native OAuth did not request its registered loopback callback")
    if query.get("code_challenge_method") != ["S256"] or not query.get("code_challenge"):
        raise StepFailure("native OAuth did not use S256 PKCE")
    checkpoint = fixture / "checkpoint"
    if stat.S_IMODE(checkpoint.stat().st_mode) != 0o700 or stat.S_IMODE((checkpoint / "checkpoint.json").stat().st_mode) != 0o600:
        raise StepFailure("native checkpoint permissions are not private")
    callback = await ctx.browser.consent_redirect(ctx.a.session, url, redirect_uri)
    # Deliver to the real loopback server in the CLI container, preserving code/state/issuer.
    await asyncio.to_thread(wait_for, lambda: run(["docker", "exec", name, "python3", "-c", LISTENER_SCRIPT,
                                                 str(parsed_redirect.port)], check=False).returncode == 0,
                            timeout=30, description="the native OAuth callback listener")
    delivered = run(["docker", "exec", "--interactive", name, "python3", "-c", CALLBACK_SCRIPT], input_text=callback)
    if delivered.stdout.strip() != "200":
        raise StepFailure("the native callback did not accept the authorization response")
    exited = await asyncio.to_thread(run, ["docker", "wait", name], timeout=900)
    logs = run(["docker", "logs", name], check=False)
    log_file = fixture / "cli.log"
    log_file.write_text(logs.stdout + logs.stderr)
    log_file.chmod(0o600)
    results = []
    for line in logs.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        # The CLI's structured result fixes this key; human OAuth logs are not result records.
        if isinstance(value, dict) and "ok" in value:
            results.append(value)
    if exited.stdout.strip() != "0" or len(results) != 1 or not results[0].get("ok"):
        raise StepFailure(f"native CLI did not return success; exit={exited.stdout.strip()}, structured_results={results}")
    result = results[0]
    import_id = str(uuid.UUID(result["import_id"]))
    if result != {"ok": True, "import_id": import_id, "state": "done"}:
        raise StepFailure("native CLI did not report the completed C5 import")
    row = await ctx.fetchrow("SELECT * FROM exomem_cloud_imports WHERE cell_id = $1 AND import_id = $2::uuid", ctx.a.cell_id, import_id)
    count = await ctx.fetchrow("SELECT count(*) AS count FROM exomem_cloud_imports WHERE cell_id = $1", ctx.a.cell_id)
    if not row or not count or count["count"] != 1 or row["observed_state"] != "done":
        raise StepFailure("C5 did not retain exactly one completed native import")
    item = ImportRow(import_id=import_id, cell_id=row["cell_id"], kind=row["kind"], declared_bytes=row["declared_bytes"],
                     declared_files=row["declared_files"], requested_state=row["requested_state"], created_at=row["created_at"],
                     ciphertext_sha256=row["ciphertext_sha256"], ciphertext_bytes=row["ciphertext_bytes"])
    ready = await ctx.wait_cell(ctx.a.cell_id, lambda cell: cell.get("ready") and cell.get("observed_state") == "running"
                               and cell.get("hold_kind") is None, timeout=600, description="the imported cell to resume ready")
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if not pod or not ctx.pod_ready(ctx.a.cell_id):
        raise StepFailure("the imported cell has no Ready runtime Pod")
    namespace = namespace_name(ctx.a.cell_id)
    observed = json.loads(ctx.exec_in(namespace, pod["metadata"]["name"], "python3", "-c", CELL_PROBE, import_id).stdout)
    if observed["files"] != truth or observed["receipt"]["request"] != item.request():
        raise StepFailure("the staged files or immutable receipt differ from source/C5 truth")
    if (item.kind, item.declared_files, item.declared_bytes) != ("adopt", len(truth), sum(file["bytes"] for file in truth.values())):
        raise StepFailure("C5 counts differ from the original folder")
    if observed["receipt"]["result"] != {"ok": True, "files": item.declared_files, "bytes": item.declared_bytes}:
        raise StepFailure("the import receipt counts differ from source truth")
    if observed["receipt_mode"] != 0o600 or observed["receipt_directory_mode"] != 0o700:
        raise StepFailure("the import receipt is not private")
    if not observed["identity_absent"] or not observed["staging_absent"] or any(checkpoint.iterdir()):
        raise StepFailure("temporary identities, staging or native checkpoint material remain")
    resources = ctx.kube_json("get", "jobs,pods", "--namespace", namespace, "--selector", f"{JOB_KIND_LABEL}=import")["items"]
    storage = _storage(ctx.stack.object_store)
    versions = [entry for page in storage.get_paginator("list_object_versions").paginate(Bucket=BACKUP_BUCKET, Prefix=item.key)
                for entry in page.get("Versions", []) + page.get("DeleteMarkers", [])]
    uploads = [entry for page in storage.get_paginator("list_multipart_uploads").paginate(Bucket=BACKUP_BUCKET, Prefix=item.key)
               for entry in page.get("Uploads", [])]
    if resources or versions or uploads:
        raise StepFailure("import phase resources or encrypted transport remain after C5 completion")
    return {"cli": result, "source_files": truth, "source_bytes": item.declared_bytes, "c5_rows": count["count"],
            "pkce": "S256", "loopback_callback": "native", "external_egress": egress,
            "receipt_private": True, "temporary_resources_absent": True, "cell_ready": ready["ready"],
            "product_and_model": image_payload(image), "cli_log_sha256": hashlib.sha256(log_file.read_bytes()).hexdigest()}
