"""Optional interrupted recovery proof using the shipped operator run and real cell.

Only restic's in-place restore is held. Scratch restores and all other restic
commands delegate to the image's unchanged binary. Recovery stays in the
release scripts; the observer records facts before the stack's teardown.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from cellctl.manifests import BACKUP_PATHS, JOB_KIND_LABEL, JOB_KIND_RESTORE, namespace_name

from . import build, images
from .infra import Stack, docker_labels
from .mcp_client import ToolResult
from .platform import CLOUD_NAMESPACE, wait_rollout
from .report import StepFailure
from .restore_abort_observer import OBSERVER_SCHEMA
from .shell import run, wait_for

if TYPE_CHECKING:
    from .scenarios import Context

INOTIFY_PACKAGE = "inotify-simple==2.0.0"

# The restore CLI fixes both this run's prior directory and restic's first
# argument. This predicate selects a protocol phase, never natural language.
RESTIC_WRAPPER = '''#!/usr/local/bin/python3
import json, os, sys, time
from pathlib import Path

run_id = "@run_id@"
roots = @roots@
prior = Path("/data/.restore-prior-" + run_id)
if sys.argv[1:2] != ["restore"] or not prior.is_dir():
    os.execv("/usr/local/bin/restic-real", ["restic", *sys.argv[1:]])

os.umask(0o077)
original = {p: (prior / Path(p).name).stat().st_ino for p in roots}
assert all((prior / Path(p).name).is_dir() for p in roots)
assert (prior / "host" / ("restore-abort-" + run_id + ".sentinel")).is_file()
for p in roots:
    Path(p).mkdir(mode=0o700)
partial = {p: Path(p).stat().st_ino for p in roots}
witness = Path("/data/.restore-abort-" + run_id + ".json")
count = 0
unsafe = False
# No setup close: the kernel closes this sole writable fd when the writer exits.
lease = os.open("/data/.restore-abort-" + run_id + ".lease", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(lease, b"active")

def record(state):
    global unsafe
    intact = all((prior / Path(p).name).exists() and
                 (prior / Path(p).name).stat().st_ino == original[p] for p in roots)
    distinct = all(Path(p).exists() and Path(p).stat().st_ino == partial[p] and
                   partial[p] != original[p] for p in roots)
    unsafe = unsafe or not intact or not distinct
    data = {"state": state, "writes": count, "prior_intact": intact,
            "canonical_partial": distinct, "unsafe": unsafe, "pid": os.getpid(),
            "partial_inodes": partial,
            "markers": all((Path(p) / ("restore-abort-" + run_id + ".partial")).is_file() for p in roots)}
    temporary = witness.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(witness)

while True:
    for p in roots:
        (Path(p) / ("restore-abort-" + run_id + ".partial")).write_text(str(count))
    count += 1
    record("writing")
    time.sleep(0.1)
'''

# Use the cell's real MCP surface with its own environment bearer. No OAuth
# or gateway claim is made by this operator recovery stage.
MCP_PROBE = '''
import asyncio, json, os, sys
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    phrase, write = sys.argv[1:]
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + os.environ["EXOMEM_CLOUD_CELL_TOKEN"]}, timeout=120) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                if write == "write":
                    result = await session.call_tool("remember", {"title": phrase,
                        "content": phrase + chr(10) * 2 + "## Observations" + chr(10) * 2 + "- [rehearsal] " + phrase + " #rehearsal" + chr(10), "status": "draft"})
                else:
                    result = await session.call_tool("ask_memory", {"query": phrase})
                wire = result.model_dump(by_alias=True)
                print(json.dumps({"is_error": wire["isError"], "result": wire.get("structuredContent") or {}}))

asyncio.run(main())
'''

ROOT_FACTS = '''
import json, os, stat, sys
from pathlib import Path
run_id, action = sys.argv[1:]
sentinel = Path("/data/host/restore-abort-" + run_id + ".sentinel")
if action == "seed":
    sentinel.write_text(run_id)
    sentinel.chmod(0o600)
roots = [Path(p) for p in @roots@]
print(json.dumps({"roots": {str(p): {"uid": p.stat().st_uid, "mode": stat.S_IMODE(p.stat().st_mode)} for p in roots},
    "sentinel_preserved": sentinel.is_file() and sentinel.read_text() == run_id,
    "sentinel_uid": sentinel.stat().st_uid if sentinel.exists() else None,
    "sentinel_mode": stat.S_IMODE(sentinel.stat().st_mode) if sentinel.exists() else None,
    "partial_absent": all(not (p / ("restore-abort-" + run_id + ".partial")).exists() for p in roots),
    "prior_absent": not Path("/data/.restore-prior-" + run_id).exists(),
    "custody_root_present": Path("/data/host/.local/state/exomem/standalone-host-control-v1").exists()}))
'''.replace("@roots@", repr(list(BACKUP_PATHS)))


def _labels(stack: Stack, *, image: bool = False) -> list[str]:
    return docker_labels(stack.run_id, image=image)


def build_writer_image(stack: Stack, base: str) -> str:
    context = stack.workdir / "restore-writer-image"
    context.mkdir()
    (context / "restic").write_text(RESTIC_WRAPPER.replace("@run_id@", stack.run_id).replace("@roots@", repr(list(BACKUP_PATHS))))
    (context / "Dockerfile").write_text(
        f"FROM {base}\nUSER 0:0\n"
        "RUN mv /usr/local/bin/restic /usr/local/bin/restic-real\n"
        "COPY --chmod=0755 restic /usr/local/bin/restic\nUSER 10001:10001\n"
    )
    tag = f"{build.CELL_REPOSITORY}:{stack.run_id}-restore-writer"
    run(["docker", "build", *_labels(stack, image=True), "--tag", tag, str(context)], timeout=900)
    return tag


def operator_kubeconfig(source: Path, destination: Path, address: str) -> None:
    """Retain the disposable cluster's CA and credentials; change only its route."""
    document = yaml.safe_load(source.read_text())
    for cluster in document["clusters"]:
        cluster["cluster"]["server"] = f"https://{address}:6443"
    destination.write_text(yaml.safe_dump(document))
    destination.chmod(0o600)


