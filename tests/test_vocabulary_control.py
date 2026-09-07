from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from exomem.governance.principal import RequestPrincipal
from exomem.vocabulary_authority import (
    AuthorityStatus,
    VocabularyAuthorityUnavailable,
    _deployment_floor_for_adapter,
    _trusted_owner_decision_for_adapter,
)
from exomem.vocabulary_control import OwnerControlIntent, VocabularyControl


@dataclass
class _Operation:
    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": "operation-1",
            "command_digest": "a" * 64,
            "effect_digest": "b" * 64,
            "image_digest": "c" * 64,
            "registry_digests": {"entity_types": "d" * 64},
            "target_digests": {"memory:entity-a": "e" * 64},
            "effects": [{"action": "entity.create", "path": "Knowledge Base/Entities/a.md"}],
        }


@dataclass
class _EdgeOperation:
    def as_dict(self) -> dict[str, Any]:
        value = _Operation().as_dict()
        value["effects"] = [
            {
                "action": "edge.add",
                "path": "Knowledge Base/Notes/source.md",
                "key": "edge-1",
                "details": {
                    "source": "memory:source",
                    "target": "memory:target",
                    "relation": "supplies",
                },
            }
        ]
        value["scope_proofs"] = [
            {
                "kind": "project-edge",
                "project_ref": "memory:project-a",
                "endpoints": ["memory:source", "memory:target"],
                "membership_digest": "f" * 64,
            }
        ]
        return value


class _Authority:
    vault_root = Path("/tmp/vocabulary-control-vault")

    def __init__(self) -> None:
        self.approved: list[tuple[str, object]] = []
        self.denied: list[tuple[str, object]] = []
        self.activated: list[tuple[object, object]] = []
        self.grants: list[dict[str, object]] = []

    def runtime_status(self):
        return AuthorityStatus("v1", None, 0)

    def status(self, principal):
        return AuthorityStatus("v2", 7, 0)

    def activate(self, *, principal, decision, deployment_floor, binding):
        self.activated.append((decision, deployment_floor))
        return AuthorityStatus("v2", 7, 0)

    def inspect_request_for_owner(self, request_id, *, principal):
        if request_id == "request-1":
            return _Operation()
        assert request_id == "edge-request"
        return _EdgeOperation()

    def approve_request(self, request_id, *, principal, decision, binding):
        self.approved.append((request_id, decision))
        return "authority-1"

    def deny_request(self, request_id, *, principal, decision, binding):
        self.denied.append((request_id, decision))

    def grant(self, **kwargs):
        self.grants.append(kwargs)
        return "grant-1"

    def revoke(self, **kwargs):
        return None


def _principal() -> RequestPrincipal:
    return RequestPrincipal(audience_id="agent-a", surface="hosted", issuer_family="hosted-gateway")


def test_agent_flags_and_missing_callback_cannot_approve_a_request() -> None:
    authority = _Authority()
    control = VocabularyControl(authority, activation_guard_factory=lambda _root: nullcontext())

    with pytest.raises(ValueError, match="VOCABULARY_CONTROL_INVALID"):
        control.approve_request(
            principal=_principal(),
            body={"request_id": "request-1", "approved": True},
        )
    with pytest.raises(VocabularyAuthorityUnavailable):
        control.approve_request(principal=_principal(), body={"request_id": "request-1"})
    assert authority.approved == []


def test_trusted_callback_receives_complete_stored_operation_before_exact_approval() -> None:
    authority = _Authority()
    seen: list[OwnerControlIntent] = []

    def owner(intent: OwnerControlIntent):
        seen.append(intent)
        return _trusted_owner_decision_for_adapter(
            owner_id="owner",
            ceremony_id="ceremony",
            binding_digest=intent.binding_digest,
            expires_at=2_000_000_000,
        )

    control = VocabularyControl(
        authority,
        owner_decision_callback=owner,
        activation_guard_factory=lambda _root: nullcontext(),
    )

    assert control.approve_request(principal=_principal(), body={"request_id": "request-1"}) == "authority-1"
    assert authority.approved[0][0] == "request-1"
    assert seen[0].canonical_operation == _Operation().as_dict()


