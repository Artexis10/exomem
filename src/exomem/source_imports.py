"""Add one durable receipt for an authenticated importer's exact Source upload."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from . import duplicates, memory_refs, private_state, reserved_paths, source_capture, vault
from .governance.principal import effective_principal, owner_local_producer

if TYPE_CHECKING:
    from .governance.lifecycle import ReceiptLifecycle

OWNER = "source_imports"
_MAX_ID_CHARS = 256
_MAX_RECEIPT_BYTES = 8192
# A receipt needs one generated Source companion. An oversized edit stays available through ordinary reads.
_MAX_RECEIPT_PAGE_BYTES = 1 << 20
# Bound request work on a large audit history. An incomplete gate cannot expose custody identifiers.
_RECEIPT_READ_SECONDS = 0.25
_PREPARED_STORE = "source-import-intents"
_UNAVAILABLE = "The imported Source could not be verified. Keep the same import ID when retrying."


def validate_import_identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ID_CHARS:
        raise ValueError(f"INVALID_IMPORT_IDENTITY: use 1 to {_MAX_ID_CHARS} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("INVALID_IMPORT_IDENTITY: use valid Unicode") from None
    return value


def issuer_key(value: object) -> str:
    external = validate_import_identity(value)
    who = effective_principal()
    binding = who.client_binding
    if not who.resolved or binding is None or not who.issuer_family:
        raise source_capture.SourceCaptureError(
            "IMPORT_IDENTITY_FORBIDDEN: Use an authenticated importer with a verified client binding."
        )
    return _digest([who.audience_id, who.issuer_family, binding.issuer, binding.client_id, external])


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Receipt(BaseModel, frozen=True, extra="forbid"):
    version: Literal[1] = 1
    key: str
    input_digest: str
    original_sha256: str
    page: str
    source_hash: str
    ref: str
    artifact: str
    protected: bool
    captured: str | None = None


@dataclass(frozen=True, slots=True)
class ImportCapture:
    key: str
    input_digest: str
    original_sha256: str
    protected: bool

    @classmethod
    def bind(cls, root: Path, key: str, artifact, *, title: str, source_fields) -> ImportCapture:
        from .add import source_artifact_name

        source_capture.verify_sha256(source_fields.get("expected_sha256"), artifact.sha256)
        metadata = source_capture.capture_digest(
            root, content="", title=title, source_type=source_fields["source_type"], url=source_fields.get("url"),
            tags=source_fields.get("tags"), why_captured=source_fields.get("why_captured"),
            domain=source_fields.get("domain"), projects=source_fields.get("projects"), slug=source_fields.get("slug"),
            raw_protection=bool(source_fields.get("raw_protection")),
        )
        fingerprint = _digest([
            1, metadata, artifact.sha256, source_artifact_name(artifact.filename),
            artifact.content_type, source_fields.get("document_form"), source_fields.get("custody_only") is True,
        ])
        return cls(key, fingerprint, artifact.sha256, bool(source_fields.get("raw_protection")))

    def _path(self, root: Path) -> Path:
        return receipt_root(root) / f"{self.key}.json"

    def receipt(
        self, page: str, ref: str, artifact: str, source_hash: str, *, captured: str | None = None,
    ) -> Receipt:
        return Receipt(
            key=self.key, input_digest=self.input_digest, original_sha256=self.original_sha256,
            page=page, ref=ref, artifact=artifact, source_hash=source_hash, protected=self.protected, captured=captured,
        )

    def planned_write(self, root: Path, receipt: Receipt) -> vault.PlannedWrite:
        content = receipt.model_dump_json() + "\n"
        if len(content.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        path = self._path(root)
        with reserved_paths._subsystem_authority_scope(OWNER):
            guard = vault.PathGuard.capture(root, path.relative_to(root).as_posix(), leaf_policy="absent")
        return vault.PlannedWrite(
            path, content, create_only=True, expected_hash=vault.MISSING_CONTENT_HASH, guard=guard,
        )

    def _read_receipt(self, raw: bytes) -> Receipt:
        try:
            receipt = Receipt.model_validate_json(raw)
        except ValidationError:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}") from None
        if receipt.key != self.key:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        return receipt

    def _require_same_input(self, receipt: Receipt) -> None:
        if receipt.input_digest != self.input_digest or receipt.original_sha256 != self.original_sha256:
            raise source_capture.SourceCaptureError(
                "IMPORT_IDENTITY_CONFLICT: This import ID already names different bytes or capture metadata."
            )

    def lookup(
        self, root: Path, artifact,
    ) -> tuple[duplicates.Duplicate | None, bool, tuple[Receipt, vault.PathGuard] | None]:
        """Verify one exact committed receipt, or reconcile an interrupted file publication."""
        rel = self._path(root).relative_to(root).as_posix()
        raw = None
        with reserved_paths._subsystem_authority_scope(OWNER):
            try:
                vault.PathGuard.capture(root, rel, leaf_policy="absent")
            except vault.PathGuardError:
                try:
                    raw, _ = vault.read_bounded_guarded_bytes(root, rel, limit=_MAX_RECEIPT_BYTES)
                except (OSError, ValueError, reserved_paths.ReservedPathLeafError) as error:
                    raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}") from error
        if raw is not None:
            receipt = self._read_receipt(raw)
            if duplicates.withheld(root, receipt.page):
                raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
            self._require_same_input(receipt)
            duplicate = self._verified_duplicate(root, receipt)
            recovery_pending = self._pending_path(root).is_file()
            if recovery_pending:
                self._refresh_payload(root, receipt.page)
            return duplicate, recovery_pending, None

        store = private_state.store(root, _PREPARED_STORE)
        try:
            with (store / f"{self.key}.json").open("rb") as pending:
                raw = pending.read(_MAX_RECEIPT_BYTES + 1)
        except FileNotFoundError:
            return None, False, None
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        receipt = self._read_receipt(raw)
        try:
            vault.PathGuard.capture(root, receipt.page, leaf_policy="absent")
        except vault.PathGuardError:
            if duplicates.withheld(root, receipt.page):
                raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}") from None
            self._require_same_input(receipt)
            self._recover_original(root, receipt, artifact)
            duplicate = self._verified_duplicate(root, receipt)
            self._refresh_payload(root, receipt.page)
            self.commit_duplicate(root, duplicate)
            return duplicate, True, None
        try:
            vault.PathGuard.capture(root, receipt.artifact, leaf_policy="absent")
        except vault.PathGuardError:
            # Store batches publish the original before their SQLite page commit.
            # Adopt its exact guarded bytes instead of deleting or overwriting it.
            from .governance import connector_boundary
            from .vault_store import availability

            connector_boundary.require_create(root, receipt.page)
            self._require_same_input(receipt)
            if availability.page_writer(root) is None or receipt.captured is None:
                raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}") from None
            guard = vault.PathGuard.capture(
                root, receipt.artifact, leaf_policy="content",
                expected_content_hash=self.original_sha256, expected_content_size=artifact.size,
            )
            return None, False, (receipt, guard)
        self.finish(root)
        return None, False, None

    def _recover_original(self, root: Path, receipt: Receipt, artifact) -> None:
        """Complete only the missing original of the exact, already-installed Source."""
        from . import vocabulary_auxiliaries
        from .add import artifact_binary_write, pools_original
        from .governance import connector_boundary

        payload = duplicates.readable_page(root, receipt.page)
        if (
            payload is None or payload.ref != receipt.ref or payload.artifact_path != receipt.artifact
            or payload.sha256 != self.original_sha256 or payload.protected != self.protected
        ):
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        if duplicates.original_intact(root, receipt.page, payload, bound=True):
            return
        page = root / receipt.page
        target = root / receipt.artifact
        if (
            vault.content_hash(payload.text) != receipt.source_hash
            or target.parent != page.parent or pools_original(root, target)
            or duplicates.binding_unproven(root, receipt.page, payload)
        ):
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        try:
            page_guard = vault.PathGuard.capture(
                root, receipt.page, leaf_policy="content", expected_content_hash=receipt.source_hash,
            )
            binary_guard = vault.PathGuard.capture(root, receipt.artifact, leaf_policy="absent")
        except vault.PathGuardError:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}") from None
        # Require the current caller to be able to publish both exact components.
        # Only the bound original goes to the repair writer.
        binary, stream = artifact_binary_write(artifact.path, target, self.original_sha256, artifact.size)
        binary.guard = binary_guard
        with stream:
            primary = vault.PlannedWrite(page, payload.text, guard=page_guard)
            proposed = {receipt.page: primary, receipt.artifact: binary}
            connector_boundary.require_write(root, receipt.page, content=payload.text.encode("utf-8"), proposed=proposed)
            connector_boundary.require_write(root, receipt.artifact, content=None, proposed=proposed)
            bound = vocabulary_auxiliaries.seal_retained_original(root, primary=primary, original=binary)
            with owner_local_producer(root, OWNER):
                vault.batch_atomic_write(
                    [binary], vault_root=root, required_guards=(page_guard,),
                    post_commit_fanout=False, commit_point=False, _vocabulary_auxiliaries=bound,
                )

    def _verified_duplicate(self, root: Path, receipt: Receipt) -> duplicates.Duplicate:
        duplicate = duplicates.named_duplicate(
            root, receipt.page, sha256=self.original_sha256, lane=duplicates.SOURCES,
            protected=self.protected, keeps_original=True,
        )
        if (
            duplicate is None or duplicate.ref != receipt.ref
            or duplicate.payload.artifact_path != receipt.artifact
        ):
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        return duplicate

    def prepare(self, root: Path, receipt: Receipt) -> None:
        # File publication can be interrupted between the original, Source and receipt.
        # Keep the exact allocation so a retry can prove and settle that outcome.
        store = private_state.store(root, _PREPARED_STORE)
        private_state.write_record(
            store, self.key, receipt.model_dump(), temp_suffix=".part", durable=True, create_only=True,
        )
        private_state.sync_directory(store)

    def _pending_path(self, root: Path) -> Path:
        return private_state.store(root, _PREPARED_STORE) / f"{self.key}.json"

    def finish(self, root: Path) -> None:
        private_state.remove(self._pending_path(root))

    @staticmethod
    def _refresh_payload(root: Path, path: str) -> None:
        from . import due_state, lexstore

        lexstore.record_payload_commit(root, [path])
        due_state.refresh_after_writes(root, [due_state.PageWrite(path)])

    def commit_duplicate(self, root: Path, duplicate: duplicates.Duplicate) -> None:
        from . import source_custody

        # A file import cannot enrol for whole-Source erasure. Refuse a durable
        # pointer into that custody so its owner can still erase every copy.
        custody, _ = source_custody.read_manifest(root, duplicate.ref)
        if custody is not None:
            raise source_capture.SourceCaptureError(f"IMPORT_RECEIPT_UNAVAILABLE: {_UNAVAILABLE}")
        assert duplicate.payload.artifact_path is not None
        source_hash = vault.content_hash(duplicate.payload.text)
        page_guard = vault.PathGuard.capture(
            root, duplicate.path, leaf_policy="content", expected_content_hash=source_hash,
        )
        receipt = self.receipt(duplicate.path, duplicate.ref, duplicate.payload.artifact_path, source_hash)
        write = self.planned_write(root, receipt)
        # The duplicate already passed the caller's byte and release checks.
        # Only the exact private receipt uses service authority.
        with reserved_paths._subsystem_authority_scope(OWNER), owner_local_producer(root, OWNER):
            vault.batch_atomic_write(
                [write], vault_root=root, required_guards=(page_guard,), post_commit_fanout=False,
            )


@dataclass(frozen=True, slots=True)
class ResumableCapture(ImportCapture):
    """A delegated upload's receipt proves one publication, without repair or deduplication."""

    file_witness: Path
    store_root: Path | None

    def prepare(self, root: Path, receipt: Receipt) -> None:
        # The upload cannot retry an ambiguous writer claim. Only this batch's commit can prove the receipt.
        return None

    def file_commit_witness(self, receipt: Receipt) -> vault._BatchFileWitness:
        from .upload_sessions import pending_file_commit_witness
        from .writer_lease import _PostCommitOutcomeUncertain

        path = self.file_witness
        pending = pending_file_commit_witness(path)
        receipt_hash = vault.content_hash(receipt.model_dump_json() + "\n")
        pending_created = False
        proof_linked = False

        def begin() -> None:
            # The session lock covers every file install. Persist the intent before the first one.
            nonlocal pending_created
            with private_state.stage_record(path.parent, path.stem, {"receipt_sha256": receipt_hash},
                                            temp_suffix=".tmp", durable=True) as temporary:
                try:
                    os.link(temporary, pending)
                except OSError as error:
                    if _publication_path_possible(pending):
                        raise _PostCommitOutcomeUncertain() from error
                    raise
                pending_created = True
            private_state.sync_directory(path.parent)

        def seal() -> None:
            nonlocal proof_linked
            if self.store_root is not None:
                # SQLite committed the receipt with the Source. It needs no file proof.
                if pending_created:
                    private_state.remove(pending)
                return
            os.link(pending, path)
            proof_linked = True
            private_state.sync_directory(path.parent)
            # The commit link is durable. A failed retirement leaves the latch blocking recovery
            # until terminal cleanup. It cannot make the writer roll back those proven bytes.
            private_state.remove(pending)

        def retire_rolled_back() -> None:
            if not pending_created:
                return
            if self.store_root is not None and self._store_receipt_possible(self.store_root):
                raise _PostCommitOutcomeUncertain()
            try:
                if proof_linked:
                    path.unlink()
                    private_state.sync_directory(path.parent)
                elif _publication_path_possible(path):
                    raise _PostCommitOutcomeUncertain()
                pending.unlink()
                private_state.sync_directory(path.parent)
            except OSError as error:
                raise _PostCommitOutcomeUncertain() from error

        return vault._BatchFileWitness(begin, seal, retire_rolled_back)

    def _store_receipt_possible(self, root: Path) -> bool:
        from .collection_store.connection import CollectionStoreError
        from .vault_store import read as store_read

        rel = self._path(root).relative_to(root).as_posix()
        try:
            with store_read.snapshot(root, join_request=False) as current:
                return current.page_meta(rel) is not None
        except (OSError, ValueError, sqlite3.Error, CollectionStoreError):
            return True

    def publication_may_have_started(self, root: Path) -> bool:
        """Retain the upload if its exact publication left any evidence or cannot be checked."""
        from .upload_sessions import pending_file_commit_witness

        return any(_publication_path_possible(item) for item in (
            self._path(root), self.file_witness, pending_file_commit_witness(self.file_witness),
        )) or (self.store_root is not None and self._store_receipt_possible(root))

    def committed_upload_receipt(self, root: Path, *, size: int) -> dict | None:
        """Read the exact sealed Source and original under the upload's current principal."""
        from .collection_store.connection import CollectionStoreError
        from .upload_sessions import pending_file_commit_witness

        try:
            if self.store_root is None:
                if _publication_path_possible(pending_file_commit_witness(self.file_witness)):
                    return None
                with self.file_witness.open("rb") as source:
                    witness = json.loads(source.read(_MAX_RECEIPT_BYTES + 1))
            relative = self._path(root).relative_to(root).as_posix()
            with reserved_paths._subsystem_authority_scope(OWNER):
                raw, _ = vault.read_bounded_guarded_bytes(root, relative, limit=_MAX_RECEIPT_BYTES)
            if self.store_root is None and witness != {"receipt_sha256": hashlib.sha256(raw).hexdigest()}:
                return None
            receipt = self._read_receipt(raw)
            self._require_same_input(receipt)
            payload = _released_upload_payload(root, page=receipt.page, ref=receipt.ref,
                                               artifact=receipt.artifact, sha256=self.original_sha256)
            if (
                payload is None or payload.protected != self.protected
                or vault.content_hash(payload.text) != receipt.source_hash
                or duplicates.original_size(root, receipt.page, payload, bound=True) != size
            ):
                return None
        except (OSError, ValueError, CollectionStoreError, reserved_paths.ReservedPathLeafError):
            return None
        return {
            "path": receipt.page, "ref": receipt.ref, "artifact_path": receipt.artifact,
            "hash": self.original_sha256, "hash_algorithm": "sha256", "size": size,
        }


