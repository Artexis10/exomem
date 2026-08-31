"""Owner-only persistence for canonical governed-consolidation plans."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .. import reserved_paths
from . import consolidation_plan, consolidation_policy, consolidation_run_state, policy

if TYPE_CHECKING:
    from .consolidation_verification_manifest import VerificationManifest

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_PLAN_KINDS = frozenset({"cutover", "retirement", "rollback"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_PLAN_BYTES = 16 * 1024 * 1024
_MAX_CONTROL_BYTES = 64 * 1024
_MAX_POLICY_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_VERIFICATION_MANIFEST_BYTES = 16 * 1024 * 1024


class ConsolidationPlanStoreUnavailable(RuntimeError):
    """Content-free refusal for missing, stale, or corrupt stored plans."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ConsolidationPlanStoreUnavailable(code) from None


def _run_id(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail("PLAN_STORE_INPUT_INVALID")
    return value


def _plan_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _PLAN_KINDS:
        _fail("PLAN_STORE_INPUT_INVALID")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("PLAN_STORE_INPUT_INVALID")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        _fail("PLAN_STORE_INPUT_INVALID")
    return value


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationPlanStoreUnavailable:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_run_state.ConsolidationRunUnavailable,
    ):
        _fail("PLAN_STORE_UNAVAILABLE")


