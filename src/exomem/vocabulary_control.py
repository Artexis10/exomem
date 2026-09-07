"""Private owner-control adapter for the finite vocabulary authority ceremony.

The service gateway authenticates transport before this adapter is reached.
That authentication identifies a request; it never substitutes for the
separate trusted owner decision supplied by the control-plane callback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import vocabulary_authority
from .governance.principal import RequestPrincipal
from .vocabulary_authority import (
    AuthorityScope,
    AuthorityStatus,
    DeploymentFloorProof,
    TrustedOwnerDecision,
    VocabularyAuthority,
    VocabularyAuthorityUnavailable,
)
from .vocabulary_effects import CanonicalWriteImage

_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _detach(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_detach(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OwnerControlIntent:
    """The finite, reviewed fact passed to a separate human-control callback."""

    action: str
    principal: RequestPrincipal
    request_id: str | None = None
    display_effects: tuple[Mapping[str, str], ...] = ()
    canonical_operation: Mapping[str, Any] | None = None
    grant_manifest: Mapping[str, Any] | None = None
    authority_id: str | None = None
    binding_digest: str = ""
    write_images: tuple[CanonicalWriteImage, ...] = ()

    def __post_init__(self) -> None:
        for name in ("display_effects", "canonical_operation", "grant_manifest"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        object.__setattr__(self, "write_images", tuple(self.write_images))

    def as_dict(self) -> dict[str, Any]:
        """Return detached review data without an agent bearer or owner proof."""

        return {
            "action": self.action,
            "audience_id": self.principal.audience_id,
            "issuer_family": self.principal.issuer_family,
            "request_id": self.request_id,
            "display_effects": _detach(self.display_effects),
            "canonical_operation": _detach(self.canonical_operation),
            "grant_manifest": _detach(self.grant_manifest),
            "authority_id": self.authority_id,
            "binding_digest": self.binding_digest,
            "write_images": [
                {
                    "path": image.path,
                    "before": None if image.before is None else image.before.decode("utf-8"),
                    "after": None if image.after is None else image.after.decode("utf-8"),
                    "role": image.role,
                }
                for image in self.write_images
            ],
        }


def _intent(
    action: str,
    principal: RequestPrincipal,
    *,
    request_id: str | None = None,
    canonical_operation: dict[str, Any] | None = None,
    grant_manifest: dict[str, Any] | None = None,
    authority_id: str | None = None,
    write_images: tuple[CanonicalWriteImage, ...] = (),
) -> OwnerControlIntent:
    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        action, principal, request_id=request_id, operation=canonical_operation,
        grant=grant_manifest, authority_id=authority_id,
    )
    effects = () if canonical_operation is None else tuple(
        {"action": item["action"], "path": item["path"], "key": item.get("key") or ""}
        for item in canonical_operation.get("effects", ())
    )
    return OwnerControlIntent(
        action,
        principal,
        request_id=request_id,
        canonical_operation=canonical_operation,
        grant_manifest=grant_manifest,
        authority_id=authority_id,
        binding_digest=binding,
        display_effects=effects,
        write_images=write_images,
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
        if any(not isinstance(action, str) or action not in _ACTIONS for action in raw_actions):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        actions = tuple(sorted(set(raw_actions)))
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
        intent = self.prepare_approval(principal=principal, body=body)
        return self.authority.approve_request(
            intent.request_id,
            principal=principal,
            decision=self._decision(intent),
            binding=intent.binding_digest,
        )

    def prepare_approval(
        self, *, principal: RequestPrincipal, body: Mapping[str, Any]
    ) -> OwnerControlIntent:
        if set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        preview = self.authority.inspect_request_preview_for_owner(body["request_id"], principal=principal)
        return _intent(
            "approve-request",
            principal,
            request_id=body["request_id"],
            canonical_operation=preview.operation.as_dict(),
            write_images=preview.images,
        )

    def deny_request(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> None:
        intent = self.prepare_denial(principal=principal, body=body)
        self.authority.deny_request(
            intent.request_id,
            principal=principal,
            decision=self._decision(intent),
            binding=intent.binding_digest,
        )

    def prepare_denial(
        self, *, principal: RequestPrincipal, body: Mapping[str, Any]
    ) -> OwnerControlIntent:
        if set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        request = self.authority.inspect_request_for_owner(body["request_id"], principal=principal)
        return _intent(
            "deny-request",
            principal,
            request_id=body["request_id"],
            canonical_operation=request.as_dict(),
        )
    def _grant_manifest(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> GrantManifest:
        if set(body) == {"request_id", "project_ref", "expires_at"}:
            request_id = body["request_id"]
            if not isinstance(request_id, str):
                raise ValueError("VOCABULARY_CONTROL_INVALID")
            return GrantManifest.project_edge_from_request(
                body,
                self.authority.inspect_request_for_owner(request_id, principal=principal),
            )
        return GrantManifest.from_body(body)

    def prepare_grant(
        self, *, principal: RequestPrincipal, body: Mapping[str, Any]
    ) -> OwnerControlIntent:
        manifest = self._grant_manifest(principal=principal, body=body)
        return _intent("grant", principal, grant_manifest=manifest.as_dict())

    def grant(self, *, principal: RequestPrincipal, body: Mapping[str, Any]) -> str:
        manifest = self._grant_manifest(principal=principal, body=body)
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
        intent = self.prepare_revocation(principal=principal, body=body)
        self.authority.revoke(
            principal=principal,
            decision=self._decision(intent),
            authority_id=intent.authority_id,
            binding=intent.binding_digest,
        )

    def prepare_revocation(
        self, *, principal: RequestPrincipal, body: Mapping[str, Any]
    ) -> OwnerControlIntent:
        if set(body) != {"authority_id"} or not isinstance(body["authority_id"], str):
            raise ValueError("VOCABULARY_CONTROL_INVALID")
        authority_id = body["authority_id"]
        return _intent("revoke", principal, authority_id=authority_id)