def observer_record(stdout: str) -> dict:
    """Read the one final witness from Docker's retained stdout carrier."""
    records = []
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        # The observer fixes this schema token; Bash's other logs are unrelated.
        if isinstance(record, dict) and record.get("schema") == OBSERVER_SCHEMA:
            records.append(record)
    if len(records) != 1:
        raise StepFailure(f"expected one final kernel witness in Docker stdout, observed {len(records)}")
    return records[0]


def observed_volume_mount(stack: Stack, volume_path: str) -> str:
    """Mount only the selected PVC subdirectory from this run's K3s volume."""
    path = Path(volume_path)
    # This directory is fixed by the pinned K3s local-path provisioner.
    relative = path.relative_to("/var/lib/rancher/k3s/storage")
    if len(relative.parts) != 1 or ".." in path.parts:
        raise StepFailure("the selected PVC is outside the disposable K3s storage directory")
    # infra._start_k3s fixes this ownership name for the disposable stack.
    if stack.k3s.container != f"exo-rehearsal-k3s-{stack.run_id}":
        raise StepFailure("the volume observer requires this run's disposable K3s container")
    observed = json.loads(run(["docker", "inspect", stack.k3s.container]).stdout)[0]
    if observed["Name"] != f"/{stack.k3s.container}":
        raise StepFailure("the volume observer did not inspect this run's K3s container")
    mounts = [mount for mount in observed["Mounts"] if path.is_relative_to(mount["Destination"])]
    mount = max(mounts, key=lambda item: len(Path(item["Destination"]).parts))
    if mount["Type"] != "volume" or not mount.get("Name"):
        raise StepFailure("the selected PVC does not reside on the disposable K3s Docker volume")
    subpath = path.relative_to(mount["Destination"])
    return f"type=volume,source={mount['Name']},target=/observed-cell-volume,readonly,volume-subpath={subpath}"


