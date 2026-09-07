"""Initial policy and offline migration behind authenticated owner acceptance.

Commit initial policy in the unconfigured custody runtime. Migration preparation
requires the prospective external custody paths; callers must run that phase in
a separate process rather than change a serving process's environment. Only an
offline runner with the accepted binding may execute its migration.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import commands, state_migration, writer_lease
from .governance import authorization_custody, policy, schema_migration, store, tool
from .governance.principal import owner_principal, request_scope
from .native_owner_reviews import _freeze, _thaw

_TTL = 300
_SCOPE_DOCUMENTS = {
    "scopes/existing-access.yaml": (
        "governance_version: 1\n"
        "id: 01K00000000000000000000000\n"
        "name: Existing access\n"
        'paths: ["**"]\n'
        "default_deny: false\n"
    )
}
_POLICY_KEYS = {"kind", "vault_binding", "expires_at", "proposal_id", "proposal_digest"}
_MIGRATION_KEYS = {"kind", "vault_binding", "expires_at", "expected_plan_digest", "configuration"}


class NativeOwnerSetupUnavailable(RuntimeError):
    """The reviewed setup no longer matches its canonical inputs."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedSetup:
    body: Mapping[str, Any]
    display: Mapping[str, Any]
    binding_digest: str
    expires_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", _freeze(_thaw(self.body)))
        object.__setattr__(self, "display", _freeze(_thaw(self.display)))


