from __future__ import annotations

from dataclasses import replace

import pytest
from test_hosted_activation_ack_client import helper as helper
from test_hosted_activation_delivery import NOW, bundle
from test_hosted_mutation_journal import _committed_fixture

from exomem import hosted_activation_ack_client as client_module
from exomem.governance import authorization_custody as custody
from exomem.governance import schema_v4, store
from exomem.hosted_activation_ack_protocol import PROTOCOL, encode_message
from exomem.hosted_activation_delivery import parse_delivery_bundle


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    governance, _, _ = _committed_fixture(tmp_path)
    root = tmp_path / "vault"
    root.mkdir()
    destination = tmp_path / "custody"
    destination.mkdir(mode=0o700)
    initial = bundle(activation=7, activation_digest="a" * 64, attachment_epoch=7)
    for name, raw in initial.items():
        (destination / name).write_bytes(raw)
        (destination / name).chmod(0o600)
    for variable, name in (
        (custody.KEYRING_FILE_ENV, "keyring.json"),
        (custody.CONTROL_FILE_ENV, "control.json"),
        (custody.MEMBERSHIP_FILE_ENV, "serving-membership.json"),
    ):
        monkeypatch.setenv(variable, str(destination / name))
    monkeypatch.setenv(client_module.PROTOCOL_ENV, PROTOCOL)
    monkeypatch.setenv(client_module.SOCKET_ENV, str(client_module.SOCKET_PATH))
    monkeypatch.setattr(custody.time, "time", lambda: NOW)
    monkeypatch.setattr(store, "sidecar_path", lambda _: governance)
    expected = parse_delivery_bundle(initial, now=NOW).control
    target = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="vault-1",
        activation_store_id="store-1",
        activation_epoch=8,
        activation_state_digest="b" * 64,
        policy_generation_id="policy-1",
        policy_fingerprint="c" * 64,
        projector_schema_version=1,
        catalog_generation=8,
        projection_namespace_id="namespace-1",
    )
    return root, destination, expected, target


def _reply(request, *, activation, revision, operation):
    return encode_message(
        {
            "protocol": PROTOCOL,
            "request_id": request["request_id"],
            "status": "ready" if operation == "check" else "acknowledged",
            "bundle_revision": revision,
            "activation": activation,
            "code": "ACK_READY" if operation == "check" else "ACKNOWLEDGED",
            "retry_after_ms": 0,
        },
        "udsResponse",
    )


def test_hosted_preflight_uses_socket_while_runtime_custody_is_readonly(
    bridge, helper, monkeypatch
):
    root, destination, expected, _ = bridge
    parsed = parse_delivery_bundle(
        bundle(activation=7, activation_digest="a" * 64, attachment_epoch=7), now=NOW
    )
    client = helper(
        lambda request: _reply(
            request,
            activation={
                name: getattr(expected, name)
                for name in ("activation_store_id", "activation_epoch", "activation_state_digest")
            },
            revision=parsed.revision,
            operation="check",
        )
    )
    monkeypatch.setattr(client_module, "ActivationAcknowledgementClient", lambda: client)
    destination.chmod(0o500)
    try:
        custody.require_activation_acknowledgement_available(root)
    finally:
        destination.chmod(0o700)


def test_exact_acknowledgement_accepts_a_concurrent_signed_membership_renewal(
    bridge, helper, monkeypatch
):
    root, destination, expected, target = bridge
    installed = bundle(activation=8, activation_digest="b" * 64, attachment_epoch=7, membership=2)

    def acknowledge(request):
        assert request["publication"]["publication_event_id"] == "publication-1"
        for name, raw in installed.items():
            (destination / name).write_bytes(raw)
        return _reply(
            request,
            activation=request["publication"]["successor"],
            revision=parse_delivery_bundle(installed, now=NOW).revision,
            operation="ack",
        )

    client = helper(acknowledge)
    monkeypatch.setattr(client_module, "ActivationAcknowledgementClient", lambda: client)
    result = custody.acknowledge_activation_tuple(
        root, expected_control=expected, target=target, now=NOW
    )
    assert result.activation_epoch == 8


def test_acknowledged_socket_reply_without_installed_successor_is_refused(
    bridge, helper, monkeypatch
):
    root, _, expected, target = bridge
    client = helper(
        lambda request: _reply(
            request,
            activation=request["publication"]["successor"],
            revision="f" * 64,
            operation="ack",
        )
    )
    monkeypatch.setattr(client_module, "ActivationAcknowledgementClient", lambda: client)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        custody.acknowledge_activation_tuple(
            root, expected_control=expected, target=target, now=NOW
        )


def test_nonexistent_committed_selector_does_not_contact_helper(bridge, monkeypatch):
    root, _, expected, target = bridge
    monkeypatch.setattr(
        client_module,
        "ActivationAcknowledgementClient",
        lambda: pytest.fail("helper must not be contacted without a committed event"),
    )
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        custody.acknowledge_activation_tuple(
            root,
            expected_control=expected,
            target=replace(target, activation_state_digest="d" * 64),
            now=NOW,
        )
