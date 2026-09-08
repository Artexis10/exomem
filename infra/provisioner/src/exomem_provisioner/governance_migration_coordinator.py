"""One durable step of the stopped-cell migration, using existing provider effects.

The lifecycle owns maintenance, stop proof, freshly authenticated PVC identity,
target start and admission. This coordinator never opens routes or clears gm1.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import replace

from .adapters import KubernetesCellAdapter, _retryable_kubernetes_error
from .authorization_membership import (
    HostedAuthorizationBundle,
    enroll_hosted_governance_bundle,
    inspect_hosted_authorization_bundle,
)
from .conflict_reason import ConflictReason
from .driver import (
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
)
from .governance_migration_checkpoint import (
    CHECKPOINT_VERSION,
    MigrationCheckpoint,
    migration_binding,
)
from .governance_migration_job import (
    KubernetesGovernanceMigrationAdapter,
    MigrationJobRequest,
    parse_migration_terminal,
)
from .governance_migration_membership import (
    complete_governance_migration_membership,
    repair_governance_schema_claim,
)
from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .repository import ClaimConflict, StaleFence
from .wire_protocol import WIRE_PROTOCOL_V2


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "governance migration state is unavailable",
        reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
    )


class HostedGovernanceMigrationCoordinator:
    def __init__(
        self,
        *,
        cell: KubernetesCellAdapter,
        jobs: KubernetesGovernanceMigrationAdapter,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._cell, self._jobs, self._now = cell, jobs, now

    async def advance(
        self,
        *,
        context: EffectContext,
        metadata: OpaqueProviderMetadata,
        owner: OpaqueProviderMetadata,
        pvc_uid: str,
        runtime_image: str,
        job_recovery_envelope: str,
        custody_recovery_envelope: str,
    ) -> DriverPending:
        """Advance only after bound evidence; uncertain effects retain the exact phase."""
        await context.assert_effect_authority()
        if (
            context.wire_protocol != WIRE_PROTOCOL_V2
            or (
                context.tenant_id,
                context.cell_id,
                context.provider_operation_id,
                context.fence_generation,
            )
            != (
                metadata.tenant_id,
                metadata.subject_id,
                metadata.operation_id,
                metadata.fence_generation,
            )
            or (owner.tenant_id, owner.subject_id) != (metadata.tenant_id, metadata.subject_id)
            or owner.fence_generation > metadata.fence_generation
            or any(
                not isinstance(value, str) or not value
                for value in (job_recovery_envelope, custody_recovery_envelope)
            )
        ):
            raise DriverTerminal("PROVISIONER_CHECKPOINT_INVALID")
        binding = migration_binding(context, pvc_uid=pvc_uid, runtime_image=runtime_image)
        if not context.checkpoint.startswith(CHECKPOINT_VERSION + ":"):
            match = re.fullmatch(r"vault-fingerprinted-([0-9a-f]{64})", context.checkpoint)
            if match is None:
                raise DriverTerminal("PROVISIONER_CHECKPOINT_INVALID")
            # The worker commits this denial barrier before any Job is submitted.
            checkpoint = MigrationCheckpoint("inspect", match.group(1), binding)
            return DriverPending(checkpoint.encode(), 1)
        checkpoint = MigrationCheckpoint.decode(context.checkpoint)
        if checkpoint.binding != binding or checkpoint.phase not in {
            "inspect",
            "prepare",
            "enroll",
            "commit",
        }:
            raise DriverTerminal("PROVISIONER_CHECKPOINT_INVALID")
        try:
            return await self._advance(
                context=context,
                checkpoint=checkpoint,
                metadata=metadata,
                owner=owner,
                pvc_uid=pvc_uid,
                runtime_image=runtime_image,
                job_recovery_envelope=job_recovery_envelope,
                custody_recovery_envelope=custody_recovery_envelope,
            )
        except (DriverRetryable, LostAcknowledgement):
            return DriverPending(context.checkpoint, 30)
        except (ClaimConflict, StaleFence, DriverTerminal, MetadataConflict):
            raise
        except Exception as error:  # noqa: BLE001 - keep provider payloads out of progress
            if _retryable_kubernetes_error(error):
                return DriverPending(context.checkpoint, 30)
            raise _refuse() from None

    async def _advance(
        self,
        *,
        context: EffectContext,
        checkpoint: MigrationCheckpoint,
        metadata: OpaqueProviderMetadata,
        owner: OpaqueProviderMetadata,
        pvc_uid: str,
        runtime_image: str,
        job_recovery_envelope: str,
        custody_recovery_envelope: str,
    ) -> DriverPending:
        files = await self._cell.read_authorization_session_bundle(owner)
        if files is None:
            raise _refuse()
        current = int(self._now())
        identity = {
            "expected_cell_id": metadata.subject_id,
            "expected_logical_vault_id": metadata.tenant_id,
            "expected_replica_id": metadata.resource_name + "-0",
            "expected_software_version": None,
            "expected_schema_version": None,
            "expected_recovery_envelope": custody_recovery_envelope,
        }
        source = inspect_hosted_authorization_bundle(
            files,
            **identity,
            now=current,
            _require_fresh=checkpoint.phase in {"inspect", "prepare"},
        )
        if (
            source.replica_state != "DRAINING"
            or not source.issuance_stopped
            or not source.no_in_flight
            or json.loads(source.control)["issued_at"] > current
            or (checkpoint.phase in {"inspect", "prepare"} and source.governance_enrolled)
            or (checkpoint.phase == "commit" and not source.governance_enrolled)
        ):
            raise _refuse()
        phase = checkpoint.phase
        if phase == "enroll":
            # Never prepare against enrolled custody, even when the enrollment
            # patch's acknowledgement was lost before c became durable.
            phase = "commit" if source.governance_enrolled else "prepare"
        if phase == "prepare" and (
            source.membership_schema_version != 3 or source.expires_at <= current
        ):
            raise _refuse()
        request = MigrationJobRequest(
            metadata=metadata,
            vault_id=metadata.tenant_id,
            pvc_uid=pvc_uid,
            runtime_image=runtime_image,
            custody_revision=source.revision,
            phase=phase,
            source_store_digest=checkpoint.source_store_digest,
            plan_digest=checkpoint.plan_digest if phase == "commit" else None,
        )
        evidence = await self._jobs.run(
            request,
            recovery_envelope=job_recovery_envelope,
            effect_guard=context.assert_effect_authority,
        )
        terminal = parse_migration_terminal(request, evidence._terminal)
        await context.assert_effect_authority()
        successor: HostedAuthorizationBundle | None = None
        if checkpoint.phase == "inspect":
            successor = repair_governance_schema_claim(
                source.files,
                request=request,
                evidence=evidence,
                recovery_envelope=custody_recovery_envelope,
                now=int(self._now()),
            )
            next_checkpoint = replace(
                checkpoint, phase="prepare", source_store_digest=terminal["sourceStoreDigest"]
            )
        elif checkpoint.phase == "prepare":
            next_checkpoint = replace(
                checkpoint, phase="enroll", plan_digest=terminal["planDigest"]
            )
        elif checkpoint.phase == "enroll":
            if terminal["planDigest"] != checkpoint.plan_digest:
                raise _refuse()
            if not source.governance_enrolled:
                successor = enroll_hosted_governance_bundle(
                    source.files,
                    **{**identity, "expected_schema_version": 3},
                    activation_store_id=terminal["activationStoreId"],
                    activation_epoch=terminal["activationEpoch"],
                    activation_state_digest=terminal["activationStateDigest"],
                    now=int(self._now()),
                )
            elif (
                source.activation_store_id,
                source.activation_epoch,
                source.activation_state_digest,
            ) != (
                terminal["activationStoreId"],
                terminal["activationEpoch"],
                terminal["activationStateDigest"],
            ):
                raise _refuse()
            # Enrollment alone is not commit evidence. Keep e until a verified
            # commit establishes c, then replay c before membership completion.
            next_checkpoint = (
                replace(checkpoint, phase="commit") if source.governance_enrolled else checkpoint
            )
        else:
            successor = complete_governance_migration_membership(
                source.files,
                request=request,
                evidence=evidence,
                recovery_envelope=custody_recovery_envelope,
                now=int(self._now()),
            )
            next_checkpoint = replace(checkpoint, phase="complete")
        if successor is not None and successor.revision != source.revision:
            await self._cell.write_authorization_session_bundle(
                owner,
                successor.files,
                recovery_envelope=custody_recovery_envelope,
                membership_epoch=successor.epoch,
                membership_digest=successor.membership_digest,
                revision=successor.revision,
                expected_revision=source.revision,
                effect_guard=context.assert_effect_authority,
            )
        await context.assert_effect_authority()
        return DriverPending(next_checkpoint.encode(), 1)
