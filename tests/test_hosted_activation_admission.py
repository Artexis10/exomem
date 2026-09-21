from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from test_hosted_helm_contract import PLATFORM, _find, _render

PROTOCOL = "exomem.hosted-activation-ack/v1"
PLATFORM_NAMESPACE = "exomem-platform"
TRUST_SHA256 = "c" * 64


def _activation_lock_values(tmp_path: Path) -> tuple[Path, dict]:
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    lock["activationAcknowledgement"] = {
        "protocol": PROTOCOL,
        "platformNamespace": PLATFORM_NAMESPACE,
        "trustBundleSha256": TRUST_SHA256,
    }
    raw = json.dumps(lock, separators=(",", ":")) + "\n"
    override = tmp_path / "activation-lock.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "deploymentLockJson": raw,
                    "deploymentLockSha256": hashlib.sha256(raw.encode()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return override, lock


def _policy_text(documents: list[dict], name: str) -> str:
    return json.dumps(_find(documents, "ValidatingAdmissionPolicy", name))


def test_legacy_lock_keeps_legacy_admission_shapes() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace=PLATFORM_NAMESPACE)

    tenant = _find(documents, "ValidatingAdmissionPolicy", "exomem-tenant-boundary")
    assert "activationAck" not in {variable["name"] for variable in tenant["spec"]["variables"]}
    tenant_text = json.dumps(tenant)
    assert "size(object.spec.volumes) == 4" in tenant_text
    assert "size(object.spec.containers[0].volumeMounts) == 5" in tenant_text
    assert "size(object.spec.containers[0].env) == 28" in tenant_text
    assert "exomem.io/activation-ack-protocol" not in tenant_text

    assert "activation-ack" not in _policy_text(documents, "exomem-provisioner-scope")
    assert "activation-ack" not in _policy_text(
        documents, "exomem-durability-actions-scope"
    )
    assert "activation-ack" not in _policy_text(
        documents, "exomem-tenant-namespace-contract"
    )


def test_extended_lock_pins_acknowledgement_pod_and_namespace_contract(
    tmp_path: Path,
) -> None:
    override, lock = _activation_lock_values(tmp_path)
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        extra_args=("--values", str(override)),
    )

    tenant = _find(documents, "ValidatingAdmissionPolicy", "exomem-tenant-boundary")
    variables = {variable["name"]: variable["expression"] for variable in tenant["spec"]["variables"]}
    activation = " ".join(variables["activationAck"].split())
    assert "!variables.lifecycleJob" in activation
    assert lock["components"]["runtime"]["image"] in activation
    assert "EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL" in json.dumps(tenant)
    assert "EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET" in json.dumps(tenant)
    assert "EXOMEM_HOSTED_ACTIVATION_ACK_PLATFORM_NAMESPACE" in json.dumps(tenant)
    assert "EXOMEM_AUTH_SESSION_REPLICA_ID" in json.dumps(tenant)
    assert "activation-ack-trust" in json.dumps(tenant)
    assert "activation-ack-socket" in json.dumps(tenant)
    assert "size(object.spec.volumes) == (variables.activationAck ? 6 : 4)" in json.dumps(tenant)
    assert "size(object.spec.containers[0].volumeMounts) == (variables.activationAck ? 6 : 5)" in json.dumps(tenant)
    assert "string(dyn(volume.emptyDir).sizeLimit) == (variables.activationAck ? '2Mi' : '256Ki')" in json.dumps(tenant)

    namespace = _policy_text(documents, "exomem-tenant-namespace-contract")
    for annotation in (
        "exomem.io/activation-ack-protocol",
        "exomem.io/activation-ack-platform-namespace",
        "exomem.io/activation-ack-trust-sha256",
    ):
        assert annotation in namespace
    assert PROTOCOL in namespace
    assert "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$" in namespace
    assert "^[a-f0-9]{64}$" in namespace


