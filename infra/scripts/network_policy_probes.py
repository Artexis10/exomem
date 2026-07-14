#!/usr/bin/env python3
"""Capture live targets and prove every tenant network boundary fails closed."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

_CURL_IMAGE = (
    "docker.io/curlimages/curl:8.18.0@sha256:"
    "d94d07ba9e7d6de898b6d96c1a072f6f8266c687af78a74f380087a0addf5d17"
)
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


class NetworkProbeError(RuntimeError):
    """A target is stale or a network boundary did not fail closed."""


@dataclass(frozen=True)
class LiveService:
    namespace: str
    name: str
    uid: str


@dataclass(frozen=True)
class LiveTargets:
    cluster_uid: str
    source: LiveService
    peer: LiveService
    neon_host: str
    b2_host: str

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> LiveTargets:
        if document.get("schema_version") != 1:
            raise NetworkProbeError("network target evidence has an unsupported schema")

        def service(name: str) -> LiveService:
            value = document.get(name)
            if not isinstance(value, dict):
                raise NetworkProbeError("network target evidence is incomplete")
            namespace = value.get("namespace")
            service_name = value.get("service")
            uid = value.get("uid")
            if (
                not isinstance(namespace, str)
                or not namespace
                or not isinstance(service_name, str)
                or not service_name
                or not isinstance(uid, str)
                or not uid
            ):
                raise NetworkProbeError("network target evidence is incomplete")
            return LiveService(namespace, service_name, uid)

        cluster_uid = document.get("cluster_uid")
        neon_host = document.get("neon_host")
        b2_host = document.get("b2_host")
        if (
            not isinstance(cluster_uid, str)
            or not cluster_uid
            or not isinstance(neon_host, str)
            or not _DNS_NAME.fullmatch(neon_host)
            or not isinstance(b2_host, str)
            or not _DNS_NAME.fullmatch(b2_host)
        ):
            raise NetworkProbeError("network target evidence is incomplete")
        return cls(cluster_uid, service("source"), service("peer"), neon_host, b2_host)

    def as_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "cluster_uid": self.cluster_uid,
            "source": {
                "namespace": self.source.namespace,
                "service": self.source.name,
                "uid": self.source.uid,
            },
            "peer": {
                "namespace": self.peer.namespace,
                "service": self.peer.name,
                "uid": self.peer.uid,
            },
            "neon_host": self.neon_host,
            "b2_host": self.b2_host,
        }


@dataclass(frozen=True)
class Probe:
    name: str
    namespace: str
    target: str
    expect_denied: bool = True
    executor: str = "existing-cell"


def _run(kubectl: str, arguments: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [kubectl, *arguments],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkProbeError("network probe execution failed") from exc


def _get_json(kubectl: str, arguments: list[str]) -> dict[str, Any]:
    result = _run(kubectl, ["get", *arguments, "-o", "json"])
    if result.returncode != 0:
        raise NetworkProbeError("network probe target is unavailable")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NetworkProbeError("network probe target returned invalid identity") from exc
    if not isinstance(value, dict):
        raise NetworkProbeError("network probe target returned invalid identity")
    return value


def _service_identity(kubectl: str, namespace: str, service: str) -> LiveService:
    resource = _get_json(kubectl, ["service", service, "-n", namespace])
    metadata = resource.get("metadata")
    spec = resource.get("spec")
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
        or not isinstance(spec, dict)
        or spec.get("clusterIP") in (None, "", "None")
    ):
        raise NetworkProbeError("network probe target service has no stable identity")
    endpoints = _get_json(kubectl, ["endpoints", service, "-n", namespace])
    subsets = endpoints.get("subsets")
    if not isinstance(subsets, list) or not any(
        isinstance(subset, dict)
        and isinstance(subset.get("addresses"), list)
        and bool(subset["addresses"])
        for subset in subsets
    ):
        raise NetworkProbeError("network probe target service has no ready endpoints")
    return LiveService(namespace, service, metadata["uid"])


def capture_live_targets(
    *,
    kubectl: str,
    source_namespace: str,
    source_service: str,
    peer_namespace: str,
    peer_service: str,
    neon_host: str,
    b2_host: str,
) -> LiveTargets:
    namespace = _get_json(kubectl, ["namespace", "kube-system"])
    metadata = namespace.get("metadata")
    cluster_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if not isinstance(cluster_uid, str) or not cluster_uid:
        raise NetworkProbeError("cluster identity is unavailable")
    document = {
        "schema_version": 1,
        "cluster_uid": cluster_uid,
        "source": {
            "namespace": source_namespace,
            "service": source_service,
            "uid": _service_identity(kubectl, source_namespace, source_service).uid,
        },
        "peer": {
            "namespace": peer_namespace,
            "service": peer_service,
            "uid": _service_identity(kubectl, peer_namespace, peer_service).uid,
        },
        "neon_host": neon_host,
        "b2_host": b2_host,
    }
    return LiveTargets.from_document(document)


def verify_live_targets(targets: LiveTargets, kubectl: str) -> None:
    namespace = _get_json(kubectl, ["namespace", "kube-system"])
    metadata = namespace.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("uid") != targets.cluster_uid:
        raise NetworkProbeError("network target evidence belongs to another cluster")
    for expected in (targets.source, targets.peer):
        current = _service_identity(kubectl, expected.namespace, expected.name)
        if current.uid != expected.uid:
            raise NetworkProbeError("network target evidence is stale")


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
                                "--connect-timeout 2 --max-time 3 --max-redirs 0 "
                                "--proto '=http,https' \"$1\"; then exit 23; fi",
                                "deny-probe",
                                probe.target,
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


def _positive_job(plan: list[Probe]) -> dict[str, Any]:
    targets = [probe.target for probe in plan]
    job = _job(Probe("positive-controls", "exomem-platform", targets[0], False))
    job["metadata"]["name"] = "exomem-network-positive-controls"
    job["metadata"]["labels"]["exomem.io/network-probe"] = "positive-v1"
    template = job["spec"]["template"]
    template["metadata"]["labels"] = {
        "exomem.io/network-probe": "positive-v1",
        "app.kubernetes.io/name": "traefik",
        "exomem.io/ingress": "traefik",
    }
    container = template["spec"]["containers"][0]
    container["name"] = "positive-controls"
    container["args"] = [
        "for target do curl --silent --show-error "
        "--output /dev/null --insecure --connect-timeout 3 --max-time 8 "
        "--max-redirs 0 --proto '=http,https' \"$target\"; done",
        "positive-controls",
        *targets,
    ]
    return job


def render_probe_manifest(plan: list[Probe]) -> str:
    if not plan or any(not probe.expect_denied for probe in plan):
        raise NetworkProbeError("network probe plan must contain only deny assertions")
    temporary = [probe for probe in plan if probe.executor == "temporary-platform-pod"]
    if len(temporary) != 1:
        raise NetworkProbeError("network probe plan has an invalid temporary probe set")
    return yaml.safe_dump_all([_job(probe) for probe in temporary], sort_keys=False)


def _apply_and_wait(kubectl: str, manifest: str, namespace: str, job_name: str) -> None:
    applied = _run(kubectl, ["apply", "-f", "-"], stdin=manifest)
    if applied.returncode != 0:
        raise NetworkProbeError(f"network probe could not start: {job_name}")
    waited = _run(
        kubectl,
        ["wait", "--for=condition=complete", "--timeout=40s", "-n", namespace, f"job/{job_name}"],
    )
    if waited.returncode != 0:
        raise NetworkProbeError(f"network probe did not prove its assertion: {job_name}")


def _execute_from_existing_cell(probe: Probe, kubectl: str, cell_service: str) -> None:
    parsed = urlsplit(probe.target)
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    if not parsed.hostname or port is None:
        raise NetworkProbeError("network probe target is invalid")
    program = (
        "import socket,sys; s=socket.socket(); s.settimeout(3); "
        "\ntry: s.connect((sys.argv[1],int(sys.argv[2])))\n"
        "except OSError: sys.exit(0)\n"
        "else: s.close(); sys.exit(23)"
    )
    result = _run(
        kubectl,
        [
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
    )
    if result.returncode != 0:
        raise NetworkProbeError(f"network boundary failed closed check: {probe.name}")


def execute_probe_manifest(
    manifest: str,
    plan: list[Probe],
    kubectl: str,
    cell_service: str,
    *,
    targets: LiveTargets,
) -> None:
    try:
        verify_live_targets(targets, kubectl)
        positive = yaml.safe_dump(_positive_job(plan), sort_keys=False)
        _apply_and_wait(kubectl, positive, "exomem-platform", "exomem-network-positive-controls")
        for probe in plan:
            if probe.executor == "existing-cell":
                _execute_from_existing_cell(probe, kubectl, cell_service)
        platform_probe = next(
            probe for probe in plan if probe.executor == "temporary-platform-pod"
        )
        _apply_and_wait(
            kubectl,
            manifest,
            platform_probe.namespace,
            f"exomem-deny-{platform_probe.name}",
        )
    finally:
        for selector in ("positive-v1", "deny-v1"):
            try:
                _run(
                    kubectl,
                    [
                        "delete",
                        "jobs",
                        "-n",
                        "exomem-platform",
                        f"--selector=exomem.io/network-probe={selector}",
                        "--ignore-not-found",
                    ],
                )
            except NetworkProbeError:
                pass


def _plan(targets: LiveTargets) -> list[Probe]:
    return build_probe_plan(
        source_namespace=targets.source.namespace,
        peer_namespace=targets.peer.namespace,
        cell_service=targets.source.name,
        peer_service=targets.peer.name,
        neon_host=targets.neon_host,
        b2_host=targets.b2_host,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="kubectl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    for option in ("source-namespace", "source-service", "peer-namespace", "peer-service", "neon-host", "b2-host"):
        capture.add_argument(f"--{option}", required=True)
    capture.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--targets", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            targets = capture_live_targets(
                kubectl=args.kubectl,
                source_namespace=args.source_namespace,
                source_service=args.source_service,
                peer_namespace=args.peer_namespace,
                peer_service=args.peer_service,
                neon_host=args.neon_host,
                b2_host=args.b2_host,
            )
            descriptor = args.output.parent
            descriptor.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(targets.as_document(), indent=2) + "\n", encoding="utf-8")
            args.output.chmod(0o600)
            print(f"Captured network target identities in {args.output}")
            return 0
        document = json.loads(args.targets.read_text(encoding="utf-8"))
        targets = LiveTargets.from_document(document)
        plan = _plan(targets)
        execute_probe_manifest(
            render_probe_manifest(plan),
            plan,
            args.kubectl,
            targets.source.name,
            targets=targets,
        )
        print("Network policy boundaries verified against live targets")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, NetworkProbeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