def _start_operator(ctx: Context, snapshot: str, volume_path: str) -> str:
    stack = ctx.stack
    context = stack.workdir / "restore-operator-image"
    context.mkdir()
    mount = observed_volume_mount(stack, volume_path)
    run(["uv", "pip", "install", "--quiet", "--target", str(context / "extra"),
         "--python-version", "3.12", "--python-platform", "x86_64-manylinux_2_28",
         "--no-cache", INOTIFY_PACKAGE], timeout=900)
    shutil.copy(Path(__file__).with_name("restore_abort_observer.py"), context / "observer.py")
    k3s = yaml.safe_load((images.REPO_ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text())
    # This rehearsal's observer dependencies and operator run on x86 only.
    binary = k3s["k3s_binaries"]["x86_64"]
    ctx.report.stages["operator_restore_abort"]["operator_k3s"] = {
        "version": k3s["k3s_version"], "sha256": binary["sha256"],
    }
    # Only install the break-glass OpenSSL CLI if the source image lacks it.
    # The remaining names are the fixed CLI dependencies of the shipped run.
    # The script needs K3s's full host launcher; the image's slim binary has a different argv contract.
    # Reuse the node role's pin and Docker's checksum verification, without an admin shim.
    (context / "Dockerfile").write_text(
        f"FROM {build.CELL_REPOSITORY}:{stack.run_id}-restore-writer\nUSER 0:0\n"
        "RUN if ! command -v openssl >/dev/null; then apt-get update "
        "&& apt-get install -y --no-install-recommends openssl "
        "&& rm -rf /var/lib/apt/lists/*; fi; "
        "for tool in bash openssl python3 base64 sha256sum awk date seq od tr cut grep tail wc install cp; "
        "do command -v \"$tool\" >/dev/null || exit 1; done\n"
        f"ADD --checksum=sha256:{binary['sha256']} --chmod=0755 "
        f"{binary['url']} /usr/local/bin/k3s\n"
        "COPY extra /opt/restore-observer\nCOPY observer.py /opt/restore-observer/observer.py\n"
    )
    tag = f"{build.CELL_REPOSITORY}:{stack.run_id}-restore-operator"
    run(["docker", "build", *_labels(stack, image=True), "--tag", tag, str(context)], timeout=900)
    config = stack.workdir / "operator-kubeconfig.yaml"
    operator_kubeconfig(stack.k3s.kubeconfig, config, stack.k3s.ip)
    name = f"exo-rehearsal-restore-operator-{stack.run_id}"
    # Register before creation, so even a partially successful start has an owner.
    stack.containers.append(name)
    run([
        "docker", "run", "--detach", "--name", name, *_labels(stack),
        "--network", stack.network, "--entrypoint", "python3", "--mount", mount,
        "--mount", f"type=bind,source={config},target=/etc/rancher/k3s/k3s.yaml,readonly",
        "--mount", f"type=bind,source={images.REPO_ROOT / 'infra/scripts'},target=/release/infra/scripts,readonly",
        "--mount", f"type=bind,source={images.REPO_ROOT / 'infra/cellctl/src'},target=/release/infra/cellctl/src,readonly",
        "--tmpfs", "/var/lib/exomem-restore:rw,mode=700,size=64m",
        # The shipped procedure executes its generated break-glass shim under /dev/shm.
        "--tmpfs", "/dev/shm:rw,exec,mode=700,size=64m",
        tag, "/opt/restore-observer/observer.py", stack.run_id,
        json.dumps([Path(path).name for path in BACKUP_PATHS]), ctx.a.cell_id, snapshot,
    ])
    return name


def _runtime(ctx: Context, script: str, *args: str) -> dict:
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        raise StepFailure("the runtime is absent during a restore abort probe")
    result = ctx.exec_in(namespace_name(ctx.a.cell_id), pod["metadata"]["name"], "python3", "-c", script, *args)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _mcp(ctx: Context, phrase: str, action: str) -> dict:
    def probe() -> ToolResult | None:
        reply = _runtime(ctx, MCP_PROBE, phrase, action)
        result = ToolResult(structured=reply["result"], is_error=reply["is_error"], seconds=0)
        # The application fixes this retryable refusal code; no mutation committed.
        return None if result.error_code == "MUTATION_WARMING" else result

    # A transport or parser failure can follow a committed write; do not replay it.
    result = wait_for(
        probe,
        timeout=90,
        interval=0.5,
        description=f"the restore abort {action} probe",
        retry_exceptions=() if action == "write" else (Exception,),
    )
    # MCP's structured error envelope is part of the application contract.
    body = result.structured
    nested = body.get("result", body) if isinstance(body, dict) else body
    if result.is_error or result.error_code or (isinstance(nested, dict) and nested.get("success") is False):
        raise StepFailure(f"the {action} MCP probe refused the restore abort operation")
    if action == "recall":
        hits = nested.get("hits") if isinstance(nested, dict) else nested
        if not isinstance(hits, list) or phrase not in json.dumps(hits):
            raise StepFailure("recall lost a governed write after the interrupted restore")
    return {"action": action, "accepted": True, "phrase_recalled": action == "recall"}


async def run_abort(ctx: Context) -> None:
    stack = ctx.stack
    evidence = ctx.report.stages["operator_restore_abort"]
    evidence["signed_custody_recovery"] = "unproved: ordinary writes do not initialize signed standalone custody"
    evidence["scope"] = "actual internal Bash run, not the systemd launcher or production qualification"
    evidence["observer_package"] = INOTIFY_PACKAGE
    files = [*sorted((images.REPO_ROOT / "infra/scripts").glob("cloud_restore*")),
             images.REPO_ROOT / "infra/scripts/cloud_vault_digest.py",
             images.REPO_ROOT / "infra/cellctl/src/cellctl/manifests.py",
             Path(__file__), Path(__file__).with_name("restore_abort_observer.py")]
    evidence["release_sha256"] = {str(path.relative_to(images.REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in files if path.is_file()}
    if not ctx.a.cell_id or not ctx.pod_ready(ctx.a.cell_id):
        raise StepFailure("operator restore abort requires the provisioned cell to be Ready")
    namespace = namespace_name(ctx.a.cell_id)
    before = ctx.kube_json("get", "deployment", "cellctl", "-n", CLOUD_NAMESPACE)
    replicas = before["spec"]["replicas"]
    _mcp(ctx, f"Before snapshot {stack.run_id}", "write")
    previous = (await ctx.cell(ctx.a.cell_id)).get("last_backup_snapshot")
    hour = dt.datetime.now(dt.UTC).hour
    ctx.set_backup_window(f"{hour}-{(hour + 2) % 24}")
    try:
        backed_up = await ctx.wait_cell(ctx.a.cell_id,
            lambda row: bool(row.get("last_backup_snapshot")) and row["last_backup_snapshot"] != previous
            and row.get("hold_kind") is None and row.get("ready") is True,
            timeout=1200, interval=2, description="the restore abort S0 snapshot to finish")
    finally:
        hour = dt.datetime.now(dt.UTC).hour
        ctx.set_backup_window(f"{(hour + 6) % 24}-{(hour + 7) % 24}")
    snapshot = backed_up["last_backup_snapshot"]
    evidence["snapshot"] = snapshot
    post_phrase = f"After snapshot {stack.run_id}"
    evidence["post_snapshot_write"] = _mcp(ctx, post_phrase, "write")
    expected = _runtime(ctx, ROOT_FACTS, stack.run_id, "seed")
    evidence["before"] = expected
    pvc = ctx.kube_json("get", "pvc", "cell-data", "-n", namespace)
    pv_name = pvc["spec"]["volumeName"]
    pv = ctx.kube_json("get", "pv", pv_name)
    claim = pv["spec"]["claimRef"]
    if (claim["namespace"], claim["name"], claim["uid"]) != (namespace, "cell-data", pvc["metadata"]["uid"]):
        raise StepFailure("the observer volume is not bound to the provisioned cell's claim")
    # The rehearsal explicitly uses K3s local-path, whose PV exposes this path.
    volume_path = pv["spec"]["hostPath"]["path"]
    marker = str(Path(volume_path) / f".restore-abort-{stack.run_id}.json")

    def witness() -> dict:
        result = run(["docker", "exec", stack.k3s.container, "cat", marker], check=False)
        return json.loads(result.stdout) if result.returncode == 0 else {}

    operator = _start_operator(ctx, snapshot, volume_path)

    def active_writer() -> dict | None:
        state = json.loads(run(["docker", "inspect", operator]).stdout)[0]["State"]
        if not state["Running"]:
            evidence["operator_exit_code"] = state["ExitCode"]
            raise StepFailure(f"the restore operator exited before partial writes (exit {state['ExitCode']}); see its retained log")
        seen = witness()
        return seen if seen.get("writes", 0) >= 2 else None

    try:
        active = wait_for(active_writer, timeout=1800, interval=1,
                          description="the in-place restore to write partial roots", retry_exceptions=())
        pods = ctx.kube_json("get", "pods", "-n", namespace, "-l", f"{JOB_KIND_LABEL}={JOB_KIND_RESTORE}")["items"]
        if len(pods) != 1:
            raise StepFailure("the in-place restore did not have exactly one writer pod")
        pod = pods[0]
        job_name = next(owner["name"] for owner in pod["metadata"]["ownerReferences"] if owner["kind"] == "Job")
        job = ctx.kube_json("get", "job", job_name, "-n", namespace)
        claims = [v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v]
        newer = wait_for(lambda: (seen if (seen := witness()).get("writes", 0) > active["writes"] else None),
                         timeout=30, interval=0.2, description="the restore writer to remain active")
        if (newer.get("state") != "writing" or newer.get("unsafe") or not newer.get("prior_intact")
                or not newer.get("canonical_partial") or not newer.get("markers")
                or claims != ["cell-data"] or job.get("status", {}).get("active") != 1):
            raise StepFailure("the active writer and both moved original roots were not proved before TERM")
        evidence["before_term"] = {"witness": newer, "pod_uid": pod["metadata"]["uid"],
            "job_uid": job["metadata"]["uid"], "job_name": job_name, "claims": claims,
            "pvc_uid": pvc["metadata"]["uid"], "pv": pv_name}
        paused = ctx.kube_json("get", "deployment", "cellctl", "-n", CLOUD_NAMESPACE)
        if paused["spec"]["replicas"] != 0:
            raise StepFailure("the actual restore run did not pause cellctl before the in-place writer")
        paused_row = await ctx.cell(ctx.a.cell_id)
        evidence["before_term"]["controller_replicas"] = paused["spec"]["replicas"]
        expected_partial = {Path(path).name: inode for path, inode in newer["partial_inodes"].items()}
        observer_state = f"/var/lib/exomem-restore/{stack.run_id}/observer.json"
        armed = wait_for(lambda: (seen if (seen := json.loads(run(["docker", "exec", operator, "cat", observer_state]).stdout))["partial_inodes"] == expected_partial else None),
                         timeout=30, interval=0.2, description="both non-original partial inode watches to arm")
        if armed["errors"] or any(inode == armed["original_inodes"][name] for name, inode in expected_partial.items()):
            raise StepFailure("the observer did not arm both non-original partial root watches")
        evidence["before_term"]["observer_partial_inodes"] = armed["partial_inodes"]
        # Signal the actual Bash child; the observer keeps its ordered event stream.
        run(["docker", "exec", operator, "python3", "-c",
             "import json,os,signal,sys;from pathlib import Path;"
             "state=json.loads(Path(sys.argv[1]).read_text());"
             "assert state['armed'] and not state['errors'];"
             "assert state['partial_inodes']==json.loads(sys.argv[2]);"
             "os.kill(state['bash_pid'],signal.SIGTERM)",
             observer_state, json.dumps(expected_partial)])
        stopped = wait_for(lambda: (state if not (state := json.loads(run(["docker", "inspect", operator]).stdout)[0]["State"])["Running"] else None),
                           timeout=1800, interval=2, description="the interrupted operator run to finish recovery")
        evidence["operator_exit_code"] = stopped["ExitCode"]
        diagnostics = stack.workdir / "diagnostics"
        diagnostics.mkdir(exist_ok=True)
        observer_path = diagnostics / "operator-restore-abort-witness.json"
        observed = observer_record(run(["docker", "logs", operator]).stdout)
        observer_path.write_text(json.dumps(observed, indent=2) + "\n")
        evidence["writer_termination"] = observed
        events = observed["events"]
        closed = [index for index, event in enumerate(events) if event["event"] == "writer_closed"]
        returned = [(event["root"], index) for index, event in enumerate(events)
                    if event["event"] == "root_returned" and event["original"]]
        deleted = [(event["root"], index) for index, event in enumerate(events)
                   if event["event"] == "root_deleted"]
        partial_activity = [index for index, event in enumerate(events) if event["event"] == "partial_activity"]
        if (stopped["ExitCode"] == 0 or observed["bash_exit_code"] in (None, 0)
                or observed["errors"] or len(closed) != 1
                or {name for name, _ in returned} != set(observed["original_inodes"])
                or {name for name, _ in deleted} != set(observed["original_inodes"])
                or any(index <= closed[0] for _, index in [*deleted, *returned])
                or any(index <= closed[0] for index in partial_activity) or witness().get("unsafe")):
            raise StepFailure("the writer fd did not close before partial file or root removal and original root return")
        ctx.kubectl("wait", "--for=condition=Ready", "pod/cell-0", "-n", namespace, "--timeout=600s")
        wait_rollout(stack, CLOUD_NAMESPACE, "cellctl")
        observed = await ctx.wait_cell(ctx.a.cell_id,
            lambda row: row.get("ready") is True and row.get("hold_kind") is None
            and row.get("observed_at") is not None and row["observed_at"] > paused_row["observed_at"],
            timeout=300, description="cellctl reconciliation to resume after recovery")
        controller = ctx.kube_json("get", "deployment", "cellctl", "-n", CLOUD_NAMESPACE)
        evidence["controller"] = {"replicas_before": replicas, "replicas_after": controller["spec"]["replicas"],
            "ready_replicas": controller["status"].get("readyReplicas"),
            "observed_at": observed["observed_at"].isoformat(), "cell_ready": ctx.pod_ready(ctx.a.cell_id)}
        recovered = _runtime(ctx, ROOT_FACTS, stack.run_id, "read")
        evidence["recovered"] = recovered
        if (recovered["roots"] != expected["roots"] or not recovered["sentinel_preserved"]
                or recovered["sentinel_uid"] != 10001 or recovered["sentinel_mode"] != 0o600
                or not recovered["partial_absent"] or not recovered["prior_absent"]
                or any(fact != {"uid": 10001, "mode": 0o700} for fact in recovered["roots"].values())
                or controller["spec"]["replicas"] != replicas or controller["status"].get("readyReplicas") != replicas
                or not evidence["controller"]["cell_ready"]):
            raise StepFailure("recovery lost the post-snapshot host sentinel, root ownership or controller readiness")
        evidence["post_snapshot_recall"] = _mcp(ctx, post_phrase, "recall")
        fresh = f"After recovery {stack.run_id}"
        evidence["new_write"] = _mcp(ctx, fresh, "write")
        evidence["new_recall"] = _mcp(ctx, fresh, "recall")
        scratch = f"exo-scratch-{ctx.a.cell_id}-{stack.run_id}"
        remaining = {
            "writer_jobs": ctx.kube_json("get", "jobs", "-n", namespace, "-l", f"{JOB_KIND_LABEL}={JOB_KIND_RESTORE}")["items"],
            "writer_pods": ctx.kube_json("get", "pods", "-n", namespace, "-l", f"{JOB_KIND_LABEL}={JOB_KIND_RESTORE}")["items"],
            "helpers": ctx.kube_json("get", "pods", "-n", namespace, "-l", "app.kubernetes.io/name=cloud-restore-helper")["items"],
            "scratch_namespaces": [n for n in ctx.kube_json("get", "namespaces")["items"] if n["metadata"]["name"] == scratch],
            "scratch_pvs": [p for p in ctx.kube_json("get", "pv")["items"] if p["spec"].get("claimRef", {}).get("namespace") == scratch],
        }
        evidence["resource_absence"] = {kind: len(objects) for kind, objects in remaining.items()}
        evidence["original_pvc_preserved"] = ctx.kube_json("get", "pvc", "cell-data", "-n", namespace)["metadata"]["uid"] == pvc["metadata"]["uid"]
        if any(remaining.values()) or not evidence["original_pvc_preserved"]:
            raise StepFailure("restore resources remained before global teardown or the original claim changed")
    finally:
        logs = run(["docker", "logs", operator], check=False)
        diagnostics = stack.workdir / "diagnostics"
        diagnostics.mkdir(exist_ok=True)
        (diagnostics / "operator-restore-abort.log").write_text(logs.stdout + logs.stderr)
