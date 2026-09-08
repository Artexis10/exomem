"""Bind additive authority to the canonical writer's exact commit boundary."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import vocabulary_authority, vocabulary_effects
from .governance.principal import RequestPrincipal
from .vocabulary_workflow import _hash


@dataclass
class _OperationContext:
    vault_root: Path
    key_digest: str | None
    command_digest: str
    receipt_id: str | None
    principal: RequestPrincipal
    uses: list[dict[str, Any]] = field(default_factory=list)
    batch: int = 0
    memory_id_ordinal: int = 0


_OPERATION: ContextVar[_OperationContext | None] = ContextVar(
    "vocabulary_authority_operation", default=None
)


@contextmanager
def operation_context(
    vault_root: Path | None,
    *,
    idempotency_key: str | None,
    command_digest: str,
    receipt_id: str | None,
    principal: RequestPrincipal,
) -> Iterator[_OperationContext | None]:
    """Carry writer-owned identity; this function grants no permission."""
    current = (
        _OperationContext(
            Path(vault_root).resolve(),
            hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None,
            command_digest,
            receipt_id,
            principal,
        )
        if vault_root is not None else None
    )
    token = _OPERATION.set(current)
    try:
        yield current
    finally:
        _OPERATION.reset(token)


def attach_evidence(
    terminal: Any, operation: _OperationContext | None
) -> Any:
    """Publish only authority uses recorded by a completed canonical batch."""
    if not isinstance(terminal, Mapping):
        return terminal
    result = dict(terminal)
    if operation is not None and operation.uses and terminal.get("state") == "committed":
        result["additive_authority"] = {"version": 2, "uses": operation.uses.copy()}
    return result


def deterministic_memory_id() -> str | None:
    """Return the next stable generated page identity for an activated write.

    The writer context exists for v1 too.  We deliberately leave that path to
    ``memory_refs.new_id``'s UUID4 default so merely installing this feature
    does not change ordinary captures or drafts.
    """
    context = _OPERATION.get()
    if context is None or context.key_digest is None:
        return None
    authority = vocabulary_authority.VocabularyAuthority(context.vault_root)
    if authority.runtime_status().mode != "v2":
        return None
    custody = authority._custody_loader(context.vault_root, now=authority._now(None))
    logical_vault_id = custody.control.logical_vault_id
    ordinal = context.memory_id_ordinal
    context.memory_id_ordinal += 1
    encoded = "\0".join(
        (
            "exomem-vocabulary-generated-memory-id/v1",
            logical_vault_id,
            context.key_digest,
            context.command_digest,
            str(ordinal),
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, encoded))


class _BatchAuthority:
    def __init__(self, authority, reservation, operation, context, guard, evidence):
        self.authority = authority
        self.reservation = reservation
        self.operation = operation
        self.context = context
        self.guard = guard
        self.evidence = evidence
        self.closed = False
        self.committed = False

    def commit(self) -> None:
        self.evidence.recheck()
        manifest = list(self.authority.authority_use_manifest(self.reservation))
        receipt = vocabulary_authority._canonical_receipt_for_writer(
            operation=self.operation, receipt_id=self.context.receipt_id
        )
        entry = {
            "authority_use_id": self.reservation.reservation_id,
            "authorities": manifest,
        }
        self.context.uses.append(entry)
        try:
            self.guard.mark_committed(receipt)
        except BaseException:
            self.context.uses.pop()
            raise
        self.committed = True
        self.closed = True
        self._close_evidence()

    def _close_evidence(self) -> None:
        try:
            self.evidence.close()
        except Exception:
            # Evidence is read-only borrowed state. Cleanup cannot undo a
            # canonical commit or turn spent authority into an unspent retry.
            import logging

            logging.getLogger(__name__).warning("authority evidence cleanup failed", exc_info=True)

    def abort(self) -> None:
        if not self.closed:
            self.guard.__exit__(None, None, None)
            self._close_evidence()
            self.closed = True


def _stage_bytes(artifact) -> bytes:
    artifact.recheck()
    previous = os.lseek(artifact.descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(artifact.descriptor, 0, os.SEEK_SET)
        chunks = []
        while chunk := os.read(artifact.descriptor, 64 * 1024):
            chunks.append(chunk)
        content = b"".join(chunks)
    finally:
        os.lseek(artifact.descriptor, previous, os.SEEK_SET)
    if hashlib.sha256(content).hexdigest() != artifact.content_hash:
        raise vocabulary_authority.VocabularyAuthorityUnavailable("staged image changed")
    artifact.recheck()
    return content


def _auxiliary_roles(root: Path, images, manifest) -> dict[str, str]:
    if manifest is None:
        return {}
    from . import vocabulary_auxiliaries
    from .vocabulary_auxiliaries import DerivedAuxiliaryManifest

    if (
        not isinstance(manifest, DerivedAuxiliaryManifest)
        or manifest._seal is not vocabulary_auxiliaries._SEAL  # noqa: SLF001
        or manifest.root != root.absolute()
    ):
        raise vocabulary_authority.VocabularyAuthorityDenied("derived auxiliary declaration changed")
    by_path = {image.path: image for image in images}
    primary = by_path.get(manifest.primary_path)
    if primary is None or primary.after_sha256 != manifest.primary_after_sha256:
        raise vocabulary_authority.VocabularyAuthorityDenied("derived auxiliary declaration changed")
    roles: dict[str, str] = {}
    for claim in manifest.entries:
        image = by_path.get(claim.path)
        if (
            image is None
            or claim.role not in vocabulary_auxiliaries._ROLES  # noqa: SLF001
            or claim.path == manifest.primary_path
            or claim.path in roles
            or image.before_sha256 != claim.before_sha256
            or image.after_sha256 != claim.after_sha256
        ):
            raise vocabulary_authority.VocabularyAuthorityDenied("derived auxiliary declaration changed")
        roles[claim.path] = claim.role
    return roles


def begin_batch(vault_root: Path | None, staged, snapshots, *, auxiliary_manifest=None) -> _BatchAuthority | None:
    """Acquire authority before the first canonical destination replacement."""
    if vault_root is None:
        return None
    root = Path(vault_root).resolve()
    authority = vocabulary_authority.VocabularyAuthority(root)
    status = authority.runtime_status()
    if status.mode == "v1":
        return None
    if status.mode != "v2":
        raise vocabulary_authority.VocabularyAuthorityUnavailable("activated authority is unavailable")
    from .kbdir import kb_dirname

    candidates = []
    for (final, _workspace, artifact), snapshot in zip(staged, snapshots, strict=True):
        try:
            relative = Path(os.path.abspath(final)).relative_to(root).as_posix()
        except ValueError:
            # Existing retained-path guards admit external derived artifacts
            # only for their registered canonical owner.
            from . import vault

            if not vault._batch_state_target(root, final):
                raise vocabulary_authority.VocabularyAuthorityDenied("unclassified external write") from None
            continue
        if relative.startswith(f"{kb_dirname()}/_Governance/curation/runs/"):
            continue  # Existing curation artifact guards still apply.
        candidates.append((relative, artifact, snapshot))
    if not candidates:
        return None
    images = tuple(
        vocabulary_effects.CanonicalWriteImage(
            relative, snapshot.content if snapshot is not None else None, _stage_bytes(artifact),
            before_sha256=snapshot.content_hash if snapshot is not None else None,
            after_sha256=artifact.content_hash,
        )
        for relative, artifact, snapshot in candidates
    )
    roles = _auxiliary_roles(root, images, auxiliary_manifest)
    if roles:
        images = tuple(
            vocabulary_effects.CanonicalWriteImage(
                image.path, image.before, image.after, image.before_sha256,
                image.after_sha256, roles.get(image.path),
            )
            for image in images
        )
    from . import vocabulary_gate_evidence

    try:
        evidence = vocabulary_gate_evidence.classify(root, images, derived_roles=roles)
    except vocabulary_authority.VocabularyAuthorityUnavailable as exc:
        if _OPERATION.get() is None:
            raise vocabulary_authority.VocabularyAuthorityDenied(
                "activated writes require a canonical operation identity"
            ) from exc
        raise
    try:
        classified = evidence.classification
        if classified.state in {"unsupported", "unavailable"}:
            raise vocabulary_authority.VocabularyAuthorityDenied(
                "complete structural effects require current supported evidence"
            )
        if classified.state == "mixed":
            raise vocabulary_authority.VocabularyAuthorityDenied(
                "mixed structural effects require existing owner confirmation"
            )
        if classified.state == "non_additive":
            evidence.close()
            return None
        if classified.state != "reviewed":
            raise vocabulary_authority.VocabularyAuthorityDenied(
                "complete structural effects require current supported evidence"
            )
        if not classified.effects:
            evidence.close()
            return None
        context = _OPERATION.get()
        if (
            context is None or context.vault_root != root
            or context.key_digest is None or context.receipt_id is None
        ):
            raise vocabulary_authority.VocabularyAuthorityDenied(
                "activated writes require a canonical operation identity"
            )
        current = authority._now(None)
        custody = authority._custody_loader(root, now=current)
        operation_id = _hash([
            "additive-authority-operation/v2", custody.control.logical_vault_id,
            context.principal.audience_id, context.key_digest, context.command_digest, context.batch,
        ])
        operation = vocabulary_authority.CanonicalOperation.from_effects(
            operation_id=operation_id,
            command_digest=context.command_digest,
            effects=classified.effects,
            image_digest=classified.digest,
            registry_digests=evidence.registry_digests,
            target_digests=evidence.target_digests,
            scope_proofs=evidence.scope_proofs,
        )
        context.batch += 1
        try:
            reservation = authority.reserve(operation, principal=context.principal)
        except vocabulary_authority.VocabularyAuthorityDenied as exc:
            pending = authority.request(
                operation, principal=context.principal, expires_at=current + 900,
                images=images,
            )
            exc.details.update(
                vocabulary_request_id=pending.request_id,
                next_action={
                    "tool": "govern_memory", "args": {
                        "operation": "vocabulary-request",
                        "vocabulary_request_id": pending.request_id,
                    },
                },
                committed=False,
            )
            raise
        guard = authority.commit_guard(reservation, principal=context.principal)
        guard.__enter__()
        return _BatchAuthority(authority, reservation, operation, context, guard, evidence)
    except BaseException:
        evidence.close()
        raise
