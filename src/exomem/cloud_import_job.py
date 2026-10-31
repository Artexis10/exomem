"""Stopped-cell import commands; C5 metadata grants one operation in one cell.

The standalone tar validator stays dependency-free. This module owns age,
protected preservation and the private receipt; only content-free results leave it.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.request import urlopen

from pydantic import BaseModel, ConfigDict, Field

from . import archive_members, import_stage, state_paths, vault
from .cell_init import _ensure_private_directory
from .cloud_cell import CELL_ID_PATTERN
from .cloud_import import DEFAULT_MAX_MEMBERS, ArchiveRefused, reserve_bytes_from_env, unpack_stream
from .governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope

CHUNK_BYTES = 1024 * 1024


class CloudImportRequest(BaseModel):
    """Immutable controller-created Job payload; no OAuth claims or caller owner flag."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    import_id: str
    cell_id: str
    # C5 fixes these operation tokens; they are not user vocabulary.
    kind: Literal["adopt", "restore", "archive_members"]
    ciphertext_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # SHA-256 wire format
    ciphertext_bytes: int = Field(gt=0)
    declared_bytes: int = Field(ge=0)
    declared_files: int = Field(ge=0)


def _id(value: str) -> str:
    if not vault.is_import_id(value):
        raise ValueError("noncanonical import UUID")
    return value


def _private_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_cell(data_root: Path, cell_id: str) -> None:
    from .cloud_cell import cloud_mode_enabled

    # C1 defines a 16-character base32 cell identifier.
    if not re.fullmatch(CELL_ID_PATTERN, cell_id) or not cloud_mode_enabled() or os.environ.get("EXOMEM_CLOUD_CELL_ID") != cell_id:
        raise ArchiveRefused("IMPORT_CELL_MISMATCH", "configuration")


def prepare(data_root: Path, import_id: str, cell_id: str) -> str:
    _check_cell(data_root, cell_id)
    directory = data_root / ".import-keys" / _id(import_id)
    _ensure_private_directory(directory.parent)
    _ensure_private_directory(directory)
    identity = directory / "identity"
    if not identity.exists():
        # A partial key never becomes the identity used on a later prepare retry.
        temporary = directory / "identity.pending"
        temporary.unlink(missing_ok=True)
        try:
            subprocess.run(["age-keygen", "-o", str(temporary)], check=True, capture_output=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, identity)
            os.sync()
        finally:
            temporary.unlink(missing_ok=True)
    result = subprocess.run(["age-keygen", "-y", str(identity)], check=True, capture_output=True)
    return result.stdout.decode("ascii").strip()


def receipt_directory(vault_root: Path) -> Path:
    directory = state_paths.ensure_vault_state_dir(vault_root) / import_stage.CLOUD_RECEIPTS
    _ensure_private_directory(directory)
    return directory


def _check_cipher(stream: BinaryIO, request: CloudImportRequest, sink=None) -> None:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_BYTES):
        size += len(chunk)
        if size > request.ciphertext_bytes:
            raise ArchiveRefused("ARCHIVE_DIGEST_MISMATCH", "bytes")
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    if size != request.ciphertext_bytes or digest.hexdigest() != request.ciphertext_sha256:
        raise ArchiveRefused("ARCHIVE_DIGEST_MISMATCH", "digest")


def _prove_manifest(vault_root: Path, reference: str) -> None:
    manifest = archive_members.read_manifest(vault_root, reference)
    if manifest is None:
        raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "manifest")
    for member in manifest.members:
        path = vault_root / Path(reference).parent / member["blob"]
        if path.is_symlink():
            raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "blob")
        digest = hashlib.sha256()
        size = 0
        with gzip.open(path, "rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                size += len(chunk)
                if size > member["bytes"]:
                    raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "blob")
                digest.update(chunk)
        if size != member["bytes"] or digest.hexdigest() != member["sha256"]:
            raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "blob")