def test_extended_lock_pins_only_signed_ack_resources(tmp_path: Path) -> None:
    override, _ = _activation_lock_values(tmp_path)
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        extra_args=("--values", str(override)),
    )

    trust_name = "exomem-ack-ca-" + TRUST_SHA256[:40]
    for policy_name in ("exomem-provisioner-scope", "exomem-durability-actions-scope"):
        policy = _policy_text(documents, policy_name)
        assert trust_name in policy
        assert "activationAckTrustConfigMap" not in policy
        assert "activation-ack-egress" in policy
        assert "exomem.io/recovery-envelope" in policy
        assert "immutable" in policy
        assert "ca.pem" in policy
        assert "kube-system" in policy
        assert "kube-dns" in policy
        assert "exomem-provisioner-worker" in policy
        assert "8443" in policy
        assert "namespaceObject.metadata.annotations['exomem.io/activation-ack-platform-namespace']" in policy



def test_the_pinned_pod_counts_equal_what_the_cell_chart_actually_renders(tmp_path: Path) -> None:
    """Close the half of task 5.2 that offline gates were assumed to miss.

    The tenant policy pins exact sizes. CEL is not evaluated by kubeconform or
    conftest, so a count that drifts from the chart passes every offline gate
    and is only caught by a real API server refusing every cell. The counts
    themselves need no API server, though: they are integers in the rendered
    policy and lengths in the rendered StatefulSet, and comparing the two
    catches drift in either. What still needs the cluster is CEL *semantics* --
    that the expressions mean what we read them to mean.

    Both modes, because the ack-disabled shape is the one a rollback produces
    and the numbers differ in every position.
    """

    import re

    from test_hosted_activation_helm import _cell_statefulset  # noqa: PLC0415

    override, _lock = _activation_lock_values(tmp_path)
    policy = _policy_text(
        _render(
            PLATFORM,
            PLATFORM / "values.validation.yaml",
            namespace=PLATFORM_NAMESPACE,
            extra_args=("--values", str(override)),
        ),
        "exomem-tenant-boundary",
    )

    def pinned_pairs(expression: str) -> set[tuple[int, int]]:
        """Every (ack-on, ack-off) pair this policy pins for one size.

        Two syntactic shapes are in use and both have to be read. Volumes and
        volume mounts put the ternary inside the comparison; env counts put the
        whole comparison inside the ternary, once per pod variant, which is why
        this returns a set rather than one pair.
        """

        sized = re.escape(f"size({expression})")
        pairs = {
            (int(on), int(off))
            for on, off in re.findall(
                sized + r" == \(variables\.activationAck \? (\d+) : (\d+)\)", policy
            )
        }
        pairs |= {
            (int(on), int(off))
            for on, off in re.findall(
                r"variables\.activationAck \? " + sized + r" == (\d+) : " + sized + r" == (\d+)",
                policy,
            )
        }
        assert pairs, f"no pinned activation-ack count pair found for {expression}"
        return pairs

    on = _cell_statefulset(tmp_path, activation_ack=True)
    off = _cell_statefulset(tmp_path, activation_ack=False)

    def rendered(spec: dict, expression: str) -> int:
        container = spec["containers"][0]
        return {
            "object.spec.volumes": len(spec["volumes"]),
            "object.spec.containers[0].env": len(container.get("env", [])),
            "object.spec.containers[0].volumeMounts": len(container.get("volumeMounts", [])),
            "object.spec.initContainers[0].volumeMounts": len(
                spec["initContainers"][0].get("volumeMounts", [])
            ),
        }[expression]

    for expression in (
        "object.spec.volumes",
        "object.spec.containers[0].env",
        "object.spec.containers[0].volumeMounts",
        "object.spec.initContainers[0].volumeMounts",
    ):
        actual = (rendered(on, expression), rendered(off, expression))
        assert actual in pinned_pairs(expression), (
            f"{expression}: chart renders {actual}, policy pins {sorted(pinned_pairs(expression))}"
        )

    # The refresh sidecar's count is pinned flat at 5, but only inside the
    # `!variables.activationAck ||` guard, so it binds the capable shape alone.
    # Asserting the guard explicitly is the point: read without it, the flat 5
    # would look like a rule that refuses every legacy cell.
    assert "(!variables.activationAck ||" in policy
    assert "size(object.spec.initContainers[1].volumeMounts) == 5" in policy
    assert len(on["initContainers"][1]["volumeMounts"]) == 5
    assert len(off["initContainers"][1]["volumeMounts"]) == 2
