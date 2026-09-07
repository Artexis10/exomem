"""Private owner-control adapter for the finite vocabulary authority ceremony.

The service gateway authenticates transport before this adapter is reached.
That authentication identifies a request; it never substitutes for the
separate trusted owner decision supplied by the control-plane callback.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .governance.principal import RequestPrincipal
from .vocabulary_authority import (
    AuthorityScope,
    AuthorityStatus,
    DeploymentFloorProof,
    TrustedOwnerDecision,
    VocabularyAuthority,
    VocabularyAuthorityUnavailable,
)

_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})


@dataclass(frozen=True, slots=True)
class OwnerControlIntent:
    """The finite, reviewed fact passed to a separate human-control callback."""

    action: str
    principal: RequestPrincipal
    request_id: str | None = None
    display_effects: tuple[dict[str, str], ...] = ()
    canonical_operation: dict[str, Any] | None = None
    grant_manifest: dict[str, Any] | None = None
    authority_id: str | None = None
    binding_digest: str = ""


def _intent(
    action: str,
    principal: RequestPrincipal,
    *,
    request_id: str | None = None,
    canonical_operation: dict[str, Any] | None = None,
    grant_manifest: dict[str, Any] | None = None,
    authority_id: str | None = None,
) -> OwnerControlIntent:
    context = principal.verified_authorization_session
    payload = {
        "action": action,
        "audience": principal.audience_id,
        "issuer": principal.issuer_family,
        "session": None if context is None else context.session_id,
        "cell": None if context is None else context.cell_id,
        "logical_vault": None if context is None else context.logical_vault_id,
        "keyring": None if context is None else context.keyring_id,
        "generation": None if context is None else context.credential_generation,
        "request_id": request_id,
        "operation": canonical_operation,
        "grant": grant_manifest,
        "authority_id": authority_id,
    }
    binding = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return OwnerControlIntent(
        action,
        principal,
        request_id=request_id,
        canonical_operation=canonical_operation,
        grant_manifest=grant_manifest,
        authority_id=authority_id,
        binding_digest=binding,
    )


@dataclass(frozen=True, slots=True)
class GrantManifest:
    actions: tuple[str, ...]
    scope: AuthorityScope
    expires_at: int

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> GrantManifest:
        if set(body) != {"actions", "scope", "expires_at"}:
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        raw_actions = body["actions"]
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        actions = tuple(sorted(set(raw_actions)))
        if any(not isinstance(action, str) or action not in _ACTIONS for action in actions):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        if body["scope"] != "vault":
            # Project edge scope requires canonical endpoint-membership proof
            # from the classifier; request JSON can never provide that proof.
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        expires_at = body["expires_at"]
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < 1:
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        return cls(actions, AuthorityScope.vault_wide(), expires_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": list(self.actions),
            "scope": self.scope.as_dict(),
            "expires_at": self.expires_at,
        }

    @classmethod
    def project_edge_from_request(
        cls, body: Mapping[str, Any], operation: object
    ) -> GrantManifest:
        if set(body) != {"request_id", "project_ref", "expires_at"}:
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        project_ref = body["project_ref"]
        expires_at = body["expires_at"]
        if (
            not isinstance(project_ref, str)
            or not project_ref
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at < 1
            or not hasattr(operation, "as_dict")
        ):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        record = operation.as_dict()
        effects = record.get("effects")
        proofs = record.get("scope_proofs")
        if (
            not isinstance(effects, list)
            or not effects
            or any(not isinstance(effect, Mapping) or effect.get("action") != "edge.add" for effect in effects)
            or not isinstance(proofs, list)
        ):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        selected = [
            proof
            for proof in proofs
            if isinstance(proof, Mapping)
            and proof.get("kind") == "project-edge"
            and proof.get("project_ref") == project_ref
        ]
        if len(selected) != 1:
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        proof = selected[0]
        endpoints = proof.get("endpoints")
        digest = proof.get("membership_digest")
        if (
            not isinstance(endpoints, list)
            or len(endpoints) != 2
            or not all(isinstance(endpoint, str) and endpoint for endpoint in endpoints)
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        return cls(
            ("edge.add",),
            AuthorityScope.project_edge(
                project_ref=project_ref,
                source_ref=endpoints[0],
                target_ref=endpoints[1],
                membership_digest=digest,
            ),
            expires_at,
        )


class VocabularyControl:
    """Coordinates callback-held consent with durable authority transitions."""

    def __init__(
        self,
        authority: VocabularyAuthority,
        *,
        owner_decision_callback: Callable[[OwnerControlIntent], object] | None = None,
        deployment_floor_callback: Callable[[OwnerControlIntent], object] | None = None,
        activation_guard_factory: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        self.authority = authority
        self._owner_decision_callback = owner_decision_callback
        self._deployment_floor_callback = deployment_floor_callback
        self._activation_guard_factory = activation_guard_factory

    def _decision(self, intent: OwnerControlIntent) -> TrustedOwnerDecision:
        if self._owner_decision_callback is None:
            raise VocabularyAuthorityUnavailable
        value = self._owner_decision_callback(intent)
        if not isinstance(value, TrustedOwnerDecision) or value.binding_digest != intent.binding_digest:
            raise VocabularyAuthorityUnavailable
        return value

    def _floor(self, intent: OwnerControlIntent) -> DeploymentFloorProof:
        if self._deployment_floor_callback is None:
            raise VocabularyAuthorityUnavailable
        value = self._deployment_floor_callback(intent)
        if not isinstance(value, DeploymentFloorProof):
            raise VocabularyAuthorityUnavailable
        return value

    @staticmethod
    def _empty(body: Mapping[str, Any]) -> None:
        if body:
            raise ValueError("VOCABULARY_CONTROL_INVALID")

    def runtime_status(self) -> AuthorityStatus:
        return self.authority.runtime_status()

    def status(self, *, principal: RequestPrincipal) -> AuthorityStatus:
        return self.authority.status(principal)

    def activate(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> AuthorityStatus:
        self._empty(body)
        # Marker creation and v2 activation share the same canonical writer
        # boundary as content mutation, so a v1 leaf cannot cross between them.
        with self._activation_guard_factory(self.authority.vault_root):
            floor = self._floor(_intent("activate", principal))
            intent = _intent(
                "activate",
                principal,
                canonical_operation={"runtime": floor.runtime, "generation": floor.generation},
            )
            decision = self._decision(intent)
            return self.authority.activate(
                principal=principal,
                decision=decision,
                deployment_floor=floor,
                binding=intent.binding_digest,
            )

    def approve_request(
        self, *, principal: RequestPrincipal, body: Mapping[str, Any]
    ) -> str:
        if set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        request = self.authority.inspect_request_for_owner(body["request_id"], principal=principal)
        intent = _intent(
            "approve-request",
            principal,
            request_id=body["request_id"],
            canonical_operation=request.as_dict(),
        )
        return self.authority.approve_request(
            body["request_id"],
            principal=principal,
            decision=self._decision(intent),
            binding=intent.binding_digest,
        )

    def deny_request(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> None:
        if set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        request = self.authority.inspect_request_for_owner(body["request_id"], principal=principal)
        intent = _intent(
            "deny-request",
            principal,
            request_id=body["request_id"],
            canonical_operation=request.as_dict(),
        )
        self.authority.deny_request(
            body["request_id"],
            principal=principal,
            decision=self._decision(intent),
            binding=intent.binding_digest,
        )

    def grant(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> str:
        if set(body) == {"request_id", "project_ref", "expires_at"}:
            request_id = body["request_id"]
            if not isinstance(request_id, str):
                raise ValueError("VOCABULARY_CONTROL_INVALID")
            manifest = GrantManifest.project_edge_from_request(
                body,
                self.authority.inspect_request_for_owner(request_id, principal=principal),
            )
        else:
            manifest = GrantManifest.from_body(body)
        intent = _intent("grant", principal, grant_manifest=manifest.as_dict())
        decision = self._decision(intent)
        return self.authority.grant(
            principal=principal,
            decision=decision,
            audience=principal,
            actions=manifest.actions,
            scope=manifest.scope,
            expires_at=manifest.expires_at,
            binding=intent.binding_digest,
        )

    def revoke(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> None:
        if set(body) != {"authority_id"} or not isinstance(body["authority_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        authority_id = body["authority_id"]
        intent = _intent("revoke", principal, authority_id=authority_id)
        self.authority.revoke(
            principal=principal,
            decision=self._decision(intent),
            authority_id=authority_id,
            binding=intent.binding_digest,
        )