def run_import(
    request: CloudImportRequest, vault_root: Path, source: Callable[[], BinaryIO],
) -> dict:
    """Prove both ciphertext reads and age EOF before the first canonical write."""
    _id(request.import_id)
    _check_cell(vault_root.parent, request.cell_id)
    receipt_path = receipt_directory(vault_root) / f"{request.import_id}.json"
    with request_scope(RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="cloud-import",
                                        issuer_family=f"cloud-cell:{request.cell_id}")):
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["request"] != request.model_dump():
                raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "request")
            if request.kind == "archive_members":
                _prove_manifest(vault_root, receipt["manifest"])
            return receipt["result"]
        with source() as stream:
            _check_cipher(stream, request)
        identity = vault_root.parent / ".import-keys" / request.import_id / "identity"
        staging = vault_root.parent / f".import-{request.import_id}"
        shutil.rmtree(staging, ignore_errors=True)
        errors: list[BaseException] = []
        # An adopt import lists the staged files in its receipt, hashed as they are unpacked.
        members: list[dict] = []

        def record_member(path: str, size: int, digest: str) -> None:
            # `unpack_stream` has set the file's time by now, and the directory rename below keeps the
            # file's inode, size and times. A retry that keeps an earlier stage reads its files again below.
            staged = import_stage.Identity.of((staging / path).stat())
            members.append(import_stage.member(path, size, digest, staged))

        process = subprocess.Popen(
            ["age", "-d", "-i", str(identity)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        def feed() -> None:
            try:
                with source() as stream, process.stdin:
                    _check_cipher(stream, request, process.stdin)
            except BaseException as error:  # noqa: BLE001 -- the caller joins and converts feed errors
                errors.append(error)
                process.stdin.close()

        worker = threading.Thread(target=feed, daemon=True)
        worker.start()
        try:
            try:
                with process.stdout:
                    result = unpack_stream(
                        process.stdout, staging, max_bytes=request.declared_bytes,
                        expect_bytes=request.declared_bytes, expect_files=request.declared_files,
                        max_members=int(os.environ.get("EXOMEM_IMPORT_MAX_MEMBERS", DEFAULT_MAX_MEMBERS)),
                        reserve_bytes=reserve_bytes_from_env(),
                        on_file=record_member if request.kind == "adopt" else None,
                    )
            except ArchiveRefused:
                # age can close its output before it exits, so one poll races a wrong key. Its output is
                # closed here: a decryptor still writing dies of SIGPIPE (a negative code), never 1. The
                # 10 s bound covers an age still waiting on a stalled feed; the refusal then stands.
                try:
                    code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    code = None
                if code is not None and code > 0:
                    raise ArchiveRefused("ARCHIVE_UNDECRYPTABLE", "decrypt") from None
                raise
            # The stopped-cell Job deadline bounds the feed and decryptor before any vault write.
            worker.join()
            if process.wait() != 0:
                raise ArchiveRefused("ARCHIVE_UNDECRYPTABLE", "decrypt")
            if errors:
                raise ArchiveRefused("ARCHIVE_DIGEST_MISMATCH", "consumed")
            public = {"ok": True, "files": result.files, "bytes": result.bytes}
            receipt = {"request": request.model_dump(), "result": public}
            if request.kind == "archive_members":
                from . import cloud_import_envelope

                envelope = cloud_import_envelope.read(staging)
                from .writer_lease import LeaseConfig, LeaseManager

                manager = LeaseManager(LeaseConfig.from_env())
                with (staging / cloud_import_envelope.ARCHIVE_NAME).open("rb") as archive:
                    saved, _created = archive_members.preserve_members(
                        vault_root, guard=lambda: manager.mutation_guard(vault_root),
                        scope=envelope.scope, category=envelope.category, filename=envelope.filename, stream=archive,
                        max_bytes=request.declared_bytes, verified="upload",
                    )
                receipt["manifest"] = saved["path"]
                _prove_manifest(vault_root, receipt["manifest"])
            elif request.kind == "adopt":
                destination = vault_root / import_stage.STAGE_DIR / request.import_id
                _ensure_private_directory(destination.parent)
                if destination.exists():
                    # A crash after rename is recoverable only with identical staged bytes.
                    trees = []
                    for directory in (staging, destination):
                        hashes = {}
                        for path in directory.rglob("*"):
                            if path.is_symlink():
                                raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "symlink")
                            if path.is_file():
                                with path.open("rb") as stream:
                                    hashes[path.relative_to(directory)] = hashlib.file_digest(stream, "sha256").hexdigest()
                        trees.append(hashes)
                    if trees[0] != trees[1]:
                        raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "committed")
                    # The kept files stay and the new staging is deleted, so the manifest names the kept files' identity.
                    members[:] = [
                        import_stage.member(
                            entry["path"], entry["size"], entry["sha256"],
                            import_stage.Identity.of((destination / entry["path"]).stat()))
                        for entry in members
                    ]
                else:
                    os.rename(staging, destination)
                receipt[import_stage.MEMBERS_KEY] = members
            else:
                _restore(vault_root, staging, request)
            os.sync()
            receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _private_json(receipt_path, receipt)
            return public
        finally:
            if process.poll() is None:
                process.kill()
            # Reap the Job child before deleting its private staging directory.
            process.wait()
            worker.join(timeout=35)
            shutil.rmtree(staging, ignore_errors=True)


