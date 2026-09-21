from __future__ import annotations

import hashlib

import pytest
import yaml
from test_hosted_activation_ack_http import tls_material as tls_material
from test_hosted_helm_contract import CELL, _find, _render, _render_process

from exomem.hosted_activation_ack_protocol import PROTOCOL


@pytest.fixture
def activation_cell_values(tmp_path, tls_material):
    pem = tls_material[0].read_text()
    binding = {
        "protocol": PROTOCOL, "platformNamespace": "exomem-platform",
        "trustBundleSha256": hashlib.sha256(pem.encode()).hexdigest(), "trustBundlePem": pem,
    }
    values = {
        "activationAcknowledgement": binding,
        "providerRecoveryEnvelopes": {
            "activationAckTrustConfigMap": "r" * 64,
            "activationAckEgressNetworkPolicy": "s" * 64,
        },
    }
    path = tmp_path / "ack-values.yaml"
    path.write_text(yaml.safe_dump(values))
    return path, values


def test_capable_cell_renders_private_socket_and_exact_public_trust(activation_cell_values):
    path, values = activation_cell_values
    documents = _render(CELL, CELL / "values.validation.yaml", namespace="cell-alpha-test", extra_args=("--values", str(path)))
    stateful = _find(documents, "StatefulSet", "cell-alpha")["spec"]["template"]["spec"]
    assert "fsGroup" not in stateful["securityContext"]
    initializer, helper = stateful["initContainers"]
    runtime, = stateful["containers"]
    env = {item["name"]: item.get("value") for item in runtime["env"]}
    assert env["EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL"] == PROTOCOL
    assert env["EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET"] == "/run/exomem/activation-ack/ack.sock"
    assert helper["restartPolicy"] == "Always"
    assert helper["args"] == ["-m", "exomem.governance.authorization_hosted_mount", "--watch"]
    socket = next(m for m in runtime["volumeMounts"] if m["name"] == "activation-ack-socket")
    assert socket == {"name": "activation-ack-socket", "mountPath": "/run/exomem/activation-ack", "subPath": "socket", "readOnly": True}
    init_socket = next(m for m in initializer["volumeMounts"] if m["name"] == "activation-ack-socket")
    assert "subPath" not in init_socket
    assert not init_socket.get("readOnly", False)
    assert not any(m["name"] == "activation-ack-trust" for m in runtime["volumeMounts"])
    assert any(m["name"] == "credentials" and m["readOnly"] for m in helper["volumeMounts"])
    volumes = {volume["name"]: volume for volume in stateful["volumes"]}
    assert volumes["authorization-session-custody"]["emptyDir"]["sizeLimit"] == "2Mi"
    digest = values["activationAcknowledgement"]["trustBundleSha256"]
    name = "exomem-ack-ca-" + digest[:40]
    trust = _find(documents, "ConfigMap", name)
    assert trust["immutable"] is True
    assert trust["data"] == {"ca.pem": values["activationAcknowledgement"]["trustBundlePem"]}
    assert trust["metadata"]["annotations"]["exomem.io/recovery-envelope"] == "r" * 64
    network = _find(documents, "NetworkPolicy", "cell-alpha-activation-ack-egress")["spec"]
    assert network["policyTypes"] == ["Egress"]
    assert network["egress"][1]["ports"] == [{"protocol": "TCP", "port": 8443}]


@pytest.mark.parametrize("mutation", ["digest", "partial", "unknown", "missing-envelope", "duplicate-envelope"])
def test_cell_refuses_unbound_or_ambiguous_acknowledgement(activation_cell_values, mutation):
    path, values = activation_cell_values
    if mutation == "digest":
        values["activationAcknowledgement"]["trustBundleSha256"] = "f" * 64
    elif mutation == "partial":
        values["activationAcknowledgement"]["protocol"] = ""
    elif mutation == "unknown":
        values["activationAcknowledgement"]["endpoint"] = "https://other.invalid"
    elif mutation == "missing-envelope":
        del values["providerRecoveryEnvelopes"]["activationAckTrustConfigMap"]
    else:
        values["providerRecoveryEnvelopes"]["activationAckTrustConfigMap"] = "s" * 64
    path.write_text(yaml.safe_dump(values))
    rendered = _render_process(CELL, CELL / "values.validation.yaml", namespace="cell-alpha-test", extra_args=("--values", str(path)))
    assert rendered.returncode != 0


