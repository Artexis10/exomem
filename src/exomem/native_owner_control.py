"""Finite native owner reviews over existing vocabulary authority commands.

The browser adapter authenticates the owner before entering this controller.
Agent audiences come from current stored authorization sessions, and are
revalidated at both preparation and execution. No agent bearer is retained.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import state_migration, vocabulary_authority, vocabulary_control, writer_lease
from .governance import authorization_custody, authorization_session_lifecycle, policy, store
from .governance.principal import RequestPrincipal
from .native_owner_reviews import NativeOwnerReviewDenied, _thaw
from .native_owner_setup import NativeOwnerSetup, NativeOwnerSetupUnavailable, _digest

_PREPARE = {
    "approve": "prepare_approval",
    "deny": "prepare_denial",
    "grant": "prepare_grant",
    "revoke": "prepare_revocation",
}
_EXECUTE = {
    "approve": "approve_request",
    "deny": "deny_request",
    "grant": "grant",
    "revoke": "revoke",
}
_SETUP = frozenset({"policy", "migration", "activation"})


class NativeOwnerControl:
    def __init__(self, vault_root: Path, *, reviews=None) -> None:
        self.vault_root = Path(vault_root)
        if reviews is None:
            from .native_owner_reviews import OwnerReviewStore

            reviews = OwnerReviewStore(self.vault_root)
        self.reviews = reviews

    def _control(self, decision=None) -> vocabulary_control.VocabularyControl:
        return vocabulary_control.VocabularyControl(
            vocabulary_authority.VocabularyAuthority(self.vault_root),
            owner_decision_callback=decision,
            activation_guard_factory=lambda root: writer_lease.get_manager().consistency_guard(
                root, operation="native-owner-activation"
            ),
        )

    @staticmethod
    def _session_context(row) -> authorization_session_lifecycle.AuthorizationSessionContext:
        return authorization_session_lifecycle.AuthorizationSessionContext(
            session_id=str(row[0]),
            principal_id=str(row[1]),
            issuer_family=str(row[2]),
            cell_id=str(row[3]),
            logical_vault_id=str(row[4]),
            keyring_id=str(row[5]),
            credential_generation=int(row[6]),
            expires_at=int(row[7]),
        )

    def _principal(self, session_id: str, now: int) -> RequestPrincipal:
        custody = authorization_custody.load_authorization_custody(self.vault_root, now=now)
        connection = store.open_authorization_session_connection(self.vault_root)
        try:
            row = connection.execute(
                "SELECT session_id, principal_id, issuer_family, cell_id, logical_vault_id, "
                "keyring_id, credential_generation, expires_at "
                "FROM governance_authorization_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise vocabulary_authority.VocabularyAuthorityUnavailable
            context = authorization_session_lifecycle.status_verified_session(
                connection,
                custody=custody,
                context=self._session_context(row),
                now=now,
            )
        finally:
            connection.close()
        return RequestPrincipal(
            audience_id=context.principal_id,
            surface="native-owner-control",
            authorization_session_id=context.session_id,
            issuer_family=context.issuer_family,
            verified_authorization_session=context,
        )

    def sessions(self, *, now: int, after: str = "") -> dict[str, Any]:
        """Bounded owner-only enumeration of non-secret current audiences."""

        connection = store.open_authorization_session_connection(self.vault_root)
        try:
            rows = connection.execute(
                "SELECT session_id FROM governance_authorization_sessions "
                "WHERE status='active' AND issuer_family!='native-owner-setup' AND expires_at>? AND session_id>? "
                "ORDER BY session_id LIMIT 21",
                (now, after),
            ).fetchall()
        finally:
            connection.close()
        items = []
        for row in rows[:20]:
            principal = self._principal(str(row[0]), now)
            items.append(
                {
                    "session_id": principal.authorization_session_id,
                    "audience_id": principal.audience_id,
                    "issuer_family": principal.issuer_family,
                    "expires_at": principal.verified_authorization_session.expires_at,
                }
            )
        return {"items": items, "next": str(rows[19][0]) if len(rows) > 20 else None}

    def requests(self, session_id: str, *, now: int, after: str = "") -> dict[str, Any]:
        principal = self._principal(session_id, now)
        authority = vocabulary_authority.VocabularyAuthority(self.vault_root)
        custody = authority._validate_custody(principal, now=now)
        connection = authority._connect(custody, create=False)
        if connection is None:
            return {"items": [], "next": None}
        try:
            rows = connection.execute(
                "SELECT request_id FROM authority_requests WHERE audience_id=? AND issuer_family=? "
                "AND state='pending' AND expires_at>? AND request_id>? ORDER BY request_id LIMIT 21",
                (principal.audience_id, principal.issuer_family, now, after),
            ).fetchall()
        finally:
            connection.close()
        return {
            "items": [row[0] for row in rows[:20]],
            "next": str(rows[19][0]) if len(rows) > 20 else None,
        }

    def grants(self, session_id: str, *, now: int, after: str = "") -> dict[str, Any]:
        principal = self._principal(session_id, now)
        authority = vocabulary_authority.VocabularyAuthority(self.vault_root)
        custody = authority._validate_custody(principal, now=now)
        connection = authority._connect(custody, create=False)
        if connection is None:
            return {"items": [], "next": None}
        try:
            rows = connection.execute(
                "SELECT authority_id, actions_json, scope_json, expires_at FROM authorities "
                "WHERE kind='grant' AND status='active' AND audience_id=? AND issuer_family=? "
                "AND expires_at>? AND authority_id>? ORDER BY authority_id LIMIT 21",
                (principal.audience_id, principal.issuer_family, now, after),
            ).fetchall()
        finally:
            connection.close()
        return {
            "items": [
                {
                    "authority_id": row[0],
                    "actions": json.loads(row[1]),
                    "scope": json.loads(row[2]),
                    "expires_at": row[3],
                }
                for row in rows[:20]
            ],
            "next": str(rows[19][0]) if len(rows) > 20 else None,
        }

    def _prepare_intent(self, action: str, body: Mapping[str, Any], now: int):
        body = vocabulary_control._detach(body)
        if action not in _PREPARE or not isinstance(body.get("session_id"), str):
            raise ValueError("OWNER_CONTROL_INVALID")
        principal = self._principal(body["session_id"], now)
        arguments = {key: value for key, value in body.items() if key != "session_id"}
        control = self._control()
        return getattr(control, _PREPARE[action])(principal=principal, body=arguments)

    def prepare(self, *, owner_id: str, action: str, body: Mapping[str, Any], now: int):
        if action in _SETUP:
            if body:
                raise ValueError("OWNER_CONTROL_INVALID")
            return self._prepare_setup(owner_id=owner_id, action=action, now=now)
        intent = self._prepare_intent(action, body, now)
        return self.reviews.prepare(
            owner_id=owner_id,
            action=action,
            body=dict(body),
            display=intent.as_dict(),
            binding_digest=intent.binding_digest,
            expires_at=now + 300,
            now=now,
        )

    def accept(self, review_id: str, *, owner_id: str, now: int):
        review = self.reviews.get(review_id, owner_id=owner_id, now=now)
        if review.state == "completed":
            return review
        if review.action in _SETUP:
            return self._accept_setup(review, now=now)
        intent = self._prepare_intent(review.action, review.body, now)
        review = self.reviews.accept(
            review_id,
            owner_id=owner_id,
            expected_binding=intent.binding_digest,
            now=now,
        )
        review = self.reviews.begin(review_id, owner_id=owner_id, now=now)

        def decision(current: vocabulary_control.OwnerControlIntent):
            if current.binding_digest != review.binding_digest:
                raise vocabulary_authority.VocabularyAuthorityUnavailable
            return vocabulary_authority._trusted_owner_decision_for_adapter(
                owner_id=owner_id,
                ceremony_id=review_id,
                binding_digest=review.binding_digest,
                expires_at=review.expires_at,
            )

        # Execution prepares again. A failed or uncertain execution leaves the
        # accepted review in applying state for evidence-based reconciliation.
        control = self._control(decision)
        result = getattr(control, _EXECUTE[review.action])(
            principal=intent.principal,
            body={
                key: value
                for key, value in vocabulary_control._detach(review.body).items()
                if key != "session_id"
            },
        )
        terminal = {"status": "ok"} if result is None else {"authority_id": result}
        return self.reviews.complete(review_id, owner_id=owner_id, result=terminal, now=now)

    def setup_status(self, *, now: int) -> str:
        current = policy.load(self.vault_root)
        if current.blocked:
            raise NativeOwnerSetupUnavailable
        if current.empty:
            return "policy"
        version = store.authorization_session_schema_version(self.vault_root)
        if version == 3:
            return "migration"
        if version != 4:
            raise NativeOwnerSetupUnavailable
        status = vocabulary_authority.VocabularyAuthority(self.vault_root).runtime_status(now=now)
        if status.mode == "v2":
            return "active"
        # A published floor with an interrupted activation still needs a fresh
        # owner review. Validate custody independently of the authority marker.
        authorization_custody.load_authorization_custody(self.vault_root, now=now)
        return "activation"

    def _current_floor(self, *, now: int) -> dict[str, Any]:
        current = authorization_custody.load_authorization_custody(self.vault_root, now=now)
        authorization_custody.require_current_standalone_registry(
            current, now=now, require_serving=True
        )
        if current.control.vocabulary_authority_floor != 2:
            raise NativeOwnerSetupUnavailable
        return {
            "kind": "activate_current_floor",
            "vault_binding": authorization_custody.standalone_attachment_id(self.vault_root),
            "logical_vault_id": current.control.logical_vault_id,
            "cell_id": current.control.cell_id,
            "keyring_id": current.keyring.keyring_id,
            "keyring_digest": hashlib.sha256(
                authorization_custody.load_external_custody(self.vault_root).keyring
            ).hexdigest(),
            "generation": current.control.activation_epoch,
            "activation_state_digest": current.control.activation_state_digest,
            "runtime": vocabulary_authority.RUNTIME_FLOOR,
        }

    def _prepare_setup(self, *, owner_id: str, action: str, now: int):
        from . import native_owner_preparation as preparation
        from . import vocabulary_deployment

        if self.setup_status(now=now) != action:
            raise NativeOwnerSetupUnavailable("owner setup stage changed")
        if action == "policy":
            prepared = NativeOwnerSetup(self.vault_root).prepare_policy(now=now)
            body, display, expiry = (
                _thaw(prepared.body),
                _thaw(prepared.display),
                prepared.expires_at,
            )
        else:
            binding, environment = preparation.deployment_binding(self.vault_root)
            configuration = preparation.prospective_custody(self.vault_root, environment)
            from .native_owner_maintenance import planned_environment_digest

            body = {
                **binding,
                "custody_environment": configuration,
                "service_target_environment_digest": planned_environment_digest(Path(binding["service_unit"]), configuration),
            }
            if action == "migration":
                prepared = preparation.migration_child(
                    self.vault_root, environment=configuration, now=now
                )
                body["setup"] = prepared["body"]
                display, expiry = prepared["display"], prepared["expires_at"]
            else:
                current = authorization_custody.load_authorization_custody(self.vault_root, now=now)
                if current.control.vocabulary_authority_floor == 2:
                    body["setup"] = self._current_floor(now=now)
                    display, expiry = dict(body["setup"]), now + 300
                else:
                    plan = vocabulary_deployment.prepare_standalone_floor(self.vault_root, now=now)
                    body["setup"] = plan.as_record()
                    display, expiry = plan.as_dict(), plan.expires_at
                body["renewal"] = "same-authority"
                display.update(
                    default_grants=[],
                    renewal="Renew the same installation's serving proof automatically; no new keys, permissions or identity.",
                )
            display = {
                **display,
                "service_unit": binding["service_unit"],
                "maintenance_required": True,
            }
        return self.reviews.prepare(
            owner_id=owner_id,
            action=action,
            body=body,
            display=display,
            binding_digest=_digest({"action": action, "body": body, "display": display}),
            expires_at=expiry,
            now=now,
        )

    @staticmethod
    def _review_binding(review) -> str:
        digest = _digest({"action": review.action, "body": review.body, "display": review.display})
        if digest != review.binding_digest:
            raise NativeOwnerReviewDenied
        return digest

    def _recheck_deployment(self, review) -> dict[str, str]:
        from .native_owner_preparation import deployment_binding, prospective_custody

        binding, environment = deployment_binding(self.vault_root)
        if any(review.body.get(key) != value for key, value in binding.items()):
            raise NativeOwnerSetupUnavailable("reviewed service changed")
        configuration = prospective_custody(self.vault_root, environment)
        if configuration != _thaw(review.body["custody_environment"]):
            raise NativeOwnerSetupUnavailable("reviewed custody settings changed")
        return configuration

    def _recheck_setup(self, review, *, now: int) -> None:
        from . import native_owner_preparation as preparation
        from . import vocabulary_deployment

        self._review_binding(review)
        if review.expired:
            raise NativeOwnerReviewDenied
        if review.action == "policy":
            NativeOwnerSetup(self.vault_root).recheck_policy(review.body, now=now)
            return
        configuration = self._recheck_deployment(review)
        if review.action == "migration":
            preparation.migration_child(
                self.vault_root, environment=configuration, now=now, body=review.body["setup"]
            )
        elif review.action == "activation":
            setup = _thaw(review.body["setup"])
            if review.body.get("renewal") != "same-authority":
                raise NativeOwnerReviewDenied
            if setup.get("kind") == "activate_current_floor":
                if setup != self._current_floor(now=now):
                    raise NativeOwnerSetupUnavailable
            else:
                plan = vocabulary_deployment.FloorPlan.from_record(setup)
                if not plan.prepared_at <= now < plan.expires_at:
                    raise NativeOwnerReviewDenied
                external = authorization_custody.load_external_custody(self.vault_root)
                _path, membership, _replica = authorization_custody._standalone_membership_file(
                    self.vault_root, external=external
                )
                if external.control != plan.source_control or membership != plan.source_membership:
                    raise NativeOwnerSetupUnavailable("reviewed deployment changed")
        else:
            raise NativeOwnerReviewDenied

    def _accept_setup(self, review, *, now: int):
        if review.state == "accepted" and review.action in {"migration", "activation"}:
            self._recheck_setup(review, now=now)
            return review
        self._recheck_setup(review, now=now)
        accepted = self.reviews.accept(
            review.review_id,
            owner_id=review.owner_id,
            expected_binding=review.binding_digest,
            now=now,
        )
        if review.action != "policy":
            return accepted
        self.reviews.begin(review.review_id, owner_id=review.owner_id, now=now)
        result = NativeOwnerSetup(self.vault_root).execute_policy(review.body, now=now)
        return self.reviews.complete(
            review.review_id, owner_id=review.owner_id, result=result, now=now
        )

    def _activate_current(self, review, floor, *, now: int):
        current = authorization_custody.load_authorization_custody(self.vault_root, now=now)
        connection = store.open_authorization_session_connection(self.vault_root)
        try:
            issued = authorization_session_lifecycle.open_session(
                connection,
                custody=current,
                principal_id=review.owner_id,
                issuer_family="native-owner-setup",
                now=now,
                ttl_seconds=300,
            )
        finally:
            connection.close()
        context = issued.context
        principal = RequestPrincipal(
            audience_id=context.principal_id,
            surface="native-owner-control",
            authorization_session_id=context.session_id,
            issuer_family=context.issuer_family,
            verified_authorization_session=context,
        )
        intent = vocabulary_control._intent(
            "activate",
            principal,
            canonical_operation={"runtime": floor.runtime, "generation": floor.generation},
        )
        decision = vocabulary_authority._trusted_owner_decision_for_adapter(
            owner_id=review.owner_id,
            ceremony_id=review.review_id,
            binding_digest=intent.binding_digest,
            expires_at=review.expires_at,
        )
        with writer_lease.get_manager().consistency_guard(
            self.vault_root, operation="native-owner-activation"
        ):
            if max(now, int(time.time())) >= review.expires_at:
                raise NativeOwnerReviewDenied
            status = vocabulary_authority.VocabularyAuthority(self.vault_root).activate(
                principal=principal,
                decision=decision,
                deployment_floor=floor,
                binding=intent.binding_digest,
            )
        return {"status": "completed", "mode": status.mode, "default_grants": []}

    def apply_maintenance(self, review_id: str, *, owner_id: str, offline_authority, now: int):
        """Apply an accepted fixed deployment only inside the proven stop window."""
        from . import vocabulary_deployment

        def current_time() -> int:
            return max(now, int(time.time()))

        state_migration._require_offline_authority(offline_authority)
        review = self.reviews.get(review_id, owner_id=owner_id, now=now)
        if review.state == "completed":
            return _thaw(review.result)
        self._review_binding(review)
        if review.action not in {"migration", "activation"}:
            raise NativeOwnerReviewDenied
        configuration = self._recheck_deployment(review)
        if any(os.environ.get(key) != value for key, value in configuration.items()):
            raise NativeOwnerSetupUnavailable("maintenance custody configuration changed")
        recovering = review.state == "applying"
        if not recovering:
            if review.state != "accepted" or review.expired:
                raise NativeOwnerReviewDenied
            # Fresh migration checks run directly in the stopped process with
            # prospective settings. No serving environment is ever changed.
            if review.action == "migration":
                NativeOwnerSetup(self.vault_root).recheck_migration(review.body["setup"], now=now)
            now = current_time()
            review = self.reviews.begin(review_id, owner_id=owner_id, now=now)
        if review.action == "migration":
            setup = NativeOwnerSetup(self.vault_root)
            result = (
                setup.recover_migration(
                    review.body["setup"],
                    started_at=review.started_at,
                    offline_authority=offline_authority,
                    now=now,
                )
                if recovering
                else setup.execute_migration(
                    review.body["setup"], offline_authority=offline_authority, now=now
                )
            )
        else:
            setup = _thaw(review.body["setup"])
            if review.body.get("renewal") != "same-authority":
                raise NativeOwnerReviewDenied
            if setup.get("kind") == "activate_current_floor":
                if setup != self._current_floor(now=now):
                    raise NativeOwnerSetupUnavailable
                floor = vocabulary_authority._deployment_floor_for_adapter(
                    runtime=setup["runtime"], generation=setup["generation"]
                )
            else:
                plan = vocabulary_deployment.FloorPlan.from_record(setup)
                decision = vocabulary_authority._trusted_owner_decision_for_adapter(
                    owner_id=owner_id,
                    ceremony_id=review_id,
                    binding_digest=plan.review_digest,
                    expires_at=review.expires_at,
                )
                if recovering:
                    floor = vocabulary_deployment.recover_standalone_floor(
                        self.vault_root,
                        plan,
                        decision=decision,
                        started_at=review.started_at,
                        offline_authority=offline_authority,
                        now=now,
                        allow_same_authority_renewal=True,
                    )
                else:
                    floor = vocabulary_deployment.publish_standalone_floor(
                        self.vault_root,
                        plan,
                        decision=decision,
                        offline_authority=offline_authority,
                        now=now,
                    )
            # Publishing the reviewed deployment can take time. A later
            # authority activation needs consent that is still current.
            now = current_time()
            status = vocabulary_authority.VocabularyAuthority(self.vault_root).runtime_status(
                now=now
            )
            if status.mode == "v2":
                result = {"status": "completed", "mode": "v2", "default_grants": []}
            elif now >= review.expires_at:
                result = {
                    "status": "deployment_ready",
                    "activation": "owner_review_required",
                    "default_grants": [],
                }
            else:
                result = self._activate_current(review, floor, now=now)
        completed = self.reviews.complete(review_id, owner_id=owner_id, result=result, now=now)
        return _thaw(completed.result)
