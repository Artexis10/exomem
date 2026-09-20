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
