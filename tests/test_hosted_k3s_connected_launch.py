"""Ordinary Substrate admission through real provisioning, ingress and memory.

This opt-in rehearsal needs the companion Substrate checkout. It owns one
disposable PostgreSQL container and the first-provision drill's K3s cluster;
it never contacts the live service or consumes an operator invitation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("RUN_K3S_CONNECTED_LAUNCH_TEST") != "1":
    pytest.skip(
        "set RUN_K3S_CONNECTED_LAUNCH_TEST=1 for the connected rehearsal", allow_module_level=True
    )

for required_flag in ("RUN_K3S_FIRST_PROVISION_DRILL_TEST", "RUN_K3S_GOVERNANCE_DRILL_TEST"):
    assert os.environ.get(required_flag) == "1", f"connected rehearsal requires {required_flag}=1"
for required_package in ("exomem_provisioner", "kubernetes"):
    assert importlib.util.find_spec(required_package), f"missing dependency: {required_package}"

import test_hosted_k3s_first_provision_drill as provision  # noqa: E402
from exomem_provisioner.lifecycle import OpaqueProviderMetadata  # noqa: E402
from exomem_provisioner.models import OperationState  # noqa: E402
from hosted_cluster_bridge import (  # noqa: E402
    control_ingress_proxy,
    disposable_postgres,
    provisioner_api,
    substrate_process,
    wait_for_provision,
)
from test_hosted_k3s_first_provision_drill import (  # noqa: E402, F401
    drill_images,
    first_provision_k3s,
)
from test_hosted_k3s_governance_drill import _host_kubectl, _observe_custody  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def connected_requirements():
    assert provision.HELM, "connected rehearsal requires HELM_BIN"
    assert provision.shutil_which("docker"), "connected rehearsal requires Docker"
    companion = os.environ.get("SUBSTRATE_REHEARSAL_REPO")
    assert companion, "SUBSTRATE_REHEARSAL_REPO is required; this lane cannot count a skip"
    assert (Path(companion) / "scripts/hosted-cluster-rehearsal.ts").is_file()


def _acceptance_module():
    path = Path(__file__).resolve().parents[1] / "infra/scripts/accept_hosted_service.py"
    spec = importlib.util.spec_from_file_location("connected_launch_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capture_and_recall(connection: dict[str, Any], *, capture: bool, run_id: str) -> str:
    acceptance = _acceptance_module()
    client = acceptance.MCPClient(
        endpoint=connection["mcp_endpoint"],
        access_token=connection["access_token"],
        allow_loopback_fixture=True,
    )
    client.initialize()
    names = {tool["name"] for tool in client.list_tools()["tools"]}
    assert {"remember", "ask_memory", "read_memory"} <= names
    fact = f"The {run_id} observatory keeps its emergency telescope key in a violet ceramic owl."
    if capture:
        arguments = {
            "title": f"Connected rehearsal {run_id}",
            "content": f"## Observations\n- [acceptance] {fact} #hosted ^{run_id}",
            "note_type": "insight",
            "sources": [],
            "response_detail": "full",
        }
        validation = acceptance._tool_result(client.capture({**arguments, "validate_only": True}))
        # The pinned v4 hosted command returns the released leaf response,
        # rather than the newer compact MCP mutation terminal.
        diagnostics = validation
        assert diagnostics["mutated"] is False
        assert diagnostics["has_non_review_blockers"] is False
        for field in ("draft_id", "draft_hash", "draft_token"):
            assert isinstance(diagnostics[field], str) and diagnostics[field]
        commit = {
            **arguments,
            **{name: diagnostics[name] for name in ("draft_id", "draft_hash", "draft_token")},
        }
        if diagnostics.get("reviewed_none_required"):
            assert diagnostics.get("committable_after_review") is True
            assert diagnostics["relation_review_hash"] == diagnostics["draft_hash"]
            commit.update(
                {
                    "relation_disposition": "reviewed_none",
                    "relation_review_hash": diagnostics["relation_review_hash"],
                    "relation_review_reason": "Isolated synthetic fixture with no existing related note.",
                }
            )
        else:
            assert diagnostics["committable_without_review"] is True
        terminal = acceptance._tool_result(client.capture(commit, idempotency_key=run_id))
        assert terminal["path"] == diagnostics["destination"]
        creation = terminal["creation"]
        assert creation["mutated"] is True
        assert terminal["path"] in creation["written_paths"]
        assert creation["creation"]["draft_id"] == diagnostics["draft_id"]
        assert creation["creation"]["draft_hash"] == diagnostics["draft_hash"]

    deadline = time.monotonic() + 120
    while True:
        recalled = acceptance._tool_result(
            client.recall(f"Where is the spare access key for the telescope at {run_id} stored?")
        )
        hits = recalled.get("hits", recalled.get("result"))
        assert isinstance(hits, list), "recall must return the released hit collection"
        for hit in hits:
            citation = hit.get("path") if isinstance(hit, dict) else None
            if not isinstance(citation, str) or not citation.startswith("Knowledge Base/"):
                continue
            readback = acceptance._tool_result(
                client.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}})
            )
            # Compact hits need not include the complete observation. The
            # returned citation must independently resolve to this run's fact.
            if fact in json.dumps(readback):
                return citation
        if time.monotonic() >= deadline:
            raise acceptance.AcceptanceError("paraphrased recall has no citation for this run fact")
        time.sleep(1)


@pytest.mark.timeout(1800)
def test_ordinary_admission_reaches_serving_and_memory_survives_runtime_restart(
    first_provision_k3s,  # noqa: F811
    drill_images,  # noqa: F811
    tmp_path_factory,
    monkeypatch,
):
    companion = os.environ.get("SUBSTRATE_REHEARSAL_REPO")
    assert companion, "SUBSTRATE_REHEARSAL_REPO is required; this lane cannot count a skip"
    repo = Path(companion).resolve()
    assert (repo / "scripts/hosted-cluster-rehearsal.ts").is_file()
    _k3s, kubeconfig = first_provision_k3s
    scratch = tmp_path_factory.mktemp("connected-hosted-launch")
    scratch.chmod(0o700)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    provision._install_platform_prerequisites(kubeconfig, scratch)
    network = provision._start_network(kubeconfig)

    async def connected():
        settings = provision._provider_settings(scratch, drill_images)
        database, repository = await provision._operation_store(scratch)
        owner = None
        try:
            async with (
                disposable_postgres() as database_url,
                provisioner_api(
                    database=database,
                    repository=repository,
                    lock_path=settings.deployment_lock_path,
                    codec=provision.CODEC,
                ) as (provider_url, bearer),
                control_ingress_proxy(network.traefik_origin) as ingress_url,
                substrate_process(
                    repo=repo,
                    state_dir=scratch / "substrate",
                    database_url=database_url,
                    provider_url=provider_url,
                    provider_bearer=bearer,
                    ingress_url=ingress_url,
                ) as node,
            ):
                operation_id, request = await wait_for_provision(
                    database=database,
                    repository=repository,
                    node=node,
                )
                owner = OpaqueProviderMetadata(
                    request["tenantId"],
                    request["cellId"],
                    request["operationId"],
                    request["fenceGeneration"],
                )
                drill, _ = await provision._compose(
                    scratch,
                    kubeconfig,
                    drill_images,
                    owner=owner,
                    database_repository=(database, repository),
                    settings=settings,
                    request=request,
                    network=network,
                )
                drill.operation_id = operation_id
                await drill.drive_until(lambda _checkpoint: False)
                stored = await repository.get_by_id(operation_id)
                assert stored is not None and stored.state is OperationState.FINAL
                cell = drill.cell(await drill.pvc_uid())
                custody = await asyncio.to_thread(_observe_custody, cell, now=int(time.time()))
                assert custody.membership_schema_version == 4
                assert custody.governance_enrolled and custody.replica_state == "SERVING"

                # Substrate polls the real API for the final provision result and
                # submits its own health operation before binding the candidate.
                async with asyncio.timeout(120):
                    while (connection := node.connection()) is None:
                        await drill.collector.publish()
                        await drill.routine.run_once(now=datetime.now(UTC) + provision.CLOCK_LEAD)
                        await asyncio.sleep(0.2)
                assert connection["tenant_id"] == owner.tenant_id
                assert connection["cell_id"] == owner.subject_id
                run_id = f"cluster-{owner.subject_id[:12]}"
                citation = await asyncio.to_thread(
                    _capture_and_recall,
                    connection,
                    capture=True,
                    run_id=run_id,
                )

                pods_before = json.loads(
                    _host_kubectl(
                        kubeconfig,
                        [
                            "get",
                            "pods",
                            "-n",
                            owner.resource_name,
                            "-o",
                            "json",
                        ],
                    ).stdout
                )
                serving_pods = [
                    pod
                    for pod in pods_before["items"]
                    if any(
                        owner_ref["kind"] == "StatefulSet"
                        for owner_ref in pod["metadata"].get("ownerReferences", [])
                    )
                ]
                assert len(serving_pods) == 1
                old_pod = serving_pods[0]
                await asyncio.to_thread(
                    _host_kubectl,
                    kubeconfig,
                    [
                        "delete",
                        "pod",
                        old_pod["metadata"]["name"],
                        "-n",
                        owner.resource_name,
                        "--wait=true",
                        "--timeout=120s",
                    ],
                )
                await asyncio.to_thread(
                    _host_kubectl,
                    kubeconfig,
                    [
                        "rollout",
                        "status",
                        f"statefulset/{owner.resource_name}",
                        "-n",
                        owner.resource_name,
                        "--timeout=180s",
                    ],
                )
                new_pod = json.loads(
                    _host_kubectl(
                        kubeconfig,
                        [
                            "get",
                            "pod",
                            old_pod["metadata"]["name"],
                            "-n",
                            owner.resource_name,
                            "-o",
                            "json",
                        ],
                    ).stdout
                )
                assert new_pod["metadata"]["uid"] != old_pod["metadata"]["uid"]
                after_citation = await asyncio.to_thread(
                    _capture_and_recall,
                    connection,
                    capture=False,
                    run_id=run_id,
                )
                assert after_citation == citation
                drill.trace.write(
                    "connected-serving-memory",
                    summary={
                        "ordinaryAdmission": True,
                        "providerApi": "real",
                        "ingress": "real",
                        "runtimeRelease": drill_images.release,
                        "custody": "SERVING",
                        "captureAndCitedRecall": True,
                        "runtimePodRestart": True,
                        "tenantId": owner.tenant_id,
                        "cellId": owner.subject_id,
                        "substitutions": [
                            "local-path CSI metadata",
                            "recording Hetzner API",
                            "disposable provisioner SQLite",
                            "cached Claude metadata",
                            "loopback control-host proxy",
                        ],
                    },
                )
        except BaseException:
            if owner is not None:
                # Preserve owned runtime evidence before the cluster fixture's
                # teardown. These private logs are not publication artifacts.
                for filename, arguments in (
                    ("runtime-pods.json", ["get", "pods", "-o", "json"]),
                    (
                        "runtime.log",
                        [
                            "logs",
                            f"statefulset/{owner.resource_name}",
                            "--all-containers=true",
                            "--tail=200",
                        ],
                    ),
                ):
                    try:
                        result = await asyncio.to_thread(
                            _host_kubectl,
                            kubeconfig,
                            [*arguments, "-n", owner.resource_name],
                            check=False,
                        )
                        descriptor = os.open(
                            scratch / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(descriptor, "w") as stream:
                            stream.write(result.stdout + result.stderr)
                    except Exception:  # noqa: BLE001 - diagnostics cannot replace the test failure
                        pass  # Preserve the original failure if diagnostics fail.
            raise
        finally:
            await database.dispose()

    try:
        asyncio.run(connected())
    finally:
        network.close()