def test_callback_cannot_reuse_a_sealed_decision_for_a_different_control_intent() -> None:
    authority = _Authority()
    first: list[object] = []

    def owner(intent: OwnerControlIntent):
        if not first:
            first.append(
                _trusted_owner_decision_for_adapter(
                    owner_id="owner",
                    ceremony_id="one-shot",
                    binding_digest=intent.binding_digest,
                    expires_at=2_000_000_000,
                )
            )
        return first[0]

    control = VocabularyControl(
        authority,
        owner_decision_callback=owner,
        activation_guard_factory=lambda _root: nullcontext(),
    )
    assert control.approve_request(principal=_principal(), body={"request_id": "request-1"})

    with pytest.raises(VocabularyAuthorityUnavailable):
        control.deny_request(principal=_principal(), body={"request_id": "request-1"})


def test_activation_requires_separate_owner_and_deployment_callbacks_under_guard() -> None:
    authority = _Authority()
    events: list[str] = []

    class _Guard:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *_args):
            events.append("exit")

    control = VocabularyControl(
        authority,
        owner_decision_callback=lambda intent: _trusted_owner_decision_for_adapter(
            owner_id="owner",
            ceremony_id="activation",
            binding_digest=intent.binding_digest,
            expires_at=2_000_000_000,
        ),
        deployment_floor_callback=lambda _intent: _deployment_floor_for_adapter(
            runtime="vocabulary-authority/v2", generation=7
        ),
        activation_guard_factory=lambda _root: _Guard(),
    )

    assert control.activate(principal=_principal(), body={}).mode == "v2"
    assert events == ["enter", "exit"]
    assert len(authority.activated) == 1


def test_activation_uses_its_dedicated_admission_guard() -> None:
    authority = _Authority()
    events: list[str] = []

    class _Guard:
        def __enter__(self):
            events.append("activation-enter")

        def __exit__(self, *_args):
            events.append("activation-exit")

    control = VocabularyControl(
        authority,
        owner_decision_callback=lambda intent: _trusted_owner_decision_for_adapter(
            owner_id="owner",
            ceremony_id="activation",
            binding_digest=intent.binding_digest,
            expires_at=2_000_000_000,
        ),
        deployment_floor_callback=lambda _intent: _deployment_floor_for_adapter(
            runtime="vocabulary-authority/v2", generation=7
        ),
        activation_guard_factory=lambda _root: _Guard(),
    )

    assert control.activate(principal=_principal(), body={}).mode == "v2"
    assert events == ["activation-enter", "activation-exit"]


def test_grant_body_is_closed_and_cannot_launder_project_edge_scope() -> None:
    authority = _Authority()
    control = VocabularyControl(
        authority,
        owner_decision_callback=lambda intent: _trusted_owner_decision_for_adapter(
            owner_id="owner",
            ceremony_id="grant",
            binding_digest=intent.binding_digest,
            expires_at=2_000_000_000,
        ),
        activation_guard_factory=lambda _root: nullcontext(),
    )

    with pytest.raises(ValueError, match="VOCABULARY_CONTROL_INVALID"):
        control.grant(
            principal=_principal(),
            body={
                "actions": ["edge.add"],
                "scope": "project-edge",
                "expires_at": 2_000_000_000,
            },
        )


def test_project_edge_grant_derives_exact_scope_from_the_stored_request() -> None:
    authority = _Authority()
    seen: list[OwnerControlIntent] = []

    def owner(intent: OwnerControlIntent):
        seen.append(intent)
        return _trusted_owner_decision_for_adapter(
            owner_id="owner",
            ceremony_id="edge-grant",
            binding_digest=intent.binding_digest,
            expires_at=2_000_000_000,
        )

    control = VocabularyControl(
        authority,
        owner_decision_callback=owner,
        activation_guard_factory=lambda _root: nullcontext(),
    )

    assert control.grant(
        principal=_principal(),
        body={
            "request_id": "edge-request",
            "project_ref": "memory:project-a",
            "expires_at": 2_000_000_000,
        },
    ) == "grant-1"
    assert seen[0].grant_manifest == {
        "actions": ["edge.add"],
        "scope": {
            "kind": "project-edge",
            "project_ref": "memory:project-a",
            "endpoints": ["memory:source", "memory:target"],
            "membership_digest": "f" * 64,
        },
        "expires_at": 2_000_000_000,
    }