class NativeOwnerSetup:
    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root).absolute()

    def _prepared(self, body: dict[str, Any], display: dict[str, Any]) -> PreparedSetup:
        return PreparedSetup(
            body, display, _digest({"body": body, "display": display}), body["expires_at"]
        )

    def _body(self, kind: str, *, now: int) -> dict[str, Any]:
        if type(now) is not int or now <= 0:
            raise NativeOwnerSetupUnavailable
        return {
            "kind": kind,
            "vault_binding": authorization_custody.standalone_attachment_id(self.vault_root),
            "expires_at": now + _TTL,
        }

    def _check_body(
        self, body: Mapping[str, Any], kind: str, *, now: int, started_at: int | None = None
    ) -> None:
        if (
            not isinstance(body, Mapping)
            or set(body) != (_POLICY_KEYS if kind == "policy" else _MIGRATION_KEYS)
            or body["kind"] != kind
            or type(body["expires_at"]) is not int
            or type(now) is not int
            or body["vault_binding"]
            != authorization_custody.standalone_attachment_id(self.vault_root)
        ):
            raise NativeOwnerSetupUnavailable
        if started_at is None:
            valid_time = now < body["expires_at"] <= now + _TTL
        else:
            valid_time = (
                type(started_at) is int
                and 0 < started_at <= now
                and body["expires_at"] - _TTL <= started_at < body["expires_at"]
            )
        if not valid_time:
            raise NativeOwnerSetupUnavailable

    def _require_empty_policy(self) -> None:
        current = policy.load(self.vault_root)
        if current.blocked or not current.empty:
            raise NativeOwnerSetupUnavailable

    def _invoke(self, **kwargs: Any) -> dict[str, Any]:
        command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "govern_memory")
        with request_scope(owner_principal(surface="cli", purpose="native-owner-setup")):
            result = writer_lease.invoke_command(
                command, self.vault_root, response_detail="full", **kwargs
            )
        if not isinstance(result, dict) or result.get("status") != "committed":
            raise NativeOwnerSetupUnavailable
        return result

    def _proposal(self, proposal_id: object) -> tuple[Any, ...]:
        if not isinstance(proposal_id, str) or len(proposal_id) != 32:
            raise NativeOwnerSetupUnavailable
        connection = store.open_readonly_connection(self.vault_root)
        if connection is None:
            raise NativeOwnerSetupUnavailable
        try:
            row = connection.execute(
                "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
                "expires_at FROM governance_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise NativeOwnerSetupUnavailable
        return tuple(row)

    def prepare_policy(self, *, now: int) -> PreparedSetup:
        with writer_lease.get_manager().consistency_guard(
            self.vault_root, operation="native-owner-policy-review"
        ):
            self._require_empty_policy()
            body = self._body("policy", now=now)
            result = self._invoke(
                operation="propose",
                documents=dict(_SCOPE_DOCUMENTS),
                intent="Preserve existing access while enabling governance setup",
                ttl_seconds=_TTL,
                duration="standing",
            )
            display = result["diagnostics"]
            proposal_id = display["proposal_id"]
            row = self._proposal(proposal_id)
            body.update(proposal_id=proposal_id, proposal_digest=_digest(list(row)))
            body["expires_at"] = min(body["expires_at"], int(row[4]))
            self.recheck_policy(body, now=now)
            return self._prepared(body, display)

    def recheck_policy(self, body: Mapping[str, Any], *, now: int) -> None:
        self._check_body(body, "policy", now=now)
        self._require_empty_policy()
        row = self._proposal(body["proposal_id"])
        if row[3] != "pending" or _digest(list(row)) != body["proposal_digest"]:
            raise NativeOwnerSetupUnavailable
        try:
            payload = tool._validate_proposal_values(
                self.vault_root,
                proposal_json=row[0],
                fingerprint=row[1],
                manifest=row[2],
                status=row[3],
                expires_at=row[4],
                now=now,
            )
            if (
                set(payload) != {"interpretation", "documents", "duration"}
                or payload["documents"] != _SCOPE_DOCUMENTS
            ):
                raise NativeOwnerSetupUnavailable
        except (tool.GovernanceError, ValueError, KeyError, TypeError) as error:
            raise NativeOwnerSetupUnavailable from error

    def execute_policy(self, body: Mapping[str, Any], *, now: int) -> dict[str, Any]:
        with writer_lease.get_manager().consistency_guard(
            self.vault_root, operation="native-owner-policy-commit"
        ):
            self.recheck_policy(body, now=now)
            return self._invoke(operation="commit", proposal_id=body["proposal_id"])

    def _configuration(self) -> dict[str, str]:
        names = (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
            authorization_custody.MEMBERSHIP_FILE_ENV,
        )
        paths = {
            name: str(authorization_custody._configured_external_path(name, self.vault_root))
            for name in names
        }
        if len(set(paths.values())) != 3:
            raise NativeOwnerSetupUnavailable
        paths[authorization_custody.REPLICA_ID_ENV] = authorization_custody._bounded_identifier(
            os.environ.get(authorization_custody.REPLICA_ID_ENV, "")
        )
        return paths

    def prepare_migration(self, *, now: int) -> PreparedSetup:
        body = self._body("migration", now=now)
        body["configuration"] = self._configuration()
        plan = schema_migration.prepare_forward_migration(self.vault_root, now=now)
        body["expected_plan_digest"] = plan.plan_digest
        return self._prepared(body, schema_migration.plan_summary(plan))

    def recheck_migration(
        self, body: Mapping[str, Any], *, now: int
    ) -> schema_migration.ForwardMigrationPlan:
        self._check_body(body, "migration", now=now)
        if _thaw(body["configuration"]) != self._configuration():
            raise NativeOwnerSetupUnavailable
        plan = schema_migration.prepare_forward_migration(self.vault_root, now=now)
        if plan.plan_digest != body["expected_plan_digest"]:
            raise NativeOwnerSetupUnavailable
        return plan

    def execute_migration(
        self,
        body: Mapping[str, Any],
        *,
        offline_authority: state_migration.OfflineMigrationAuthority,
        now: int,
    ) -> dict[str, Any]:
        state_migration._require_offline_authority(offline_authority)
        plan = self.recheck_migration(body, now=now)
        schema_migration.stage_forward_migration(
            self.vault_root, expected_plan_digest=plan.plan_digest, now=now
        )
        return self._commit_migration(plan.plan_digest, now=now)

    def recover_migration(
        self,
        body: Mapping[str, Any],
        *,
        started_at: int,
        offline_authority: state_migration.OfflineMigrationAuthority,
        now: int,
    ) -> dict[str, Any]:
        """Resume an accepted in-flight cutover only from its verified backup.

        The owner ledger supplies the original applying timestamp. Inert
        preparation and namespace staging cannot satisfy the backup check, so
        an expired review cannot begin a fresh migration through this route.
        The production coordinator rechecks partial or active state at actual
        recovery time; consent expiry is never extended.
        """
        state_migration._require_offline_authority(offline_authority)
        if type(started_at) is not int:
            raise NativeOwnerSetupUnavailable
        self._check_body(body, "migration", now=now, started_at=started_at)
        if _thaw(body["configuration"]) != self._configuration():
            raise NativeOwnerSetupUnavailable
        schema_migration.verify_forward_migration_backup(
            self.vault_root, expected_plan_digest=body["expected_plan_digest"]
        )
        return self._commit_migration(body["expected_plan_digest"], now=now)

    def _commit_migration(self, expected_plan_digest: str, *, now: int) -> dict[str, Any]:
        result = schema_migration.commit_forward_migration(
            self.vault_root, expected_plan_digest=expected_plan_digest, now=now
        )
        return {
            "status": "completed",
            "schema_version": result.schema_version,
            "plan_digest": result.plan_digest,
            "source_store_digest": result.source_store_digest,
            "backup_reference": result.backup_reference,
            "replayed": result.replayed,
        }
