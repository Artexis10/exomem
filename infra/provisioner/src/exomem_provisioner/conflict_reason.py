"""Closed vocabulary naming which provider conflict produced a terminal failure.

`PROVISIONER_PROVIDER_METADATA_CONFLICT` is one stable terminal code standing for
a dozen distinct provider conditions, and `operation_recovery` matches that code
verbatim, so it cannot be split. The label rides alongside it and says which
condition fired, which is what a bare code could never tell an operator.

The vocabulary is closed on purpose. This is a multi-tenant control plane, and a
reason travels into operation state and into logs: only a member of this
enumeration is ever admitted, so a tenant, cell, operation, volume handle,
recovery envelope, or provider payload can never ride out on it. Anything else --
a caller string, a provider message, an attribute overwritten after the fact --
degrades to `UNCLASSIFIED` rather than being carried through.
"""

from __future__ import annotations

from enum import StrEnum


class ConflictReason(StrEnum):
    """One internal condition label per distinguishable provider conflict."""

    UNCLASSIFIED = "unclassified"

    # Opaque provider identity and fence encoding (lifecycle).
    PROVIDER_IDENTITY_NOT_OPAQUE = "provider-identity-not-opaque"
    PROVIDER_IDENTITY_EXCEEDS_LABEL_CAPACITY = "provider-identity-exceeds-label-capacity"
    PROVIDER_IDENTITY_IMMUTABLE = "provider-identity-immutable"
    PROVIDER_FENCE_OUT_OF_RANGE = "provider-fence-out-of-range"
    HCLOUD_IDENTITY_CHUNK_COUNT_INVALID = "hcloud-identity-chunk-count-invalid"
    HCLOUD_IDENTITY_CHUNKS_INCOMPLETE = "hcloud-identity-chunks-incomplete"
    HCLOUD_IDENTITY_CHUNK_INVALID = "hcloud-identity-chunk-invalid"
    HCLOUD_IDENTITY_UNDECODABLE = "hcloud-identity-undecodable"
    HCLOUD_IDENTITY_DIGEST_DIFFERS = "hcloud-identity-digest-differs"
    HCLOUD_FENCE_LABEL_INVALID = "hcloud-fence-label-invalid"
    KUBERNETES_IDENTITY_DIGEST_DIFFERS = "kubernetes-identity-digest-differs"
    KUBERNETES_FENCE_ANNOTATION_INVALID = "kubernetes-fence-annotation-invalid"

    # Bound-volume registration and retained storage (lifecycle).
    BOUND_VOLUME_NOT_DISCOVERABLE = "bound-volume-not-discoverable"
    HCLOUD_VOLUME_IDENTITY_DIFFERS = "hcloud-volume-identity-differs"
    RECORDED_VOLUME_LOCATION_DIFFERS = "recorded-volume-location-differs"
    PROVIDER_VOLUME_IDENTITY_DIFFERS = "provider-volume-identity-differs"
    STATIC_BINDING_HANDLE_DIFFERS = "static-binding-handle-differs"
    RETAINED_CLAIM_DELETION_UNCONVERGED = "retained-claim-deletion-unconverged"
    RETAINED_STORAGE_ABSENCE_UNPROVEN = "retained-storage-absence-unproven"

    # Kubernetes and HCloud volume observation (adapters).
    PVC_TERMINATING = "pvc-terminating"
    PV_TERMINATING = "pv-terminating"
    BOUND_PV_MISSING_VOLUME_HANDLE = "bound-pv-missing-volume-handle"
    BOUND_PV_LOCATION_AMBIGUOUS = "bound-pv-location-ambiguous"
    PVC_RECOVERY_IDENTITY_UNAUTHENTICATED = "pvc-recovery-identity-unauthenticated"
    PV_RECOVERY_IDENTITY_ABSENT = "pv-recovery-identity-absent"
    PV_RECOVERY_IDENTITY_UNAUTHENTICATED = "pv-recovery-identity-unauthenticated"
    HCLOUD_VOLUME_ABSENT = "hcloud-volume-absent"
    HCLOUD_VOLUME_HANDLE_NOT_NUMERIC = "hcloud-volume-handle-not-numeric"
    HCLOUD_TENANT_SELECTOR_MISMATCH = "hcloud-tenant-selector-mismatch"
    HCLOUD_RECOVERY_IDENTITY_UNAUTHENTICATED = "hcloud-recovery-identity-unauthenticated"

    # Request and durable-record admission.
    WORKER_POLICY_INCOMPLETE = "worker-policy-incomplete"
    PROVIDER_RECOVERY_ENVELOPE_UNAUTHENTICATED = "provider-recovery-envelope-unauthenticated"
    DURABLE_RESOURCE_IDENTITY_IMMUTABLE = "durable-resource-identity-immutable"

    # Kubernetes object authentication and identity (live registry).
    KUBERNETES_RECOVERY_IDENTITY_UNAUTHENTICATED = "kubernetes-recovery-identity-unauthenticated"
    KUBERNETES_CELL_IDENTITY_ANNOTATIONS_DIFFER = "kubernetes-cell-identity-annotations-differ"
    PROVIDER_OBJECT_TERMINATING = "provider-object-terminating"
    ROUTE_OBJECT_TERMINATING = "route-object-terminating"
    HELM_RELEASE_RECORD_NOT_EXACT = "helm-release-record-not-exact"
    HELM_RELEASE_RECORD_IDENTITY_DIFFERS = "helm-release-record-identity-differs"
    NAMESPACE_PROVISION_MODE_DIFFERS = "namespace-provision-mode-differs"
    PROVIDER_STATE_NOT_OBSERVED = "provider-state-not-observed"

    # Authorization session bootstrap and membership (live plane).
    AUTHORIZATION_ENVELOPE_SET_ABSENT = "authorization-envelope-set-absent"
    AUTHORIZATION_SECRET_ENVELOPE_ABSENT = "authorization-secret-envelope-absent"
    AUTHORIZATION_SESSION_BUNDLE_ABSENT = "authorization-session-bundle-absent"
    ORIGINAL_AUTHORIZATION_SESSION_IDENTITY_UNAVAILABLE = (
        "original-authorization-session-identity-unavailable"
    )
    RUNTIME_ATTESTATION_AUTHORITY_UNAVAILABLE = "runtime-attestation-authority-unavailable"
    RUNTIME_DRAIN_NOT_ACKNOWLEDGED = "runtime-drain-not-acknowledged"

    # Capacity admission and provision mode (live plane).
    PROVISION_MODE_INVALID = "provision-mode-invalid"
    CAPACITY_RESERVATION_OPERATION_UNAVAILABLE = "capacity-reservation-operation-unavailable"
    ACTIVE_CAPACITY_RESERVATION_ABSENT = "active-capacity-reservation-absent"

    # Provision retarget and stranded-provision recovery (live plane).
    RETARGET_OPERATION_UNAVAILABLE = "retarget-operation-unavailable"
    RETARGET_RECOVERY_RECEIPT_INVALID = "retarget-recovery-receipt-invalid"
    CURRENT_HELM_RUNTIME_SELECTION_INVALID = "current-helm-runtime-selection-invalid"
    RETARGET_BLOCKED_BY_ADMITTED_OR_ROUTED_CELL = "retarget-blocked-by-admitted-or-routed-cell"
    RETARGET_REQUIRES_DECLARED_MIGRATION = "retarget-requires-declared-migration"
    STRANDED_PROVISION_ADMITTED_OR_ROUTED = "stranded-provision-admitted-or-routed"

    # Storage initialization and stored-request replay (live plane).
    CELL_STORAGE_INITIALIZATION_ALREADY_FAILED = "cell-storage-initialization-already-failed"
    CELL_STORAGE_INITIALIZATION_FAILED = "cell-storage-initialization-failed"
    ORIGINAL_HELM_REQUEST_UNAUTHENTICATED = "original-helm-request-unauthenticated"
    ORIGINAL_RUNTIME_IDENTITY_UNAVAILABLE = "original-runtime-identity-unavailable"

    # Vault fingerprint and declared runtime migration (live plane).
    VAULT_FINGERPRINT_ADAPTER_UNAVAILABLE = "vault-fingerprint-adapter-unavailable"
    VAULT_FINGERPRINT_AUTHORITY_ABSENT = "vault-fingerprint-authority-absent"
    RUNTIME_MIGRATION_NOT_DECLARED = "runtime-migration-not-declared"

    # Hosted credential rotation (live plane).
    ACTIVE_CREDENTIAL_VERSION_ABSENT = "active-credential-version-absent"
    ACTIVE_PROVIDER_CREDENTIAL_ABSENT = "active-provider-credential-absent"
    ACTIVE_CREDENTIAL_DOES_NOT_MATCH_PROVIDER = "active-credential-does-not-match-provider"
    CREDENTIAL_REVISION_DID_NOT_ADVANCE = "credential-revision-did-not-advance"
    PENDING_CREDENTIAL_VERSION_IMMUTABLE = "pending-credential-version-immutable"
    PENDING_CREDENTIAL_ABSENT = "pending-credential-absent"
    CREDENTIAL_DID_NOT_STAGE = "credential-did-not-stage"
    CREDENTIAL_DID_NOT_PROMOTE = "credential-did-not-promote"
    CREDENTIAL_DID_NOT_FINALIZE = "credential-did-not-finalize"

    # Deletion routed to the wrong plane (live plane owns no deletion authority).
    CANDIDATE_DELETION_REQUIRES_DELETION_WORKER = "candidate-deletion-requires-deletion-worker"
    TENANT_DELETION_REQUIRES_DELETION_WORKER = "tenant-deletion-requires-deletion-worker"

    # PROVISION-path adapters and authorization membership.
    ANOTHER_CELL_LIFECYCLE_JOB_OWNS_THE_FIXED_SLOT = (
        "another-cell-lifecycle-job-owns-the-fixed-slot"
    )
    AUTHORIZATION_BUNDLE_DOCUMENT_INVALID = "authorization-bundle-document-invalid"
    AUTHORIZATION_CONTROL_IS_INVALID = "authorization-control-is-invalid"
    AUTHORIZATION_SESSION_EXPIRED_ON_SERVING_CELL = "authorization-session-expired-on-serving-cell"
    AUTHORIZATION_DRAIN_ACKNOWLEDGEMENT_IS_INCOMPLETE = (
        "authorization-drain-acknowledgement-is-incomplete"
    )
    AUTHORIZATION_KEYRING_IS_INVALID = "authorization-keyring-is-invalid"
    AUTHORIZATION_MEMBERSHIP_ATTESTATION_IS_INVALID = (
        "authorization-membership-attestation-is-invalid"
    )
    AUTHORIZATION_MEMBERSHIP_BOOTSTRAP_INPUT_IS_INVALID = (
        "authorization-membership-bootstrap-input-is-invalid"
    )
    AUTHORIZATION_MEMBERSHIP_CHALLENGE_IS_INVALID = "authorization-membership-challenge-is-invalid"
    AUTHORIZATION_MEMBERSHIP_ENTROPY_IS_INVALID = "authorization-membership-entropy-is-invalid"
    AUTHORIZATION_MEMBERSHIP_IDENTITY_IS_INVALID = "authorization-membership-identity-is-invalid"
    AUTHORIZATION_MEMBERSHIP_IS_INVALID = "authorization-membership-is-invalid"
    AUTHORIZATION_MEMBERSHIP_IS_NOT_FULLY_DRAINED = "authorization-membership-is-not-fully-drained"
    AUTHORIZATION_MEMBERSHIP_TIME_IS_INVALID = "authorization-membership-time-is-invalid"
    AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID = (
        "authorization-membership-transition-is-invalid"
    )
    AUTHORIZATION_RUNTIME_ATTESTATION_IS_INVALID = "authorization-runtime-attestation-is-invalid"
    AUTHORIZATION_SECRET_PROVIDER_AUTHORITY_IS_ABSENT = (
        "authorization-secret-provider-authority-is-absent"
    )
    AUTHORIZATION_SECRET_RECOVERY_IDENTITY_UNAUTHENTICATED = (
        "authorization-secret-recovery-identity-unauthenticated"
    )
    AUTHORIZATION_SESSION_BUNDLE_CHANGED_CONCURRENTLY = (
        "authorization-session-bundle-changed-concurrently"
    )
    AUTHORIZATION_SESSION_BUNDLE_PREDECESSOR_DIFFERS = (
        "authorization-session-bundle-predecessor-differs"
    )
    AUTHORIZATION_SESSION_BUNDLE_PREDECESSOR_IS_ABSENT = (
        "authorization-session-bundle-predecessor-is-absent"
    )
    AUTHORIZATION_SESSION_BUNDLE_SHAPE_IS_INVALID = "authorization-session-bundle-shape-is-invalid"
    AUTHORIZATION_SESSION_POD_GENERATION_COULD_NOT_BE_STAGED = (
        "authorization-session-pod-generation-could-not-be-staged"
    )
    AUTHORIZATION_SESSION_REVISION_IS_INVALID = "authorization-session-revision-is-invalid"
    CELL_CREDENTIAL_BUNDLE_IS_ABSENT = "cell-credential-bundle-is-absent"
    CELL_CREDENTIAL_BUNDLE_IS_INVALID = "cell-credential-bundle-is-invalid"
    CELL_CREDENTIAL_BUNDLE_SHAPE_IS_INVALID = "cell-credential-bundle-shape-is-invalid"
    CREDENTIAL_SECRET_RECOVERY_IDENTITY_UNAUTHENTICATED = (
        "credential-secret-recovery-identity-unauthenticated"
    )
    CURRENT_HELM_VALUES_ARE_INVALID = "current-helm-values-are-invalid"
    CURRENT_HELM_VALUES_ARE_UNAVAILABLE = "current-helm-values-are-unavailable"
    HELM_DEPLOYED_REVISION_IS_AMBIGUOUS = "helm-deployed-revision-is-ambiguous"
    HELM_RELEASE_HISTORY_IS_INVALID = "helm-release-history-is-invalid"
    HELM_RELEASE_HISTORY_IS_UNAVAILABLE = "helm-release-history-is-unavailable"
    HELM_RUNTIME_ROLLBACK_AUTHORITY_IS_ABSENT = "helm-runtime-rollback-authority-is-absent"
    HELM_RUNTIME_ROLLBACK_AUTHORITY_IS_AMBIGUOUS = "helm-runtime-rollback-authority-is-ambiguous"
    HELM_RUNTIME_ROLLBACK_FAILED = "helm-runtime-rollback-failed"
    HELM_RUNTIME_UPGRADE_MARKER_IS_INVALID = "helm-runtime-upgrade-marker-is-invalid"
    HELM_VALUES_MUST_NOT_CARRY_PLAINTEXT_CREDENTIALS = (
        "helm-values-must-not-carry-plaintext-credentials"
    )
    HISTORICAL_HELM_VALUES_ARE_INVALID = "historical-helm-values-are-invalid"
    HISTORICAL_HELM_VALUES_ARE_UNAVAILABLE = "historical-helm-values-are-unavailable"
    HOSTED_CELL_REPLICAS_MUST_BE_ZERO_OR_ONE = "hosted-cell-replicas-must-be-zero-or-one"
    INSTALLED_HELM_CLI_DOES_NOT_MATCH_THE_PINNED_VERSION = (
        "installed-helm-cli-does-not-match-the-pinned-version"
    )
    KUBERNETES_OBJECT_IDENTITY_ANNOTATIONS_DIFFER = "kubernetes-object-identity-annotations-differ"
    PINNED_HELM_RECONCILIATION_FAILED = "pinned-helm-reconciliation-failed"
    PRIVATE_CELL_AGENT_CONTRACT_DIGEST_DIFFERS = "private-cell-agent-contract-digest-differs"
    PRIVATE_CELL_AGENT_CONTRACT_IS_INCOMPLETE = "private-cell-agent-contract-is-incomplete"
    PRIVATE_CELL_AGENT_CONTRACT_IS_INVALID = "private-cell-agent-contract-is-invalid"
    PRIVATE_CELL_AGENT_CONTRACT_PROTOCOL_DIFFERS = "private-cell-agent-contract-protocol-differs"
    PRIVATE_CELL_AGENT_CONTRACT_RELEASE_DIFFERS = "private-cell-agent-contract-release-differs"
    PRIVATE_CELL_AGENT_CONTRACT_REQUEST_FAILED = "private-cell-agent-contract-request-failed"
    PRIVATE_CELL_CONTRACT_DIGEST_DIFFERS = "private-cell-contract-digest-differs"
    PRIVATE_CELL_CONTRACT_REQUEST_FAILED = "private-cell-contract-request-failed"
    PRIVATE_CELL_CONTRACT_RESPONSE_IS_INVALID = "private-cell-contract-response-is-invalid"
    PRIVATE_CELL_HEALTH_RESPONSE_IS_INCOMPLETE = "private-cell-health-response-is-incomplete"
    PRIVATE_CELL_LIFECYCLE_REQUEST_FAILED = "private-cell-lifecycle-request-failed"
    PRIVATE_CELL_LIFECYCLE_RESPONSE_DATA_IS_INVALID = (
        "private-cell-lifecycle-response-data-is-invalid"
    )
    PRIVATE_CELL_LIFECYCLE_RESPONSE_IS_INVALID = "private-cell-lifecycle-response-is-invalid"
    PRIVATE_CELL_OPERATOR_OPERATION_IDENTITY_IS_ABSENT = (
        "private-cell-operator-operation-identity-is-absent"
    )
    PRIVATE_CELL_READER_STATUS_IS_INCOMPLETE = "private-cell-reader-status-is-incomplete"
    PRIVATE_CELL_READER_STATUS_IS_INVALID = "private-cell-reader-status-is-invalid"
    PRIVATE_CELL_READER_STATUS_REQUEST_FAILED = "private-cell-reader-status-request-failed"
    RUNTIME_UPGRADE_MARKER_IS_PROVISIONER_OWNED = "runtime-upgrade-marker-is-provisioner-owned"
    SELECTED_RUNTIME_IDENTITY_IS_UNAVAILABLE = "selected-runtime-identity-is-unavailable"
    STALE_AUTHORIZATION_MEMBERSHIP_CANNOT_BE_RENEWED = (
        "stale-authorization-membership-cannot-be-renewed"
    )
    UNSUPPORTED_PRIVATE_CELL_OPERATOR_COMMAND = "unsupported-private-cell-operator-command"
    VAULT_FINGERPRINT_JOB_DID_NOT_COMPLETE_WITHIN_ITS_BOUND = (
        "vault-fingerprint-job-did-not-complete-within-its-bound"
    )
    VAULT_FINGERPRINT_JOB_FAILED = "vault-fingerprint-job-failed"
    VAULT_FINGERPRINT_JOB_IDENTITY_DIFFERS = "vault-fingerprint-job-identity-differs"
    VAULT_FINGERPRINT_JOB_RUNTIME_DIFFERS = "vault-fingerprint-job-runtime-differs"
    VAULT_FINGERPRINT_RESULT_IS_INVALID = "vault-fingerprint-result-is-invalid"
    VAULT_FINGERPRINT_RESULT_IS_UNAVAILABLE = "vault-fingerprint-result-is-unavailable"


def coerce_conflict_reason(value: object) -> ConflictReason:
    """Admit a member of the closed set, and nothing else.

    Applied at construction and again at the terminal boundary, so a reason that
    was never a member -- or was replaced by one after construction -- becomes
    `UNCLASSIFIED` instead of carrying arbitrary text into state or logs.
    """

    return value if isinstance(value, ConflictReason) else ConflictReason.UNCLASSIFIED
