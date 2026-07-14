#!/usr/bin/env python3
"""Render or execute destructive-free deny probes for every tenant network boundary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yaml

_CURL_IMAGE = (
    "docker.io/curlimages/curl:8.18.0@sha256:"
    "d94d07ba9e7d6de898b6d96c1a072f6f8266c687af78a74f380087a0addf5d17"
)


class NetworkProbeError(RuntimeError):
    """A network boundary did not fail closed."""


@dataclass(frozen=True)
class Probe:
    name: str
    namespace: str
    target: str
    expect_denied: bool = True
    executor: str = "existing-cell"


def build_probe_plan(
    *,
    source_namespace: str,
    peer_namespace: str,
    cell_service: str,
    peer_service: str,
    neon_host: str,
    b2_host: str,
) -> list[Probe]:
    return [
        Probe(
            "cell-to-cell",
            source_namespace,
            f"http://{peer_service}.{peer_namespace}.svc.cluster.local:8765/private/exomem/v1/live",
        ),
        Probe("kubernetes-api", source_namespace, "https://kubernetes.default.svc:443/readyz"),
        Probe("neon", source_namespace, f"https://{neon_host}:443/"),
        Probe("b2", source_namespace, f"https://{b2_host}:443/"),
        Probe("cloud-metadata", source_namespace, "http://169.254.169.254/latest/meta-data/"),
        Probe(
            "unlabelled-platform-ingress",
            "exomem-platform",
            f"http://{cell_service}.{source_namespace}.svc.cluster.local:8765/private/exomem/v1/live",
            executor="temporary-platform-pod",
        ),
    ]


def _job(probe: Probe) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"exomem-deny-{probe.name}",
            "namespace": probe.namespace,
            "labels": {"exomem.io/network-probe": "deny-v1"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 20,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"exomem.io/network-probe": "deny-v1"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "deny-probe",
                            "image": _CURL_IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-ec"],
                            "args": [
                                "if curl --silent --show-error --output /dev/null "
                                "--connect-timeout 2 --max-time 3 --proto '=http,https' "
                                f"{probe.target}; then exit 23; fi"
                            ],
                            "resources": {
                                "requests": {"cpu": "5m", "memory": "8Mi"},
                                "limits": {"cpu": "50m", "memory": "24Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def render_probe_manifest(plan: list[Probe]) -> str:
    if not plan or any(not probe.expect_denied for probe in plan):
        raise NetworkProbeError("network probe plan must contain only deny assertions")
    temporary = [probe for probe in plan if probe.executor == "temporary-platform-pod"]
    if len(temporary) != 1:
        raise NetworkProbeError("network probe plan has an invalid temporary probe set")
    return yaml.safe_dump_all([_job(probe) for probe in temporary], sort_keys=False)


def _execute_from_existing_cell(probe: Probe, kubectl: str, cell_service: str) -> None:
    parsed = urlsplit(probe.target)
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    if not parsed.hostname or port is None:
        raise NetworkProbeError("network probe target is invalid")
    program = (
        "import socket,sys; "
        "s=socket.socket(); s.settimeout(3); "
        "\ntry: s.connect((sys.argv[1],int(sys.argv[2])))\n"
        "except OSError: sys.exit(0)\n"
        "else: s.close(); sys.exit(23)"
    )
    result = subprocess.run(
        [
            kubectl,
            "exec",
            "-n",
            probe.namespace,
            f"statefulset/{cell_service}",
            "--",
            "python",
            "-c",
            program,
            parsed.hostname,
            str(port),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise NetworkProbeError(f"network boundary failed closed check: {probe.name}")


def execute_probe_manifest(
    manifest: str, plan: list[Probe], kubectl: str, cell_service: str
) -> None:
    try:
        for probe in plan:
            if probe.executor == "existing-cell":
                _execute_from_existing_cell(probe, kubectl, cell_service)
        service = subprocess.run(
            [
                kubectl,
                "get",
                "service",
                "-n",
                next(probe.namespace for probe in plan if probe.name == "cell-to-cell"),
                cell_service,
                "-o",
                "jsonpath={.spec.clusterIP}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        cluster_ip = service.stdout.strip()
        if service.returncode != 0 or not cluster_ip:
            raise NetworkProbeError("network probe target service is unavailable")
        platform_probe = next(
            probe for probe in plan if probe.executor == "temporary-platform-pod"
        )
        platform_manifest = manifest.replace(platform_probe.target, f"http://{cluster_ip}:8765/")
        applied = subprocess.run(
            [kubectl, "apply", "-f", "-"],
            input=platform_manifest,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if applied.returncode != 0:
            raise NetworkProbeError("network probe resources could not be applied")
        for probe in plan:
            if probe.executor != "temporary-platform-pod":
                continue
            waited = subprocess.run(
                [
                    kubectl,
                    "wait",
                    "--for=condition=complete",
                    "--timeout=30s",
                    "-n",
                    probe.namespace,
                    f"job/exomem-deny-{probe.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=40,
            )
            if waited.returncode != 0:
                raise NetworkProbeError(f"network boundary failed closed check: {probe.name}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkProbeError("network probe execution failed") from exc
    finally:
        try:
            subprocess.run(
                [
                    kubectl,
                    "delete",
                    "jobs",
                    "-n",
                    "exomem-platform",
                    "--selector=exomem.io/network-probe=deny-v1",
                    "--ignore-not-found",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-namespace", required=True)
    parser.add_argument("--peer-namespace", required=True)
    parser.add_argument("--cell-service", required=True)
    parser.add_argument("--peer-service", required=True)
    parser.add_argument("--neon-host", required=True)
    parser.add_argument("--b2-host", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args()
    try:
        plan = build_probe_plan(
            source_namespace=args.source_namespace,
            peer_namespace=args.peer_namespace,
            cell_service=args.cell_service,
            peer_service=args.peer_service,
            neon_host=args.neon_host,
            b2_host=args.b2_host,
        )
        manifest = render_probe_manifest(plan)
        if args.execute:
            execute_probe_manifest(manifest, plan, args.kubectl, args.cell_service)
            print(json.dumps({"denied": [probe.name for probe in plan]}, sort_keys=True))
        else:
            print(manifest, end="")
    except NetworkProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
