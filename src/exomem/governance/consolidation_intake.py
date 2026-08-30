"""Opaque, authenticated, private intake for governed vault consolidation.

This module is deliberately below the product command.  Public requests carry
only opaque archive/proof references; a configured resolver supplies their
private bytes and trust records.  Verified source bytes are copied into a
content-addressed private store, never restored into a vault or exposed through
ordinary recall.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .. import hosted_portability
from . import authorization_custody, consolidation_attestation, consolidation_fingerprints

_ARCHIVE_REF = re.compile(r"exomem-export://sha256/([0-9a-f]{64})\Z")
_SOURCE_PROOF_REF = re.compile(r"exomem-source-attestation://sha256/([0-9a-f]{64})\Z")
_PRIVATE_ARCHIVE_REF = re.compile(
    r"exomem-consolidation-archive://sha256/([0-9a-f]{64})\Z"
)
_PRIVATE_PROOF_REF = re.compile(
    r"exomem-consolidation-proof://sha256/([0-9a-f]{64})\Z"
)
_PRIVATE_OBJECT_REF = re.compile(
    r"exomem-consolidation-object://sha256/([0-9a-f]{64})\Z"
)
_PRIVATE_PREIMAGE_REF = re.compile(
    r"exomem-consolidation-preimage://sha256/([0-9a-f]{64})\Z"
)


class ConsolidationIntakeUnavailable(RuntimeError):
    """Stable, content-free refusal at the private intake boundary."""

    code = "CONSOLIDATION_INTAKE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation intake is unavailable")


@dataclass(frozen=True, slots=True)
class ConsolidationIntakeRequest:
    source_artifact_ref: str
    source_attestation_ref: str


@dataclass(frozen=True, slots=True)
class ResolvedSourceExportProof:
    """Proof and trust facts returned by a configured private resolver."""

    claim_bytes: bytes
    signature: str
    expectation: consolidation_attestation.SourceExportExpectation
    verifier_records: tuple[consolidation_attestation.SourceExportVerifierRecord, ...]


class ConsolidationIntakeResolver(Protocol):
    """Private control-plane resolver; never implemented by request content."""

    def resolve_archive(self, reference: str) -> Path: ...

    def resolve_source_proof(self, reference: str) -> ResolvedSourceExportProof: ...


@dataclass(frozen=True, slots=True)
class ConsolidationInventoryItem:
    path: str
    size: int
    sha256: str
    classification: str
    artifact_ref: str

    def to_bounded_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "classification": self.classification,
            "artifact_ref": self.artifact_ref,
        }


@dataclass(frozen=True, slots=True)
class ConsolidationIntakeResult:
    archive_artifact_ref: str
    archive_sha256: str
    manifest_sha256: str
    source_census_sha256: str
    source_proof_artifact_ref: str
    source_proof_digest: str
    source_claims_digest: str
    source_fingerprint: str
    object_count: int
    total_bytes: int
    inventory: tuple[ConsolidationInventoryItem, ...]

    def to_bounded_dict(self) -> dict[str, object]:
        return {
            "archive_artifact_ref": self.archive_artifact_ref,
            "archive_sha256": self.archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "source_census_sha256": self.source_census_sha256,
            "source_proof_artifact_ref": self.source_proof_artifact_ref,
            "source_proof_digest": self.source_proof_digest,
            "source_claims_digest": self.source_claims_digest,
            "source_fingerprint": self.source_fingerprint,
            "object_count": self.object_count,
            "total_bytes": self.total_bytes,
            "inventory": [item.to_bounded_dict() for item in self.inventory],
        }


def _proof_bytes(claim_bytes: bytes, signature: str) -> bytes:
    if not isinstance(claim_bytes, bytes) or not isinstance(signature, str):
        raise ConsolidationIntakeUnavailable
    try:
        value = {
            "claim_bytes": base64.urlsafe_b64encode(claim_bytes)
            .decode("ascii")
            .rstrip("="),
            "signature": signature,
        }
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ConsolidationIntakeUnavailable from None


def detached_source_proof_digest(claim_bytes: bytes, signature: str) -> str:
    return hashlib.sha256(_proof_bytes(claim_bytes, signature)).hexdigest()


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise ConsolidationIntakeUnavailable from None
    return digest.hexdigest()


def _overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _existing_ancestor_is_unsafe(path: Path) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for current in (path, *path.parents):
        if not os.path.lexists(current):
            continue
        try:
            info = current.lstat()
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & reparse
        ):
            return True
    return False


def _ensure_private_directory(path: Path) -> None:
    if _existing_ancestor_is_unsafe(path):
        raise ConsolidationIntakeUnavailable
    try:
        authorization_custody._prepare_private_directory(path)  # noqa: SLF001
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConsolidationIntakeUnavailable
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ConsolidationIntakeUnavailable
    except ConsolidationIntakeUnavailable:
        raise
    except (authorization_custody.AuthorizationCustodyUnavailable, OSError):
        raise ConsolidationIntakeUnavailable from None


def _fsync_file(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        raise ConsolidationIntakeUnavailable from None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise ConsolidationIntakeUnavailable from None
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise ConsolidationIntakeUnavailable from None
    finally:
        os.close(descriptor)


class PrivateConsolidationArtifactStore:
    """Durable private content-addressed store outside every active vault."""

    def __init__(self, root: Path | str, *, active_vault_roots: tuple[Path | str, ...]):
        if not isinstance(active_vault_roots, tuple):
            raise ConsolidationIntakeUnavailable
        self.root = Path(root).absolute()
        try:
            resolved_root = self.root.resolve(strict=False)
            active = tuple(
                Path(item).absolute().resolve(strict=False)
                for item in active_vault_roots
            )
        except (OSError, RuntimeError, ValueError):
            raise ConsolidationIntakeUnavailable from None
        if any(part.casefold() == "knowledge base" for part in resolved_root.parts) or any(
            _overlap(resolved_root, item) for item in active
        ):
            raise ConsolidationIntakeUnavailable
        if _existing_ancestor_is_unsafe(self.root):
            raise ConsolidationIntakeUnavailable
        self._active_vault_roots = active

    def _ensure(self) -> None:
        _ensure_private_directory(self.root)
        for name in ("archives", "proofs", "objects", "preimages"):
            _ensure_private_directory(self.root / name)

    def _object_path(self, digest: str) -> Path:
        return self.root / "objects" / digest[:2] / digest

    def _resolve(
        self,
        reference: str,
        *,
        pattern: re.Pattern[str],
        directory: str,
        suffix: str,
    ) -> Path:
        match = pattern.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise ConsolidationIntakeUnavailable
        digest = match.group(1)
        path = (
            self._object_path(digest)
            if directory == "objects"
            else self.root / directory / f"{digest}{suffix}"
        )
        try:
            info = path.lstat()
        except OSError:
            raise ConsolidationIntakeUnavailable from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConsolidationIntakeUnavailable
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ConsolidationIntakeUnavailable
        if _content_digest(path) != digest:
            raise ConsolidationIntakeUnavailable
        return path

    def resolve_object(self, reference: str) -> Path:
        return self._resolve(
            reference,
            pattern=_PRIVATE_OBJECT_REF,
            directory="objects",
            suffix="",
        )

    def resolve_archive(self, reference: str) -> Path:
        return self._resolve(
            reference,
            pattern=_PRIVATE_ARCHIVE_REF,
            directory="archives",
            suffix=".zip",
        )

    def resolve_source_proof(self, reference: str) -> Path:
        return self._resolve(
            reference,
            pattern=_PRIVATE_PROOF_REF,
            directory="proofs",
            suffix=".json",
        )

    def resolve_preimage(self, reference: str) -> Path:
        return self._resolve(
            reference,
            pattern=_PRIVATE_PREIMAGE_REF,
            directory="preimages",
            suffix=".json",
        )

    def _install(self, staged: Path, destination: Path, expected_digest: str) -> None:
        _ensure_private_directory(destination.parent)
        try:
            os.chmod(staged, 0o600)
            info = staged.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConsolidationIntakeUnavailable
            if _content_digest(staged) != expected_digest:
                raise ConsolidationIntakeUnavailable
            try:
                os.link(staged, destination)
                created = True
            except FileExistsError:
                created = False
            if not created:
                existing = destination.lstat()
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                    raise ConsolidationIntakeUnavailable
                if _content_digest(destination) != expected_digest:
                    raise ConsolidationIntakeUnavailable
            _fsync_file(destination)
            _fsync_directory(destination.parent)
        except ConsolidationIntakeUnavailable:
            raise
        except OSError:
            raise ConsolidationIntakeUnavailable from None

    def install_object_file(self, staged: Path, *, expected_digest: str) -> str:
        """Install one independently staged immutable object and return its opaque ref."""

        if not isinstance(staged, Path) or _PRIVATE_OBJECT_REF.fullmatch(
            f"exomem-consolidation-object://sha256/{expected_digest}"
        ) is None:
            raise ConsolidationIntakeUnavailable
        self._ensure()
        self._install(staged, self._object_path(expected_digest), expected_digest)
        return f"exomem-consolidation-object://sha256/{expected_digest}"

    def install_preimage_bytes(self, payload: bytes) -> str:
        """Publish one canonical preimage manifest after all objects are durable."""

        if not isinstance(payload, bytes) or not payload:
            raise ConsolidationIntakeUnavailable
        self._ensure()
        digest = hashlib.sha256(payload).hexdigest()
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(prefix=".preimage-", dir=self.root)
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._install(
                temporary,
                self.root / "preimages" / f"{digest}.json",
                digest,
            )
        except ConsolidationIntakeUnavailable:
            raise
        except OSError:
            raise ConsolidationIntakeUnavailable from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return f"exomem-consolidation-preimage://sha256/{digest}"


def _request_refs(request: ConsolidationIntakeRequest) -> tuple[str, str]:
    if not isinstance(request, ConsolidationIntakeRequest):
        raise ConsolidationIntakeUnavailable
    archive = request.source_artifact_ref
    proof = request.source_attestation_ref
    if (
        not isinstance(archive, str)
        or _ARCHIVE_REF.fullmatch(archive) is None
        or not isinstance(proof, str)
        or _SOURCE_PROOF_REF.fullmatch(proof) is None
    ):
        raise ConsolidationIntakeUnavailable
    return archive, proof


def _resolved_proof(
    value: object,
) -> ResolvedSourceExportProof:
    if not isinstance(value, ResolvedSourceExportProof):
        raise ConsolidationIntakeUnavailable
    if not isinstance(value.claim_bytes, bytes) or not isinstance(value.signature, str):
        raise ConsolidationIntakeUnavailable
    if not isinstance(value.expectation, consolidation_attestation.SourceExportExpectation):
        raise ConsolidationIntakeUnavailable
    if not isinstance(value.verifier_records, tuple):
        raise ConsolidationIntakeUnavailable
    return value


def intake_source_export(
    request: ConsolidationIntakeRequest,
    *,
    resolver: ConsolidationIntakeResolver,
    artifact_store: PrivateConsolidationArtifactStore,
    verified_at: str,
    limits: hosted_portability.PortabilityLimits | None = None,
) -> ConsolidationIntakeResult:
    """Resolve, authenticate, and privately content-address one source export."""

    archive_ref, proof_ref = _request_refs(request)
    if not isinstance(artifact_store, PrivateConsolidationArtifactStore):
        raise ConsolidationIntakeUnavailable
    try:
        archive_path = resolver.resolve_archive(archive_ref)
        proof = _resolved_proof(resolver.resolve_source_proof(proof_ref))
    except ConsolidationIntakeUnavailable:
        raise
    except Exception:  # noqa: BLE001 - private resolver failures share one refusal
        raise ConsolidationIntakeUnavailable from None

    archive_match = _ARCHIVE_REF.fullmatch(archive_ref)
    proof_match = _SOURCE_PROOF_REF.fullmatch(proof_ref)
    assert archive_match is not None and proof_match is not None
    if detached_source_proof_digest(proof.claim_bytes, proof.signature) != proof_match.group(1):
        raise ConsolidationIntakeUnavailable

    try:
        verified = hosted_portability.verify_export_archive(
            archive_path,
            expected_cell_id=proof.expectation.source_cell_id,
            expected_vault_id=proof.expectation.source_vault_id,
            limits=limits,
        )
        census = consolidation_fingerprints.source_content_census_from_manifest(
            verified.manifest
        ).digest
        if (
            verified.archive_sha256 != archive_match.group(1)
            or verified.archive_sha256 != proof.expectation.archive_sha256
            or str(verified.manifest["overall_digest"]["value"])
            != proof.expectation.manifest_sha256
            or census != proof.expectation.source_census_sha256
        ):
            raise ConsolidationIntakeUnavailable
        trusted = consolidation_attestation.verify_source_export_attestation(
            proof.claim_bytes,
            proof.signature,
            proof.verifier_records,
            expectation=proof.expectation,
            verified_at=verified_at,
            verification_gate="intake",
        )
    except ConsolidationIntakeUnavailable:
        raise
    except (
        hosted_portability.PortabilityError,
        consolidation_attestation.SourceExportAttestationUnavailable,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise ConsolidationIntakeUnavailable from None

    artifact_store._ensure()
    transaction = Path(tempfile.mkdtemp(prefix=".intake-", dir=artifact_store.root))
    try:
        os.chmod(transaction, 0o700)
        archive_copy = transaction / "archive.zip"
        shutil.copyfile(verified.archive_path, archive_copy)
        os.chmod(archive_copy, 0o600)
        _fsync_file(archive_copy)
        copied = hosted_portability.verify_export_archive(
            archive_copy,
            expected_cell_id=proof.expectation.source_cell_id,
            expected_vault_id=proof.expectation.source_vault_id,
            limits=limits,
        )
        if copied.archive_sha256 != verified.archive_sha256 or copied.manifest != verified.manifest:
            raise ConsolidationIntakeUnavailable

        extracted_root = transaction / "extracted"
        extracted = hosted_portability.extract_verified_archive(
            copied,
            extracted_root,
            limits=limits,
        )

        proof_bytes = _proof_bytes(proof.claim_bytes, proof.signature)
        proof_digest = hashlib.sha256(proof_bytes).hexdigest()
        source_fingerprint = consolidation_fingerprints.source_fingerprint(
            dict(trusted.claims),
            authentication_proof_digest=proof_digest,
        ).digest
        proof_path = transaction / "proof.json"
        proof_path.write_bytes(proof_bytes)
        os.chmod(proof_path, 0o600)
        _fsync_file(proof_path)

        inventory: list[ConsolidationInventoryItem] = []
        for record in copied.manifest["files"]:
            relative = str(record["path"])
            source = extracted.staging_root.joinpath(*PurePosixPath(relative).parts)
            digest = str(record["sha256"])
            destination = artifact_store._object_path(digest)
            artifact_store._install(source, destination, digest)
            inventory.append(
                ConsolidationInventoryItem(
                    path=relative,
                    size=int(record["size"]),
                    sha256=digest,
                    classification=str(record["classification"]),
                    artifact_ref=f"exomem-consolidation-object://sha256/{digest}",
                )
            )

        archive_destination = (
            artifact_store.root / "archives" / f"{verified.archive_sha256}.zip"
        )
        artifact_store._install(
            archive_copy,
            archive_destination,
            verified.archive_sha256,
        )
        proof_destination = artifact_store.root / "proofs" / f"{proof_digest}.json"
        artifact_store._install(proof_path, proof_destination, proof_digest)

        _fsync_directory(artifact_store.root)
        result = ConsolidationIntakeResult(
            archive_artifact_ref=(
                f"exomem-consolidation-archive://sha256/{verified.archive_sha256}"
            ),
            archive_sha256=verified.archive_sha256,
            manifest_sha256=str(verified.manifest["overall_digest"]["value"]),
            source_census_sha256=census,
            source_proof_artifact_ref=(
                f"exomem-consolidation-proof://sha256/{proof_digest}"
            ),
            source_proof_digest=proof_digest,
            source_claims_digest=trusted.claims_sha256,
            source_fingerprint=source_fingerprint,
            object_count=len(inventory),
            total_bytes=sum(item.size for item in inventory),
            inventory=tuple(inventory),
        )
    except ConsolidationIntakeUnavailable:
        raise
    except Exception:  # noqa: BLE001 - intake never exposes private failure details
        raise ConsolidationIntakeUnavailable from None
    finally:
        shutil.rmtree(transaction, ignore_errors=True)
    return result


__all__ = [
    "ConsolidationIntakeRequest",
    "ConsolidationIntakeResolver",
    "ConsolidationIntakeResult",
    "ConsolidationIntakeUnavailable",
    "ConsolidationInventoryItem",
    "PrivateConsolidationArtifactStore",
    "ResolvedSourceExportProof",
    "detached_source_proof_digest",
    "intake_source_export",
]