def _publication_path_possible(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def _released_upload_payload(
    root: Path, *, page: str, ref: str, artifact: str, sha256: str,
) -> duplicates.PagePayload | None:
    from .collection_store.connection import CollectionStoreError
    from .governance.egress import _release_level_for_path_only_snapshot, release_level_for
    from .governance.policy import DISCLOSURE_MAX

    try:
        if release_level_for(root, page) != DISCLOSURE_MAX:
            return None
        payload = duplicates.stored_payload(root, page)
        if (
            payload is not None and payload.lane == duplicates.SOURCES and payload.ref == ref
            and payload.artifact_path == artifact and payload.sha256 == sha256
            and not duplicates.binding_unproven(root, page, payload)
            # A historical receipt cannot stand in for a live content-bound artifact grant.
            and _release_level_for_path_only_snapshot(root, artifact, content_fingerprint=None) == DISCLOSURE_MAX
        ):
            return payload
    except (OSError, ValueError, CollectionStoreError, reserved_paths.ReservedPathLeafError):
        return None
    return None


def upload_receipt_releasable(
    root: Path, *, page: object, ref: object, artifact: object, sha256: object, lifecycle_view: ReceiptLifecycle,
) -> bool | None:
    """Decide current receipt release. None means a lifecycle snapshot is pending."""
    from . import document_containers, request_budget
    from .collection_store.connection import CollectionStoreError
    from .governance import egress, lifecycle, policy

    if not (isinstance(page, str) and isinstance(ref, str) and isinstance(artifact, str) and isinstance(sha256, str)):
        return False
    if any(reserved_paths.classify_logical(path).blocked or document_containers.in_payload_tree(path)
           for path in (page, artifact)):
        return False  # Container members need their separate parent proof.
    checkpoint = request_budget.ReadCheckpoint(request_budget.RequestBudget(seconds=_RECEIPT_READ_SECONDS))
    try:
        with request_budget.checking(checkpoint), policy.request_policy_snapshot():
            checkpoint()
            proof = lifecycle_view.snapshot(checkpoint)
            if proof is None:
                return None
            tombstones = proof.tombstones
            if any(lifecycle.is_tombstoned_in(tombstones, value) for value in (page, ref, artifact)):
                return False
            compiled = policy.load(root)
            raw, guard = vault.read_bounded_guarded_bytes(root, page, limit=_MAX_RECEIPT_PAGE_BYTES)
            payload = duplicates.derive_page_payload(page, raw)
            if (payload is None or payload.lane != duplicates.SOURCES or payload.ref != ref
                    or payload.artifact_path != artifact or payload.sha256 != sha256
                    or duplicates.binding_unproven(root, page, payload, page_snapshot=raw)):
                return False
            checkpoint()
            if egress.release_level_for(root, page, record=False, policy=compiled, tombstones=tombstones,
                                        page_snapshot=raw) != policy.DISCLOSURE_MAX:
                return False
            if egress._release_level_for_path_only_snapshot(
                root, artifact, policy=compiled, tombstones=tombstones, resolve_bytes=False,
            ) != policy.DISCLOSURE_MAX:
                return False
            if not lifecycle_view.current(proof, checkpoint):
                return None
            guard.recheck(root)
            checkpoint()
            return True if lifecycle_view.current(proof, checkpoint) else None
    except (request_budget.ReadBudgetExhausted, OSError, ValueError, CollectionStoreError,
            reserved_paths.ReservedPathLeafError, lifecycle.LifecycleError):
        return False


def resumable_capture(root: Path, record: dict) -> ResumableCapture:
    """Bind the exact session, issuing login binding, original and admitted capture fields."""
    from .upload_sessions import file_commit_witness_path
    from .vault_store.mode import store_mode_active

    origin = record["source_handoff"]
    target = record["target"]
    store_root = root if store_mode_active(root) else None
    file_witness = file_commit_witness_path(root, record["id"])
    key = _digest(["source-upload-commit/v1", record["id"], origin["binding"],
                   origin["deployment"], origin["ticket_sha256"]])
    # A file receipt copied into SQLite during a mode transition cannot stand for a store commit.
    fingerprint = _digest([1, key, store_root is None, target, record["length"], record["sha256"]])
    return ResumableCapture(key, fingerprint, record["sha256"], target["raw_protection"], file_witness, store_root)


def receipt_root(root: Path) -> Path:
    return vault.kb_root(root) / "_Sources" / "imports"


def is_receipt_path(root: Path, relative: str) -> bool:
    path = root / relative
    return path.parent == receipt_root(root) and re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is not None


def validate_bound_receipt(root: Path, path: str, body: bytes, *, page: str, source: bytes) -> None:
    """Check that a private receipt names its one proposed Source and original."""
    receipt = Receipt.model_validate_json(body)
    fields = vault.parse_frontmatter(source.decode("utf-8"), strict=True)[0]
    # The registry reserves one JSON filename per lowercase SHA-256 scope key.
    if (
        not is_receipt_path(root, path) or (root / path).stem != receipt.key
        or hashlib.sha256(source).hexdigest() != receipt.source_hash
        or receipt.page != page or not isinstance(fields, dict)
        or receipt.ref != memory_refs.ref_from_markdown(source.decode("utf-8"))
        or receipt.original_sha256 != fields.get("binary_sha256")
        or receipt.artifact != fields.get("evidence_file")
    ):
        raise ValueError("IMPORT_RECEIPT_INVALID: receipt does not match the proposed Source")
