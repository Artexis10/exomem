"""Add one durable receipt for an authenticated importer's exact Source upload."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from . import duplicates, memory_refs, private_state, reserved_paths, source_capture, vault
from .governance.principal import effective_principal, owner_local_producer

OWNER = "source_imports"
_MAX_ID_CHARS = 256
_MAX_RECEIPT_BYTES = 8192
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