def _restore(vault_root: Path, staging: Path, request: CloudImportRequest) -> None:
    from .vault import _is_vault

    # Operator tar exports have one fixed vault/ wrapper; mixed archive roots are ambiguous.
    if not _is_vault(staging) and (staging / "vault").exists():
        if staging.is_symlink() or sorted(path.name for path in staging.iterdir()) != ["vault"]:
            raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "mixed_vault_roots")
        staging = staging / "vault"
    if not _is_vault(staging):
        raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "vault")
    _prove_restore_collections(staging)
    prior = vault_root.parent / f".restore-prior-{request.import_id}"
    _ensure_private_directory(prior)
    marker = prior / "request.json"
    if marker.exists():
        if json.loads(marker.read_text()) != request.model_dump():
            raise ArchiveRefused("IMPORT_RECEIPT_INVALID", "restore_request")
    else:
        if (prior / "vault").exists():
            raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "restore_prior")
        _private_json(marker, request.model_dump())
    state = state_paths.vault_state_dir(vault_root)
    if not (prior / "vault").exists():
        os.rename(vault_root, prior / "vault")
    if not (prior / "state").exists() and state.exists():
        os.rename(state, prior / "state")
    # cell-init can prepare the mount directory, but refuses to initialize a vault during this swap.
    if vault_root.is_dir() and not any(vault_root.iterdir()):
        vault_root.rmdir()
    if not vault_root.exists():
        os.rename(staging, vault_root)
    else:
        # A replay after the final rename may acknowledge only this exact restored tree.
        for directory in (staging, vault_root):
            if any(path.is_symlink() for path in directory.rglob("*")):
                raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "restore_symlink")
        def hashes(directory):
            result = {}
            for path in directory.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        result[path.relative_to(directory)] = hashlib.file_digest(stream, "sha256").hexdigest()
            return result
        if hashes(staging) != hashes(vault_root):
            raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "restore_committed")


def _prove_restore_collections(staging: Path) -> None:
    """Prove collection bytes in private staging before moving destination vault or state."""
    from . import structured_collections as collections
    from . import vault_portability
    from .collection_store import chain, connection, ownership, preflight

    try:
        with vault_portability.checked_collection_replica(staging) as verified:
            marker = None if verified is None else ownership.declarations(staging, verified.connection)[0]
            proof = ownership.Proof((), marker)
            rows, manifests, _store_ids = preflight.candidates(staging, ownership_proof=proof)
            if manifests:
                with tempfile.TemporaryDirectory(prefix="exomem-cloud-restore-proof-") as private:
                    with connection.staging_store(Path(private) / "staging.sqlite") as store:
                        rows.extend(preflight.prove_into(
                            staging, manifests, store, Path(private), preflight.import_context()))
        if any(row["status"] == "blocked" for row in rows):
            raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "collection_proof")
    except (connection.CollectionStoreError, chain.StoreChainError, sqlite3.Error,
            collections.CollectionError, vault_portability.PortabilityError, ValueError, OSError):
        raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "collection_proof") from None


def discard(vault_root: Path, import_id: str, *, keep_adoption: bool = False) -> None:
    """Remove transport/adoption material. Committed RAW sources remain governed data."""
    _id(import_id)
    if (vault_root / import_stage.STAGE_DIR).is_symlink() or (vault_root.parent / ".import-keys").is_symlink():
        raise ArchiveRefused("IMPORT_STAGING_UNAVAILABLE", "symlink")
    if not keep_adoption:
        # First, so that a stop between the two steps leaves a stage with no manifest, which nothing consumes.
        _drop_members(vault_root, import_id)
    for path in [
        vault_root.parent / ".import-keys" / import_id,
        vault_root.parent / f".import-{import_id}",
    ] + ([] if keep_adoption else [vault_root / import_stage.STAGE_DIR / import_id]):
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def _drop_members(vault_root: Path, import_id: str) -> None:
    """Remove the member manifest from the import's receipt; the receipt itself still answers a replay."""
    receipt_path = state_paths.vault_state_dir(vault_root) / import_stage.CLOUD_RECEIPTS / f"{import_id}.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(receipt, dict) and receipt.pop(import_stage.MEMBERS_KEY, None) is not None:
        _private_json(receipt_path, receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-import")
    parser.add_argument("operation", choices=("prepare", "import", "discard"))
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    parser.add_argument("--import-id", required=True)
    parser.add_argument("--request")
    parser.add_argument("--url")
    parser.add_argument("--keep-adoption", action="store_true")
    parser.add_argument("--termination-log", type=Path, default=Path("/dev/termination-log"))
    args = parser.parse_args(argv)
    try:
        cell_id = os.environ["EXOMEM_CLOUD_CELL_ID"]
        _check_cell(args.vault.parent, cell_id)
        if args.operation == "prepare":
            result = {"ok": True, "recipient": prepare(args.vault.parent, args.import_id, cell_id)}
        elif args.operation == "discard":
            discard(args.vault, args.import_id, keep_adoption=args.keep_adoption)
            result = {"ok": True}
        else:
            request = CloudImportRequest.model_validate_json(args.request)
            if request.import_id != args.import_id or not args.url.startswith("https://"):
                raise ArchiveRefused("IMPORT_REQUEST_INVALID", "request")
            result = run_import(request, args.vault, lambda: urlopen(args.url, timeout=30))
    except ArchiveRefused as error:
        result = {"ok": False, "error": error.code}
    except Exception:  # noqa: BLE001 -- errors may contain member names or signed URLs
        result = {"ok": False, "error": "IMPORT_FAILED"}
    args.termination_log.write_text(json.dumps(result), encoding="ascii")
    return 0 if result["ok"] else 1