class ConsolidationPlanStore:
    """Persist exact plan/control bytes beneath the private run subtree."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).absolute()
        self.run_store = consolidation_run_state.ConsolidationRunStore(self.vault_root)

    def _plan_dir(self, run_id: str, plan_kind: str, plan_digest: str) -> Path:
        return self.run_store.base / run_id / "plans" / plan_kind / plan_digest

    def _read_optional(self, path: Path, *, limit: int) -> bytes | None:
        try:
            return reserved_paths._read_owner_bytes(  # noqa: SLF001
                self.vault_root,
                path,
                _DESCRIPTOR_ID,
                limit=limit,
            )
        except FileNotFoundError:
            return None

    def _publish_missing(self, path: Path, value: bytes) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            value,
            require_missing=True,
        )

    @staticmethod
    def _bind_run(
        record: consolidation_run_state.ConsolidationRunRecord,
        plan: consolidation_plan.CanonicalConsolidationPlan,
    ) -> None:
        if (
            plan.preimage["run_id"] != record.run_id
            or plan.preimage["run_mode"] != record.identity.run_mode
            or plan.preimage["source_snapshot_fingerprint"] != record.identity.source_fingerprint
            or plan.preimage["destination_snapshot_fingerprint"]
            != record.identity.destination_snapshot_fingerprint
        ):
            _fail("PLAN_RUN_MISMATCH")

    @staticmethod
    def _bind_policy_bundle(
        record: consolidation_run_state.ConsolidationRunRecord,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        bundle: consolidation_policy.DestinationPolicyPlan,
    ) -> None:
        documents = tuple(
            {
                "path": path,
                "content": raw.decode("utf-8"),
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for path, raw in bundle.prospective.target_documents
            if policy._document_kind(path) is not None  # noqa: SLF001
        )
        if (
            plan.preimage["plan_kind"] != "cutover"
            or plan.preimage["policy_bundle_digest"] != bundle.digest
            or plan.preimage["policy_documents"] != documents
            or plan.preimage["prospective_policy_fingerprint"]
            != bundle.prospective.policy.fingerprint
            or plan.preimage["principal_attestation_set_digest"]
            != bundle.principal_attestation_set_digest
            or plan.preimage["nonce"] != bundle.nonce
            or record.identity.destination_vault_id != bundle.destination_vault_id
        ):
            _fail("PLAN_POLICY_BUNDLE_MISMATCH")

    def persist(
        self,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        *,
        policy_bundle: consolidation_policy.DestinationPolicyPlan | None = None,
        verification_manifest: VerificationManifest | None = None,
        expected_run_revision: int,
    ) -> consolidation_plan.CanonicalConsolidationPlan:
        """Create or adopt one byte-identical immutable plan."""

        if not isinstance(plan, consolidation_plan.CanonicalConsolidationPlan):
            _fail("PLAN_STORE_INPUT_INVALID")
        revision = _revision(expected_run_revision)
        try:
            checked = consolidation_plan.parse_canonical_plan(
                plan.canonical_bytes,
                control_basis=plan.control_basis,
            )
        except consolidation_plan.ConsolidationPlanUnavailable:
            _fail("PLAN_STORE_INPUT_INVALID")
        if checked != plan:
            _fail("PLAN_STORE_INPUT_INVALID")
        run_id = _run_id(plan.preimage["run_id"])
        kind = _plan_kind(plan.preimage["plan_kind"])
        digest = _digest(plan.digest)
        if plan.control_basis.preimage["basis_run_revision"] != revision:
            _fail("PLAN_RUN_REVISION_CONFLICT")
        directory = self._plan_dir(run_id, kind, digest)
        control_path = directory / "control-basis.json"
        policy_bundle_path = directory / "policy-bundle.json"
        verification_manifest_path = directory / "verification-manifest.json"
        plan_path = directory / "plan.json"

        policy_bundle_raw: bytes | None = None
        verification_manifest_raw: bytes | None = None
        if kind == "cutover":
            from . import consolidation_verification_manifest as verification_manifest_module

            if not isinstance(policy_bundle, consolidation_policy.DestinationPolicyPlan):
                _fail("PLAN_POLICY_BUNDLE_REQUIRED")
            if not isinstance(
                verification_manifest,
                verification_manifest_module.VerificationManifest,
            ):
                _fail("PLAN_VERIFICATION_MANIFEST_REQUIRED")
            try:
                policy_bundle_raw = consolidation_policy.canonical_destination_policy_bundle(
                    policy_bundle
                )
                if (
                    consolidation_policy.parse_destination_policy_bundle(policy_bundle_raw)
                    != policy_bundle
                ):
                    _fail("PLAN_STORE_INPUT_INVALID")
                verification_manifest_module._bind_to_stored_plan(  # noqa: SLF001
                    run_id=run_id,
                    plan_digest=digest,
                    plan=plan,
                    bundle=policy_bundle,
                    manifest=verification_manifest,
                )
                verification_manifest_raw = (
                    verification_manifest_module.canonical_verification_manifest(
                        verification_manifest
                    )
                )
            except (
                consolidation_policy.DestinationPolicyUnavailable,
                verification_manifest_module.ConsolidationVerificationManifestUnavailable,
            ):
                _fail("PLAN_STORE_INPUT_INVALID")
        elif policy_bundle is not None or verification_manifest is not None:
            _fail("PLAN_STORE_INPUT_INVALID")

        with _authority(self.vault_root, mutation=True):
            record, _inventory, _raw = self.run_store._load_locked(run_id)  # noqa: SLF001
            if record.revision != revision:
                _fail("PLAN_RUN_REVISION_CONFLICT")
            self._bind_run(record, plan)
            if policy_bundle is not None:
                self._bind_policy_bundle(record, plan, policy_bundle)
            existing_control = self._read_optional(
                control_path,
                limit=_MAX_CONTROL_BYTES,
            )
            existing_policy_bundle = self._read_optional(
                policy_bundle_path,
                limit=_MAX_POLICY_BUNDLE_BYTES,
            )
            existing_verification_manifest = self._read_optional(
                verification_manifest_path,
                limit=_MAX_VERIFICATION_MANIFEST_BYTES,
            )
            existing_plan = self._read_optional(plan_path, limit=_MAX_PLAN_BYTES)
            if existing_control is None and (
                existing_policy_bundle is not None
                or existing_verification_manifest is not None
                or existing_plan is not None
            ):
                _fail("PLAN_STORE_CORRUPT")
            if (
                kind == "cutover"
                and existing_plan is not None
                and (existing_policy_bundle is None or existing_verification_manifest is None)
            ) or (
                kind != "cutover"
                and (
                    existing_policy_bundle is not None or existing_verification_manifest is not None
                )
            ):
                _fail("PLAN_STORE_CORRUPT")
            if (
                (
                    existing_control is not None
                    and existing_control != plan.control_basis.canonical_bytes
                )
                or (
                    existing_policy_bundle is not None
                    and existing_policy_bundle != policy_bundle_raw
                )
                or (
                    existing_verification_manifest is not None
                    and existing_verification_manifest != verification_manifest_raw
                )
                or (existing_plan is not None and existing_plan != plan.canonical_bytes)
            ):
                _fail("PLAN_STORE_CONFLICT")
            if existing_control is None:
                self._publish_missing(control_path, plan.control_basis.canonical_bytes)
            if existing_policy_bundle is None and policy_bundle_raw is not None:
                self._publish_missing(policy_bundle_path, policy_bundle_raw)
            if existing_verification_manifest is None and verification_manifest_raw is not None:
                self._publish_missing(
                    verification_manifest_path,
                    verification_manifest_raw,
                )
            if existing_plan is None:
                self._publish_missing(plan_path, plan.canonical_bytes)
        return plan

    def _load_exact(
        self,
        run_id: str,
        *,
        plan_kind: str,
        plan_digest: str,
    ) -> tuple[
        consolidation_plan.CanonicalConsolidationPlan,
        consolidation_policy.DestinationPolicyPlan | None,
    ]:
        run_id = _run_id(run_id)
        kind = _plan_kind(plan_kind)
        digest = _digest(plan_digest)
        directory = self._plan_dir(run_id, kind, digest)
        with _authority(self.vault_root, mutation=False):
            record, _inventory, _raw = self.run_store._load_locked(run_id)  # noqa: SLF001
            control_raw = self._read_optional(
                directory / "control-basis.json",
                limit=_MAX_CONTROL_BYTES,
            )
            policy_bundle_raw = self._read_optional(
                directory / "policy-bundle.json",
                limit=_MAX_POLICY_BUNDLE_BYTES,
            )
            verification_manifest_raw = self._read_optional(
                directory / "verification-manifest.json",
                limit=_MAX_VERIFICATION_MANIFEST_BYTES,
            )
            plan_raw = self._read_optional(
                directory / "plan.json",
                limit=_MAX_PLAN_BYTES,
            )
            if (
                control_raw is None
                and policy_bundle_raw is None
                and verification_manifest_raw is None
                and plan_raw is None
            ):
                _fail("PLAN_NOT_FOUND")
            if control_raw is None or plan_raw is None:
                _fail("PLAN_STORE_CORRUPT")
            if (kind == "cutover") != (policy_bundle_raw is not None) or (
                (kind == "cutover") != (verification_manifest_raw is not None)
            ):
                _fail("PLAN_STORE_CORRUPT")
            try:
                control = consolidation_plan.parse_control_basis(control_raw)
                plan = consolidation_plan.parse_canonical_plan(
                    plan_raw,
                    control_basis=control,
                )
                bundle = (
                    None
                    if policy_bundle_raw is None
                    else consolidation_policy.parse_destination_policy_bundle(policy_bundle_raw)
                )
            except (
                consolidation_plan.ConsolidationPlanUnavailable,
                consolidation_policy.DestinationPolicyUnavailable,
            ):
                _fail("PLAN_STORE_CORRUPT")
            if (
                plan.digest != digest
                or plan.preimage["run_id"] != run_id
                or plan.preimage["plan_kind"] != kind
            ):
                _fail("PLAN_STORE_CORRUPT")
            self._bind_run(record, plan)
            if bundle is not None:
                self._bind_policy_bundle(record, plan, bundle)
                from . import consolidation_verification_manifest as verification_manifest_module

                if verification_manifest_raw is None:
                    _fail("PLAN_STORE_CORRUPT")
                try:
                    manifest = verification_manifest_module.parse_verification_manifest(
                        verification_manifest_raw
                    )
                    verification_manifest_module._bind_to_stored_plan(  # noqa: SLF001
                        run_id=run_id,
                        plan_digest=digest,
                        plan=plan,
                        bundle=bundle,
                        manifest=manifest,
                    )
                except (verification_manifest_module.ConsolidationVerificationManifestUnavailable,):
                    _fail("PLAN_STORE_CORRUPT")
        return plan, bundle

    def load(
        self,
        run_id: str,
        *,
        plan_kind: str,
        plan_digest: str,
    ) -> consolidation_plan.CanonicalConsolidationPlan:
        """Load an exact stored plan by its opaque run/kind/digest key."""

        plan, _bundle = self._load_exact(
            run_id,
            plan_kind=plan_kind,
            plan_digest=plan_digest,
        )
        return plan

    def load_policy_bundle(
        self,
        run_id: str,
        *,
        plan_kind: str,
        plan_digest: str,
    ) -> consolidation_policy.DestinationPolicyPlan:
        """Load the exact policy bundle bound by one stored cutover plan."""

        _plan, bundle = self._load_exact(
            run_id,
            plan_kind=plan_kind,
            plan_digest=plan_digest,
        )
        if bundle is None:
            _fail("PLAN_POLICY_BUNDLE_REQUIRED")
        return bundle


__all__ = ["ConsolidationPlanStore", "ConsolidationPlanStoreUnavailable"]
