"""Opt-in disposable K3s proof of the fresh-cell first-consumer storage boundary.

This uses K3s's real local-path dynamic provisioner, not Hetzner CSI. It proves
only Kubernetes scheduling/binding behavior, not production volume guarantees.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_hosted_k3s_admission import (
    CELL,
    K3S_IMAGE,
    ROOT,
    RUNTIME_GATE,
    shutil_which,
)

HELM = os.environ.get("HELM_BIN") or shutil_which("helm")
RUN_STORAGE = os.environ.get("RUN_K3S_STORAGE_BINDING_TEST") == "1"
STORAGE_CLASS = "exomem-hcloud-encrypted-retain"


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def _run_bounded(
    command: list[str],
    *,
    timeout: float,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        raise AssertionError(f"command timed out after {timeout}s") from error
    except BaseException:
        _kill_process_group(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise AssertionError(stdout + stderr)
    return result


def _run(
    command: list[str],
    *,
    timeout: float = 30,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_bounded(command, timeout=timeout, input_text=input_text, check=check, env=env)


def _cleanup_owned_container(
    name: str,
    run_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    label = "exomem.io/storage-feasibility-id"
    owned = runner(
        ["docker", "inspect", name, "--format", "{{json .Config.Labels}}"],
        check=False,
        timeout=10,
    )
    if owned.returncode == 0:
        assert json.loads(owned.stdout)[label] == run_id
        removed = runner(["docker", "rm", "--force", "--volumes", name], timeout=30)
        assert removed.returncode == 0
    else:
        assert "No such object" in owned.stderr or "No such container" in owned.stderr


def _kubectl(
    k3s: str,
    arguments: list[str],
    *,
    documents: list[dict[str, Any]] | None = None,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "exec", "--interactive", k3s, "kubectl", *arguments],
        input_text=None if documents is None else yaml.safe_dump_all(documents),
        check=check,
        timeout=timeout,
    )


def _host_kubeconfig(k3s: str, output: Path) -> Path:
    published = _run(["docker", "port", k3s, "6443/tcp"]).stdout.strip()
    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", published)
    assert match is not None
    configuration = _run(["docker", "exec", k3s, "cat", "/etc/rancher/k3s/k3s.yaml"]).stdout
    output.write_text(
        configuration.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{match.group(1)}"),
        encoding="utf-8",
    )
    output.chmod(0o600)
    return output


def _build_current_runtime_image() -> tuple[str, dict[str, str]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gate = json.loads(RUNTIME_GATE.read_text(encoding="utf-8"))
    image = f"exomem-hosted-storage-binding:{uuid.uuid4().hex[:12]}"
    try:
        _run(
            [
                "docker",
                "build",
                "--target",
                gate["dockerTarget"],
                "--build-arg",
                f"EXOMEM_RELEASE_BUILD_TIME={gate['releaseBuildTime']}",
                "--tag",
                image,
                str(ROOT),
            ],
            timeout=240,
        )
    except BaseException:
        _run(["docker", "image", "rm", image], check=False, timeout=20)
        raise
    return image, {"release": project["project"]["version"], "hostedProtocol": "1"}


def _import_runtime_image(k3s: str, image: str) -> str:
    save = subprocess.Popen(
        ["docker", "save", image],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    imported: subprocess.Popen[bytes] | None = None
    try:
        assert save.stdout is not None
        imported = subprocess.Popen(
            ["docker", "exec", "--interactive", k3s, "ctr", "images", "import", "-"],
            cwd=ROOT,
            stdin=save.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        save.stdout.close()
        import_stdout, import_stderr = imported.communicate(timeout=150)
        save_returncode = save.wait(timeout=20)
    except BaseException:
        if imported is not None:
            _kill_process_group(imported)
        _kill_process_group(save)
        raise
    assert imported.returncode == 0, (import_stdout + import_stderr).decode(errors="replace")
    assert save_returncode == 0, "docker save failed during bounded image import"

    images = _run(["docker", "exec", k3s, "ctr", "images", "ls"]).stdout
    imported_reference: str | None = None
    manifest_digest: str | None = None
    for line in images.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0].endswith(image):
            imported_reference, manifest_digest = fields[0], fields[2]
            break
    assert imported_reference is not None, images
    assert manifest_digest is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest)
    digest_reference = f"ghcr.io/artexis10/exomem@{manifest_digest}"
    _run(["docker", "exec", k3s, "ctr", "images", "tag", imported_reference, digest_reference])
    return digest_reference


@pytest.mark.skipif(sys.platform != "linux", reason="K3s subprocess-group proof requires Linux")
def test_stalled_child_is_terminated_and_owned_cleanup_runs(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    cleanup: list[str] = []
    source = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(30)"
    )
    child_pid: int | None = None

    def fake_docker(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == "inspect":
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"exomem.io/storage-feasibility-id": "fake-run"}), ""
            )
        assert command == ["docker", "rm", "--force", "--volumes", "fake-container"]
        cleanup.append("removed owned container")
        return subprocess.CompletedProcess(command, 0, "fake-container\n", "")

    try:
        with pytest.raises(AssertionError, match="command timed out"):
            try:
                _run_bounded([sys.executable, "-c", source, str(child_pid_path)], timeout=0.5)
            finally:
                cleanup.append("owned fixture cleanup")
                _cleanup_owned_container("fake-container", "fake-run", runner=fake_docker)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert cleanup == ["owned fixture cleanup", "removed owned container"]
        deadline = time.monotonic() + 2
        while _pid_executing(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_executing(child_pid)
    finally:
        if child_pid is None and child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if child_pid is not None and _pid_executing(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _pid_executing(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1][0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _get(k3s: str, kind: str, name: str, *, namespace: str | None = None) -> dict[str, Any]:
    args = ["get", kind, name, "--output=json"]
    if namespace is not None:
        args.extend(["--namespace", namespace])
    return json.loads(_kubectl(k3s, args).stdout)


def _assert_observed_binding_pod(
    pod: dict[str, Any], job: dict[str, Any], *, image: str, claim_name: str
) -> None:
    """Check the admitted Pod, not only the Job manifest we submitted."""
    actual_spec = pod["spec"]
    assert actual_spec["automountServiceAccountToken"] is False
    assert actual_spec["securityContext"]["runAsNonRoot"] is True
    assert actual_spec["securityContext"]["runAsUser"] == 10001
    assert actual_spec["securityContext"]["runAsGroup"] == 10001
    assert actual_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert len(actual_spec["containers"]) == 1
    container = actual_spec["containers"][0]
    assert container["name"] == "bind"
    assert container["image"] == image
    assert container["command"] == ["/bin/true"]
    assert not container.get("args")
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert all(
        not container.get("volumeMounts")
        and not container.get("volumeDevices")
        and not container.get("env")
        and not container.get("envFrom")
        for container in actual_spec["containers"]
    )
    assert actual_spec.get("initContainers", []) == []
    assert actual_spec.get("ephemeralContainers", []) == []
    assert actual_spec.get("volumes") == [
        {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}}
    ]
    owners = pod["metadata"].get("ownerReferences", [])
    assert len(owners) == 1
    assert owners[0]["kind"] == "Job"
    assert owners[0]["name"] == job["metadata"]["name"]
    assert owners[0]["uid"] == job["metadata"]["uid"]
    assert owners[0]["controller"] is True
    assert pod["status"]["phase"] == "Succeeded"
    statuses = pod["status"].get("containerStatuses", [])
    assert len(statuses) == 1
    assert statuses[0]["name"] == "bind"
    assert statuses[0]["state"]["terminated"]["exitCode"] == 0
    assert statuses[0]["state"]["terminated"]["reason"] == "Completed"


@pytest.mark.parametrize("mutation", ["image", "command", "extra-container", "failed", "owner"])
def test_observed_binding_pod_rejects_non_inert_execution(mutation: str) -> None:
    image = "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
    job = {"metadata": {"name": "cell-init", "uid": "job-uid"}}
    pod = {
        "metadata": {
            "ownerReferences": [
                {"kind": "Job", "name": "cell-init", "uid": "job-uid", "controller": True}
            ]
        },
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "bind",
                    "image": image,
                    "command": ["/bin/true"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "cell-data"}}],
        },
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "bind",
                    "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
                }
            ],
        },
    }
    mutated = copy.deepcopy(pod)
    if mutation == "image":
        mutated["spec"]["containers"][0]["image"] = "other-image"
    elif mutation == "command":
        mutated["spec"]["containers"][0]["command"] = ["/bin/sh"]
    elif mutation == "extra-container":
        mutated["spec"]["containers"].append({"name": "sidecar", "image": image})
    elif mutation == "failed":
        mutated["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
    else:
        mutated["metadata"]["ownerReferences"][0]["uid"] = "other-job-uid"
    with pytest.raises(AssertionError):
        _assert_observed_binding_pod(mutated, job, image=image, claim_name="cell-data")


@pytest.fixture
def storage_k3s(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    if not RUN_STORAGE:
        pytest.skip(
            "set RUN_K3S_STORAGE_BINDING_TEST=1 for the disposable dynamic-storage K3s proof"
        )
    if HELM is None:
        pytest.skip("set HELM_BIN or install helm for the disposable K3s storage proof")
    if not shutil_which("docker"):
        pytest.skip("Docker is required for the disposable K3s storage proof")
    if sys.platform != "linux":
        pytest.skip("the disposable K3s storage proof uses Linux process-group cleanup")
    if _run(["docker", "image", "inspect", K3S_IMAGE], check=False).returncode != 0:
        pytest.skip("the exact pinned K3s image must be cached before the storage proof")

    run_id = uuid.uuid4().hex
    name = f"exomem-storage-{run_id[:12]}"
    label = "exomem.io/storage-feasibility-id"
    try:
        _run(
            [
                "docker",
                "run",
                "--privileged",
                "--detach",
                "--name",
                name,
                "--label",
                f"{label}={run_id}",
                "--publish",
                "127.0.0.1::6443",
                K3S_IMAGE,
                "server",
                "--disable=traefik",
                "--disable=servicelb",
                "--write-kubeconfig-mode=600",
            ]
        )
        ready_deadline = time.monotonic() + 90
        while time.monotonic() < ready_deadline:
            ready = _run(
                ["docker", "exec", name, "kubectl", "get", "--raw=/readyz"],
                check=False,
                timeout=5,
            )
            if ready.returncode == 0 and ready.stdout.strip() == "ok":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("disposable K3s API did not become ready")
        provisioner_deadline = time.monotonic() + 90
        while time.monotonic() < provisioner_deadline:
            provisioner = _kubectl(
                name,
                ["get", "deployment/local-path-provisioner", "--namespace=kube-system"],
                check=False,
                timeout=5,
            )
            if provisioner.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("bundled local-path provisioner was not installed")
        _kubectl(
            name,
            [
                "rollout",
                "status",
                "deployment/local-path-provisioner",
                "--namespace=kube-system",
                "--timeout=90s",
            ],
            timeout=100,
        )
        yield name, _host_kubeconfig(name, tmp_path / "kubeconfig")
    finally:
        _cleanup_owned_container(name, run_id)


@pytest.mark.timeout(900, method="signal")
def test_production_binding_adapter_binds_and_proves_own_cleanup(
    storage_k3s: tuple[str, Path],
) -> None:
    from exomem_provisioner.adapters import KubernetesCellAdapter
    from exomem_provisioner.governance_storage_binding import (
        KubernetesGovernanceStorageBindingAdapter,
    )
    from exomem_provisioner.lifecycle import OpaqueProviderMetadata
    from exomem_provisioner.provider_identity import (
        ProviderRecoveryIdentityCodec,
        ProviderReference,
    )
    from kubernetes import client, config

    k3s, kubeconfig = storage_k3s
    suffix = uuid.uuid4().hex[:10]
    metadata = OpaqueProviderMetadata(
        f"tenant-{suffix}", f"cell-{suffix}", f"operation-{suffix}", 7
    )
    namespace = metadata.resource_name
    claim_name = f"{namespace}-data"
    job_name = f"{namespace}-init"
    codec = ProviderRecoveryIdentityCodec.from_secret(f"binding-test-{suffix}")

    def envelope(kind: str, api_version: str, name: str) -> str:
        return codec.seal(
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version=api_version,
                kind=kind,
                namespace=namespace,
                name=name,
            ),
            tenant_id=metadata.tenant_id,
            cell_id=metadata.subject_id,
            operation_id=metadata.operation_id,
            fence_generation=metadata.fence_generation,
        )

    _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": STORAGE_CLASS},
                "provisioner": "rancher.io/local-path",
                "reclaimPolicy": "Retain",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
            {
                "apiVersion": "node.k8s.io/v1",
                "kind": "RuntimeClass",
                "metadata": {"name": "exomem-storage-init"},
                "handler": "runc",
            },
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        ],
    )
    configuration = client.Configuration()
    config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    api_client = client.ApiClient(configuration=configuration)
    image: str | None = None
    try:
        core = client.CoreV1Api(api_client)
        batch = client.BatchV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        core.create_namespaced_service_account(
            namespace, client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=namespace))
        )
        core.create_namespaced_persistent_volume_claim(
            namespace,
            client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(
                    name=claim_name,
                    annotations=metadata.kubernetes_annotations
                    | {
                        "exomem.io/recovery-envelope": envelope(
                            "PersistentVolumeClaim", "v1", claim_name
                        )
                    },
                ),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteOnce"],
                    volume_mode="Filesystem",
                    storage_class_name=STORAGE_CLASS,
                    resources=client.V1VolumeResourceRequirements(requests={"storage": "10Gi"}),
                ),
            ),
        )
        cell = KubernetesCellAdapter(core_v1=core, apps_v1=apps, identity_verifier=codec.verifier())

        async def exercise() -> None:
            pvc_uid, phase = await cell.authenticated_volume_state(metadata)
            assert phase == "Pending"
            pending_claim = _get(k3s, "pvc", claim_name, namespace=namespace)
            assert pending_claim["metadata"]["uid"] == pvc_uid
            assert not pending_claim["spec"].get("volumeName")
            volumes_before = json.loads(_kubectl(k3s, ["get", "pv", "--output=json"]).stdout)[
                "items"
            ]
            assert all(
                volume.get("spec", {}).get("claimRef", {}).get("uid") != pvc_uid
                for volume in volumes_before
            )
            assert (
                json.loads(_kubectl(k3s, ["get", "pods", "-n", namespace, "--output=json"]).stdout)[
                    "items"
                ]
                == []
            )

            nonlocal image
            image, _ = _build_current_runtime_image()
            runtime_image = _import_runtime_image(k3s, image)
            job_envelope = envelope("Job", "batch/v1", job_name)
            deleted: list[dict[str, Any]] = []
            actual_delete = batch.delete_namespaced_job

            def record_delete(name: str, target_namespace: str, **kwargs: Any) -> Any:
                assert (name, target_namespace) == (job_name, namespace)
                deleted.append(kwargs)
                return actual_delete(name, target_namespace, **kwargs)

            batch.delete_namespaced_job = record_delete
            adapter = KubernetesGovernanceStorageBindingAdapter(
                core_v1=core,
                batch_v1=batch,
                apps_v1=apps,
                identity_verifier=codec.verifier(),
                runtime_image=runtime_image,
                cell=cell,
            )

            async def effect_guard() -> None:
                observed_uid, _ = await cell.authenticated_volume_state(metadata)
                assert observed_uid == pvc_uid

            assert not await adapter.reconcile(
                metadata,
                pvc_uid=pvc_uid,
                recovery_envelope=job_envelope,
                effect_guard=effect_guard,
            )
            job = _get(k3s, "job", job_name, namespace=namespace)
            assert job["metadata"]["annotations"]["exomem.io/job-purpose"] == "storage-binding"
            assert job["metadata"]["annotations"]["exomem.io/recovery-envelope"] == job_envelope
            assert job["spec"]["activeDeadlineSeconds"] == 90
            assert job["spec"]["ttlSecondsAfterFinished"] == 60
            _kubectl(
                k3s,
                [
                    "wait",
                    "--for=condition=Complete",
                    f"job/{job_name}",
                    "-n",
                    namespace,
                    "--timeout=120s",
                ],
                timeout=130,
            )
            bound_uid, bound_phase = await cell.authenticated_volume_state(metadata)
            claim = _get(k3s, "pvc", claim_name, namespace=namespace)
            volume = _get(k3s, "pv", claim["spec"]["volumeName"])
            assert bound_uid == pvc_uid == claim["metadata"]["uid"]
            assert bound_phase == "Bound"
            assert volume["spec"]["claimRef"]["uid"] == pvc_uid
            pods = json.loads(
                _kubectl(k3s, ["get", "pods", "-n", namespace, "--output=json"]).stdout
            )["items"]
            assert len(pods) == 1
            pod = pods[0]
            assert pod["metadata"]["ownerReferences"][0]["uid"] == job["metadata"]["uid"]
            assert pod["status"]["phase"] == "Succeeded"
            assert pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] == 0
            pod_spec = pod["spec"]
            assert pod_spec["runtimeClassName"] == "exomem-storage-init"
            assert pod_spec["automountServiceAccountToken"] is False
            assert pod_spec["volumes"] == [
                {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}}
            ]
            assert not pod_spec.get("initContainers") and not pod_spec.get("ephemeralContainers")
            assert len(pod_spec["containers"]) == 1
            container = pod_spec["containers"][0]
            assert container["image"] == runtime_image
            assert container["command"] == ["/bin/true"]
            assert not any(
                container.get(key) for key in ("volumeMounts", "volumeDevices", "env", "envFrom")
            )

            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if await adapter.reconcile(
                    metadata,
                    pvc_uid=pvc_uid,
                    recovery_envelope=job_envelope,
                    effect_guard=effect_guard,
                ):
                    break
                await asyncio.sleep(0.4)
            else:
                raise AssertionError("binding adapter did not prove Job and Pod absence")
            assert deleted
            delete_body = deleted[0]["body"]
            assert delete_body["propagationPolicy"] == "Foreground"
            assert delete_body["preconditions"]["uid"] == job["metadata"]["uid"]
            assert delete_body["preconditions"]["resourceVersion"]
            assert not _kubectl(
                k3s,
                ["get", "job", job_name, "-n", namespace, "--ignore-not-found", "--output=json"],
            ).stdout.strip()
            assert (
                json.loads(_kubectl(k3s, ["get", "pods", "-n", namespace, "--output=json"]).stdout)[
                    "items"
                ]
                == []
            )
            assert await cell.authenticated_volume_state(metadata) == (pvc_uid, "Bound")

        asyncio.run(exercise())
    finally:
        api_client.close()
        if image is not None:
            _run(["docker", "image", "rm", image], check=False, timeout=30)


@pytest.mark.timeout(900, method="signal")
def test_restore_shell_waits_for_first_consumer_then_unmounted_job_binds(
    storage_k3s: tuple[str, Path],
) -> None:
    k3s, kubeconfig = storage_k3s
    suffix = uuid.uuid4().hex[:10]
    namespace = f"exo-storage-{suffix}"
    resource = namespace
    claim_name = f"{resource}-data"

    _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": STORAGE_CLASS},
                "provisioner": "rancher.io/local-path",
                "reclaimPolicy": "Retain",
                "volumeBindingMode": "WaitForFirstConsumer",
            }
        ],
    )
    value_args = [
        "--values",
        str(CELL / "values.validation.yaml"),
        "--set",
        "workloadMode=restore",
        "--set",
        "provisionMode=restore-candidate",
        "--set",
        f"resourceName={resource}",
    ]
    rendered = _run(
        [str(HELM), "template", resource, str(CELL), "--namespace", namespace, *value_args]
    )
    namespace_document = next(
        item for item in yaml.safe_load_all(rendered.stdout) if item.get("kind") == "Namespace"
    )
    namespace_document["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
    namespace_document["metadata"]["annotations"].update(
        {
            "meta.helm.sh/release-name": resource,
            "meta.helm.sh/release-namespace": namespace,
        }
    )
    _kubectl(k3s, ["apply", "--filename=-"], documents=[namespace_document])
    helm = _run(
        [
            str(HELM),
            "upgrade",
            "--install",
            resource,
            str(CELL),
            "--namespace",
            namespace,
            "--create-namespace=false",
            *value_args,
            "--wait",
            "--timeout=12s",
        ],
        check=False,
        env=os.environ | {"KUBECONFIG": str(kubeconfig)},
        timeout=20,
    )
    assert helm.returncode != 0, helm.stdout + helm.stderr
    assert "context deadline exceeded" in helm.stderr, helm.stdout + helm.stderr
    pvc = _get(k3s, "pvc", claim_name, namespace=namespace)
    assert pvc["spec"]["storageClassName"] == STORAGE_CLASS
    assert pvc["status"]["phase"] == "Pending"
    pods = json.loads(
        _kubectl(k3s, ["get", "pods", "--namespace", namespace, "--output=json"]).stdout
    )
    assert pods["items"] == []
    pvs = json.loads(_kubectl(k3s, ["get", "pv", "--output=json"]).stdout)
    assert pvs["items"] == []
    events = json.loads(
        _kubectl(k3s, ["get", "events", "--namespace", namespace, "--output=json"]).stdout
    )
    claim_events = [
        item
        for item in events["items"]
        if item.get("involvedObject", {}).get("uid") == pvc["metadata"]["uid"]
    ]
    assert any(item.get("reason") == "WaitForFirstConsumer" for item in claim_events), claim_events
    print(
        json.dumps(
            {
                "stage": "storage-only-red",
                "helm_exit": helm.returncode,
                "pvc_uid": pvc["metadata"]["uid"],
                "pvc_phase": pvc["status"]["phase"],
                "pv_count": len(pvs["items"]),
                "pod_count": len(pods["items"]),
                "claim_event_reasons": [item["reason"] for item in claim_events],
            },
            sort_keys=True,
        )
    )

    image, gate = _build_current_runtime_image()
    try:
        runtime_image = _import_runtime_image(k3s, image)
        ownership = {
            key: value
            for key, value in namespace_document["metadata"]["annotations"].items()
            if key
            in {
                "exomem.io/tenant-id",
                "exomem.io/cell-id",
                "exomem.io/operation-id",
                "exomem.io/tenant-digest",
                "exomem.io/subject-digest",
                "exomem.io/operation-digest",
                "exomem.io/fence",
            }
        }
        values = yaml.safe_load((CELL / "values.validation.yaml").read_text(encoding="utf-8"))
        job_name = f"{resource}-init"
        binding_job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": {"exomem.io/storage-binding": "true"},
                "annotations": ownership
                | {
                    "exomem.io/recovery-envelope": values["providerRecoveryEnvelopes"]["initJob"],
                    "exomem.io/job-purpose": "binding-only",
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 90,
                "ttlSecondsAfterFinished": 60,
                "template": {
                    "metadata": {"labels": {"exomem.io/storage-binding": "true"}},
                    "spec": {
                        "serviceAccountName": resource,
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "bind",
                                "image": runtime_image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/true"],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "16Mi"},
                                    "limits": {"cpu": "100m", "memory": "64Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": claim_name},
                            }
                        ],
                    },
                },
            },
        }
        proposed_spec = binding_job["spec"]["template"]["spec"]
        assert len(proposed_spec["volumes"]) == 1
        assert "initContainers" not in proposed_spec
        assert "ephemeralContainers" not in proposed_spec
        assert "nodeName" not in proposed_spec
        assert "schedulerName" not in proposed_spec
        assert all(
            not container.get("volumeMounts")
            and not container.get("volumeDevices")
            and not container.get("env")
            and not container.get("envFrom")
            for container in proposed_spec["containers"]
        )
        _kubectl(k3s, ["apply", "--filename=-"], documents=[binding_job])
        complete = _kubectl(
            k3s,
            [
                "wait",
                "--for=condition=Complete",
                f"job/{job_name}",
                "--namespace",
                namespace,
                "--timeout=120s",
            ],
            check=False,
            timeout=130,
        )
        if complete.returncode != 0:
            pod_state = _kubectl(k3s, ["get", "pods", "--namespace", namespace, "--output=json"])
            events_state = _kubectl(
                k3s, ["get", "events", "--namespace", namespace, "--output=json"]
            )
            raise AssertionError(
                complete.stdout + complete.stderr + pod_state.stdout + events_state.stdout
            )

        bound_pvc = _get(k3s, "pvc", claim_name, namespace=namespace)
        pv = _get(k3s, "pv", bound_pvc["spec"]["volumeName"])
        job = _get(k3s, "job", job_name, namespace=namespace)
        pod_list = json.loads(
            _kubectl(k3s, ["get", "pods", "--namespace", namespace, "--output=json"]).stdout
        )
        assert len(pod_list["items"]) == 1
        _assert_observed_binding_pod(
            pod_list["items"][0], job, image=runtime_image, claim_name=claim_name
        )
        assert bound_pvc["metadata"]["uid"] == pvc["metadata"]["uid"]
        assert bound_pvc["status"]["phase"] == "Bound"
        assert pv["status"]["phase"] == "Bound"
        assert pv["spec"]["claimRef"]["uid"] == pvc["metadata"]["uid"]
        assert (
            pv["metadata"]["annotations"]["pv.kubernetes.io/provisioned-by"]
            == "rancher.io/local-path"
        )
        assert job["status"]["succeeded"] == 1
        print(
            json.dumps(
                {
                    "stage": "unmounted-consumer-green",
                    "pvc_uid": bound_pvc["metadata"]["uid"],
                    "pvc_phase": bound_pvc["status"]["phase"],
                    "pv_uid": pv["metadata"]["uid"],
                    "pv_phase": pv["status"]["phase"],
                    "pv_provisioner": pv["metadata"]["annotations"][
                        "pv.kubernetes.io/provisioned-by"
                    ],
                    "job_succeeded": job["status"]["succeeded"],
                    "runtime_image": runtime_image,
                    "runtime_release": gate["release"],
                    "k3s_version": _run(
                        ["docker", "exec", k3s, "k3s", "--version"]
                    ).stdout.splitlines()[0],
                    "kernel_version": _run(["docker", "exec", k3s, "uname", "-r"]).stdout.strip(),
                },
                sort_keys=True,
            )
        )
    finally:
        _run(["docker", "image", "rm", image], timeout=20)