def _platform_worker(documents: list[dict]) -> dict:
    return _find(documents, "Deployment", "exomem-provisioner-worker")["spec"]["template"]["spec"]


def test_capable_platform_serves_the_acknowledgement_listener(tmp_path):
    from test_hosted_activation_admission import PLATFORM_NAMESPACE, _activation_lock_values
    from test_hosted_helm_contract import PLATFORM

    override, _ = _activation_lock_values(tmp_path)
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        extra_args=("--values", str(override)),
    )

    service = _find(documents, "Service", "exomem-activation-ack")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"]["app.kubernetes.io/name"] == "exomem-provisioner-worker"
    assert service["spec"]["ports"] == [
        {"name": "activation-ack", "protocol": "TCP", "port": 8443, "targetPort": "activation-ack"}
    ]

    worker = _platform_worker(documents)
    container, = worker["containers"]
    assert {"name": "activation-ack", "containerPort": 8443, "protocol": "TCP"} in container["ports"]

    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["EXOMEM_PROVIDER_ACTIVATION_ACK_PROTOCOL"] == PROTOCOL
    assert env["EXOMEM_PROVIDER_ACTIVATION_ACK_TLS_CERT_PATH"] == (
        "/run/exomem/activation-ack-tls/tls.crt"
    )
    assert env["EXOMEM_PROVIDER_ACTIVATION_ACK_TLS_KEY_PATH"] == (
        "/run/exomem/activation-ack-tls/tls.key"
    )

    # Each key by subPath, never a directory mount. Secret projection publishes
    # a key as a symlink into ..data/, and `_read_certificate` refuses a symlink,
    # which would refuse the listener -- and with it the worker -- on every start.
    mounts = sorted(
        (m for m in container["volumeMounts"] if m["name"] == "activation-ack-tls"),
        key=lambda m: m["mountPath"],
    )
    assert [(m["mountPath"], m["subPath"], m["readOnly"]) for m in mounts] == [
        ("/run/exomem/activation-ack-tls/tls.crt", "tls.crt", True),
        ("/run/exomem/activation-ack-tls/tls.key", "tls.key", True),
    ]
    volume = next(v for v in worker["volumes"] if v["name"] == "activation-ack-tls")
    assert volume["secret"]["secretName"] == "exomem-activation-ack-tls"
    # Not 0400: this pod sets no fsGroup, so the projection stays root-owned and
    # the worker's own UID 10001 would be denied its own certificate.
    assert volume["secret"]["defaultMode"] == 0o444
    assert "fsGroup" not in worker.get("securityContext", {})

    # The private key never reaches the general admission API.
    api = _find(documents, "Deployment", "exomem-provisioner-api")["spec"]["template"]["spec"]
    assert all(v["name"] != "activation-ack-tls" for v in api["volumes"])


def test_legacy_platform_has_no_acknowledgement_listener():
    import json as _json

    from test_hosted_activation_admission import PLATFORM_NAMESPACE
    from test_hosted_helm_contract import PLATFORM

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace=PLATFORM_NAMESPACE)
    assert not any(
        document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == "exomem-activation-ack"
        for document in documents
    )
    worker = _platform_worker(documents)
    container, = worker["containers"]
    assert all(port.get("containerPort") != 8443 for port in container.get("ports", []))
    assert "ACTIVATION_ACK" not in _json.dumps(container["env"])
    assert all(volume["name"] != "activation-ack-tls" for volume in worker["volumes"])
