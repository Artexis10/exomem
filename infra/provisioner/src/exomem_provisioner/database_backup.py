"""Complete PostgreSQL logical backup and empty-environment restore proof."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .durability_crypto import (
    ChunkedArchiveCipher,
    DatabaseRecoveryEnvelope,
    DataKeyWrapper,
    RecoveryEnvelopeCodec,
    RecoveryIdentity,
)
from .durability_repository import (
    DurabilityRepository,
    RecoveryObjectInput,
    RunKind,
    RunSnapshot,
)
from .durability_store import ProviderObjectHead
from .provider_recovery import ProviderIdentitySigner, ProviderReference

_OPAQUE_DATABASE_IDENTITY = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class DatabaseRecoveryVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


class CommandExecutor(Protocol):
    async def run(self, argv: tuple[str, ...], *, env: dict[str, str]) -> CommandResult: ...


class SubprocessCommandExecutor:
    """Content-free subprocess adapter; stderr is never surfaced or retained."""

    async def run(self, argv: tuple[str, ...], *, env: dict[str, str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="strict"),
        )


@dataclass(frozen=True, slots=True)
class PostgresRecoveryConfig:
    pg_dump: str
    pg_restore: str
    psql: str
    dropdb: str
    createdb: str
    service_file: Path
    password_file: Path
    source_service: str
    maintenance_service: str
    scratch_service: str
    scratch_database: str
    expected_restore_owner: str
    verification_sql: str


@dataclass(frozen=True, slots=True)
class LogicalDumpProof:
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ScratchRestoreProof:
    owner_authenticated: bool
    tenant_resolved: bool
    cell_resolved: bool


class PostgresLogicalBackup:
    """Uses service/pass files so database credentials never enter argv or output."""

    def __init__(
        self,
        config: PostgresRecoveryConfig,
        *,
        executor: CommandExecutor | None = None,
    ) -> None:
        self._config = config
        self._executor = executor or SubprocessCommandExecutor()
        for binary in (
            config.pg_dump,
            config.pg_restore,
            config.psql,
            config.dropdb,
            config.createdb,
        ):
            if not Path(binary).is_absolute():
                raise ValueError("PostgreSQL recovery binaries must use absolute paths")
        for path in (config.service_file, config.password_file):
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ValueError("PostgreSQL service and password files must be mode 0600")
        if not all(
            (
                config.source_service,
                config.maintenance_service,
                config.scratch_service,
                config.scratch_database,
                config.expected_restore_owner,
                config.verification_sql,
            )
        ):
            raise ValueError("PostgreSQL recovery configuration must be complete")

    async def dump_complete_database(self, destination: Path) -> LogicalDumpProof:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        temporary = destination.with_name(f".{destination.name}.partial")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        result = await self._executor.run(
            (
                self._config.pg_dump,
                "--format=custom",
                "--compress=0",
                "--no-owner",
                "--no-privileges",
                "--serializable-deferrable",
                "--file",
                str(temporary),
            ),
            env=self._environment(self._config.source_service),
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            raise DatabaseRecoveryVerificationError("complete logical backup failed")
        os.replace(temporary, destination)
        destination.chmod(0o600)
        return LogicalDumpProof(
            sha256=self._sha256(destination),
            size=destination.stat().st_size,
        )

    async def restore_and_verify_empty_scratch(
        self,
        source: Path,
        *,
        tenant_id: str,
        cell_id: str,
    ) -> ScratchRestoreProof:
        if not source.is_file() or source.stat().st_size == 0:
            raise DatabaseRecoveryVerificationError("logical backup is unavailable")
        if not all(_OPAQUE_DATABASE_IDENTITY.fullmatch(value) for value in (tenant_id, cell_id)):
            raise DatabaseRecoveryVerificationError("scratch restore identity is invalid")
        if not all(
            _OPAQUE_DATABASE_IDENTITY.fullmatch(value)
            for value in (
                self._config.scratch_database,
                self._config.expected_restore_owner,
            )
        ):
            raise DatabaseRecoveryVerificationError("scratch database identity is invalid")
        maintenance_env = self._environment(self._config.maintenance_service)
        dropped = await self._executor.run(
            (
                self._config.dropdb,
                "--if-exists",
                "--force",
                f"--maintenance-db=service={self._config.maintenance_service}",
                self._config.scratch_database,
            ),
            env=maintenance_env,
        )
        if dropped.returncode != 0:
            raise DatabaseRecoveryVerificationError("scratch database reset failed")
        created = await self._executor.run(
            (
                self._config.createdb,
                f"--maintenance-db=service={self._config.maintenance_service}",
                f"--owner={self._config.expected_restore_owner}",
                self._config.scratch_database,
            ),
            env=maintenance_env,
        )
        if created.returncode != 0:
            raise DatabaseRecoveryVerificationError("empty scratch database creation failed")
        restore_env = self._environment(self._config.scratch_service)
        restored = await self._executor.run(
            (
                self._config.pg_restore,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                f"--dbname=service={self._config.scratch_service}",
                str(source),
            ),
            env=restore_env,
        )
        if restored.returncode != 0:
            raise DatabaseRecoveryVerificationError("empty scratch restore failed")
        verify_env = {
            **restore_env,
            "PGOPTIONS": (f"-c app.restore_tenant_id={tenant_id} -c app.restore_cell_id={cell_id}"),
        }
        verified = await self._executor.run(
            (
                self._config.psql,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--field-separator=\t",
                "--set=ON_ERROR_STOP=1",
                f"--dbname=service={self._config.scratch_service}",
                "--command",
                self._config.verification_sql,
            ),
            env=verify_env,
        )
        fields = verified.stdout.strip().split("\t") if verified.returncode == 0 else []
        proof = ScratchRestoreProof(
            owner_authenticated=len(fields) == 3
            and fields[0] == self._config.expected_restore_owner,
            tenant_resolved=len(fields) == 3 and fields[1] == tenant_id,
            cell_resolved=len(fields) == 3 and fields[2] == cell_id,
        )
        if not all((proof.owner_authenticated, proof.tenant_resolved, proof.cell_resolved)):
            raise DatabaseRecoveryVerificationError(
                "scratch restore owner or tenant resolution proof failed"
            )
        return proof

    def _environment(self, service: str) -> dict[str, str]:
        return {
            "PGSERVICE": service,
            "PGSERVICEFILE": str(self._config.service_file),
            "PGPASSFILE": str(self._config.password_file),
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()


class DatabaseUploadStore(Protocol):
    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime,
    ) -> ProviderObjectHead: ...

    async def head(self, key: str) -> ProviderObjectHead | None: ...


class DatabaseRestoreStore(Protocol):
    async def list_page(
        self, *, prefix: str, continuation_token: str | None = None
    ) -> tuple[list[str], str | None]: ...

    async def download_file(self, key: str, destination: Path) -> None: ...

    async def head(self, key: str) -> ProviderObjectHead | None: ...


class DatabaseBackupWorkflow:
    """Restart-safe complete-database dump, scratch restore, encryption and upload."""

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        logical_backup: PostgresLogicalBackup,
        upload_store: DatabaseUploadStore,
        cipher: ChunkedArchiveCipher,
        key_wrapper: DataKeyWrapper,
        recovery_envelope_codec: RecoveryEnvelopeCodec,
        provider_identity_signer: ProviderIdentitySigner,
        provider_bucket: str,
        scratch_root: Path,
        minimum_dump_bytes: int = 1024,
        maximum_dump_bytes: int = 16 * 1024 * 1024 * 1024,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._logical_backup = logical_backup
        self._upload_store = upload_store
        self._cipher = cipher
        self._key_wrapper = key_wrapper
        self._recovery_envelope_codec = recovery_envelope_codec
        self._provider_identity_signer = provider_identity_signer
        self._provider_bucket = provider_bucket
        self._scratch_root = scratch_root.resolve()
        self._minimum_dump_bytes = minimum_dump_bytes
        self._maximum_dump_bytes = maximum_dump_bytes
        self._clock = clock

    async def run(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
        proof_tenant_id: str,
        proof_cell_id: str,
    ) -> dict[str, object]:
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        paths = (
            self._scratch_root / f"{run.id}.database.dump",
            self._scratch_root / f"{run.id}.database.encrypted",
            self._scratch_root / f"{run.id}.database.envelope",
        )
        try:
            return await self._run_once(
                run,
                worker_id=worker_id,
                proof_tenant_id=proof_tenant_id,
                proof_cell_id=proof_cell_id,
            )
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    async def _run_once(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
        proof_tenant_id: str,
        proof_cell_id: str,
    ) -> dict[str, object]:
        if run.identity.kind is not RunKind.DATABASE_BACKUP:
            raise DatabaseRecoveryVerificationError(
                "database workflow requires database-backup run"
            )
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        dump_path = self._scratch_root / f"{run.id}.database.dump"
        encrypted_path = self._scratch_root / f"{run.id}.database.encrypted"
        envelope_path = self._scratch_root / f"{run.id}.database.envelope"
        claim = {
            "claim_token": run.claim_token,
            "claim_generation": run.claim_generation,
        }
        state = dict(run.state)
        checkpoint = run.checkpoint
        if checkpoint == "scratch-verified" and (
            not dump_path.is_file() or dump_path.stat().st_size != int(state["dump_size"])
        ):
            restarted = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="requested",
                state={},
            )
            state = restarted.state
            checkpoint = restarted.checkpoint
        if checkpoint == "requested":
            proof = await self._logical_backup.dump_complete_database(dump_path)
            dump_path.chmod(0o600)
            if not self._minimum_dump_bytes <= proof.size <= self._maximum_dump_bytes:
                raise DatabaseRecoveryVerificationError("logical backup size is implausible")
            await self._logical_backup.restore_and_verify_empty_scratch(
                dump_path,
                tenant_id=proof_tenant_id,
                cell_id=proof_cell_id,
            )
            manifest = {
                "format": "postgresql-custom",
                "scope": "complete-database",
                "includes": ["substrate-application", "exomem-provisioner"],
                "consistency": "pg-dump-serializable-deferrable",
                "scratchRestore": {
                    "emptyEnvironment": True,
                    "ownerAuthenticated": True,
                    "tenantResolved": True,
                    "cellResolved": True,
                },
            }
            manifest_sha256 = await asyncio.to_thread(
                lambda: hashlib.sha256(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            state = {
                "dump_sha256": proof.sha256,
                "dump_size": proof.size,
                "manifest_sha256": manifest_sha256,
            }
            updated = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="scratch-verified",
                state=state,
            )
            checkpoint = updated.checkpoint

        if checkpoint not in {
            "scratch-verified",
            "encrypted",
            "database-object-uploaded",
            "uploaded",
        }:
            raise DatabaseRecoveryVerificationError("database backup checkpoint is invalid")
        if checkpoint == "scratch-verified":
            if not dump_path.is_file() or dump_path.stat().st_size != int(state["dump_size"]):
                raise DatabaseRecoveryVerificationError(
                    "database scratch checkpoint is unavailable"
                )
            if (
                await asyncio.to_thread(PostgresLogicalBackup._sha256, dump_path)
                != state["dump_sha256"]
            ):
                raise DatabaseRecoveryVerificationError(
                    "database scratch checkpoint digest drifted"
                )
        identity = RecoveryIdentity(
            tenant_id=run.identity.tenant_id,
            cell_id=run.identity.cell_id,
            operation_id=run.identity.operation_id,
            fence_generation=run.identity.fence_generation,
            archive_sha256=str(state["dump_sha256"]),
            manifest_sha256=str(state["manifest_sha256"]),
            archive_size=int(state["dump_size"]),
        )
        if checkpoint == "scratch-verified":
            encryption = await asyncio.to_thread(
                self._cipher.encrypt,
                dump_path,
                encrypted_path,
                identity=identity,
                key_wrapper=self._key_wrapper,
            )
            protected_at = self._clock()
            state = {
                **state,
                "wrapped_data_key": encryption.wrapped_data_key,
                "ciphertext_sha256": encryption.ciphertext_sha256,
                "ciphertext_size": encryption.ciphertext_size,
                "protected_at": protected_at.isoformat(),
                "object_lock_until": (protected_at + timedelta(days=7)).isoformat(),
                "expires_at": (protected_at + timedelta(days=30)).isoformat(),
            }
            updated = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="encrypted",
                state=state,
            )
            checkpoint = updated.checkpoint
        if checkpoint == "encrypted":
            if encrypted_path.is_file():
                if encrypted_path.stat().st_size != int(state["ciphertext_size"]):
                    raise DatabaseRecoveryVerificationError(
                        "encrypted database checkpoint size drifted"
                    )
                if (
                    await asyncio.to_thread(PostgresLogicalBackup._sha256, encrypted_path)
                    != state["ciphertext_sha256"]
                ):
                    raise DatabaseRecoveryVerificationError(
                        "encrypted database checkpoint digest drifted"
                    )

        opaque = hashlib.sha256(
            f"{run.identity.operation_id}:{run.identity.fence_generation}".encode()
        ).hexdigest()
        opaque_reference = f"database_{opaque[:26]}"
        key = f"database-backup/{opaque[:2]}/{opaque}.recovery"
        provider_recovery_reference = ProviderReference.b2(
            bucket=self._provider_bucket,
            key=key,
        )
        metadata = {
            **identity.provider_metadata(),
            "run-kind": RunKind.DATABASE_BACKUP.value,
            "database-scope": "complete-database",
            "wrapped-key-reference": opaque_reference,
            "ciphertext-sha256": str(state["ciphertext_sha256"]),
            "ciphertext-size": str(state["ciphertext_size"]),
            "identity-envelope": self._provider_identity_signer.seal(
                provider="b2",
                provider_reference=provider_recovery_reference,
                tenant_id=run.identity.tenant_id,
                cell_id=run.identity.cell_id,
                operation_id=run.identity.operation_id,
                fence_generation=run.identity.fence_generation,
            ),
        }
        metadata_sha256 = hashlib.sha256(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lock_until = datetime.fromisoformat(str(state["object_lock_until"]))
        expires_at = datetime.fromisoformat(str(state["expires_at"]))
        if checkpoint == "encrypted":
            if encrypted_path.is_file():
                head = await self._upload_store.put_file(
                    key,
                    encrypted_path,
                    metadata=metadata,
                    retain_until=lock_until,
                )
            else:
                head = await self._upload_store.head(key)
                if head is None:
                    restarted = await self._repository.checkpoint(
                        run.id,
                        worker_id,
                        **claim,
                        checkpoint="requested",
                        state={},
                    )
                    return await self._run_once(
                        restarted,
                        worker_id=worker_id,
                        proof_tenant_id=proof_tenant_id,
                        proof_cell_id=proof_cell_id,
                    )
            self._verify_remote(
                head,
                key=key,
                expected_size=int(state["ciphertext_size"]),
                metadata=metadata,
                lock_until=lock_until,
            )
            protected_at = datetime.fromisoformat(
                str(state.get("protected_at", lock_until - timedelta(days=7)))
            )
            recovery_envelope = DatabaseRecoveryEnvelope(
                object_key=key,
                object_version_id=head.version_id or "",
                identity=identity,
                wrapped_data_key=str(state["wrapped_data_key"]),
                ciphertext_sha256=str(state["ciphertext_sha256"]),
                ciphertext_size=int(state["ciphertext_size"]),
                metadata_sha256=metadata_sha256,
                created_at=protected_at,
                object_lock_until=lock_until,
            )
            sealed_envelope = self._recovery_envelope_codec.seal(recovery_envelope)
            envelope_key = f"{key}.envelope"
            state = {
                **state,
                "provider_version_id": head.version_id or "",
                "recovery_envelope_key": envelope_key,
                "recovery_envelope_ciphertext": base64.b64encode(sealed_envelope).decode("ascii"),
                "recovery_envelope_sha256": hashlib.sha256(sealed_envelope).hexdigest(),
                "recovery_envelope_size": len(sealed_envelope),
            }
            updated = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="database-object-uploaded",
                state=state,
            )
            checkpoint = updated.checkpoint

        if checkpoint == "database-object-uploaded":
            head = await self._upload_store.head(key)
            if head is None:
                raise DatabaseRecoveryVerificationError("uploaded database backup is unavailable")
            self._verify_remote(
                head,
                key=key,
                expected_size=int(state["ciphertext_size"]),
                metadata=metadata,
                lock_until=lock_until,
            )
            envelope_key = str(state["recovery_envelope_key"])
            try:
                sealed_envelope = base64.b64decode(
                    str(state["recovery_envelope_ciphertext"]),
                    validate=True,
                )
            except (ValueError, TypeError) as error:
                raise DatabaseRecoveryVerificationError(
                    "database recovery envelope checkpoint is invalid"
                ) from error
            if (
                len(sealed_envelope) != int(state["recovery_envelope_size"])
                or hashlib.sha256(sealed_envelope).hexdigest() != state["recovery_envelope_sha256"]
            ):
                raise DatabaseRecoveryVerificationError(
                    "database recovery envelope checkpoint drifted"
                )
            self._write_private_file(envelope_path, sealed_envelope)
            envelope_provider_reference = ProviderReference.b2(
                bucket=self._provider_bucket,
                key=envelope_key,
            )
            envelope_metadata = {
                **identity.provider_metadata(),
                "artifact-kind": "database-recovery-envelope",
                "object-key-sha256": hashlib.sha256(key.encode()).hexdigest(),
                "envelope-sha256": str(state["recovery_envelope_sha256"]),
                "identity-envelope": self._provider_identity_signer.seal(
                    provider="b2",
                    provider_reference=envelope_provider_reference,
                    tenant_id=run.identity.tenant_id,
                    cell_id=run.identity.cell_id,
                    operation_id=run.identity.operation_id,
                    fence_generation=run.identity.fence_generation,
                ),
            }
            envelope_head = await self._upload_store.put_file(
                envelope_key,
                envelope_path,
                metadata=envelope_metadata,
                retain_until=lock_until,
            )
            self._verify_remote(
                envelope_head,
                key=envelope_key,
                expected_size=envelope_path.stat().st_size,
                metadata=envelope_metadata,
                lock_until=lock_until,
            )
            state = {
                **state,
                "recovery_envelope_version_id": envelope_head.version_id or "",
            }
            state.pop("recovery_envelope_ciphertext", None)
            await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="uploaded",
                state=state,
            )
            checkpoint = "uploaded"
        if checkpoint == "uploaded":
            head = await self._upload_store.head(key)
            if head is None:
                raise DatabaseRecoveryVerificationError("uploaded database backup is unavailable")
            self._verify_remote(
                head,
                key=key,
                expected_size=int(state["ciphertext_size"]),
                metadata=metadata,
                lock_until=lock_until,
            )
            envelope_key = str(state["recovery_envelope_key"])
            envelope_head = await self._upload_store.head(envelope_key)
            envelope_provider_reference = ProviderReference.b2(
                bucket=self._provider_bucket,
                key=envelope_key,
            )
            envelope_metadata = {
                **identity.provider_metadata(),
                "artifact-kind": "database-recovery-envelope",
                "object-key-sha256": hashlib.sha256(key.encode()).hexdigest(),
                "envelope-sha256": str(state["recovery_envelope_sha256"]),
                "identity-envelope": self._provider_identity_signer.seal(
                    provider="b2",
                    provider_reference=envelope_provider_reference,
                    tenant_id=run.identity.tenant_id,
                    cell_id=run.identity.cell_id,
                    operation_id=run.identity.operation_id,
                    fence_generation=run.identity.fence_generation,
                ),
            }
            if envelope_head is None:
                raise DatabaseRecoveryVerificationError(
                    "uploaded database recovery envelope is unavailable"
                )
            self._verify_remote(
                envelope_head,
                key=envelope_key,
                expected_size=int(state["recovery_envelope_size"]),
                metadata=envelope_metadata,
                lock_until=lock_until,
            )
        now = self._clock()
        await self._repository.record_verified_object(
            run.id,
            worker_id,
            **claim,
            value=RecoveryObjectInput(
                opaque_reference=opaque_reference,
                provider_reference=ProviderReference.b2(
                    bucket=self._provider_bucket,
                    key=key,
                    version_id=self._required_provider_version(state),
                ),
                wrapped_data_key=str(state["wrapped_data_key"]),
                archive_sha256=identity.archive_sha256,
                manifest_sha256=identity.manifest_sha256,
                archive_size=identity.archive_size,
                ciphertext_sha256=str(state["ciphertext_sha256"]),
                ciphertext_size=int(state["ciphertext_size"]),
                metadata_sha256=metadata_sha256,
                object_lock_until=lock_until,
                expires_at=expires_at,
            ),
            verified_at=now,
        )
        result: dict[str, object] = {
            "databaseBackupRef": opaque_reference,
            "encryptionScheme": "envelope-aes-256-gcm",
            "integrityVerified": True,
            "scratchRestoreVerified": True,
        }
        await self._repository.complete(
            run.id,
            worker_id,
            **claim,
            result=result,
            now=now,
        )
        return result

    @staticmethod
    def _required_provider_version(state: dict[str, object]) -> str:
        version_id = str(state.get("provider_version_id", ""))
        if not version_id:
            raise DatabaseRecoveryVerificationError(
                "uploaded database recovery object has no exact version ID"
            )
        return version_id

    @staticmethod
    def _write_private_file(path: Path, contents: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_remote(
        head: ProviderObjectHead,
        *,
        key: str,
        expected_size: int,
        metadata: dict[str, str],
        lock_until: datetime,
    ) -> None:
        retained_until = head.retain_until
        if retained_until is not None and retained_until.tzinfo is None:
            retained_until = retained_until.replace(tzinfo=UTC)
        if (
            head.key != key
            or head.size != expected_size
            or head.metadata != metadata
            or retained_until is None
            or retained_until < lock_until
        ):
            raise DatabaseRecoveryVerificationError("remote database backup proof differs")


class DatabaseRestoreWorkflow:
    """Recover a complete database using only remote B2 artifacts and root escrow."""

    def __init__(
        self,
        *,
        restore_store: DatabaseRestoreStore,
        logical_backup: PostgresLogicalBackup,
        cipher: ChunkedArchiveCipher,
        key_wrapper: DataKeyWrapper,
        recovery_envelope_codec: RecoveryEnvelopeCodec,
        scratch_root: Path,
    ) -> None:
        self._restore_store = restore_store
        self._logical_backup = logical_backup
        self._cipher = cipher
        self._key_wrapper = key_wrapper
        self._recovery_envelope_codec = recovery_envelope_codec
        self._scratch_root = scratch_root.resolve()

    async def restore_latest(
        self,
        *,
        tenant_id: str,
        cell_id: str,
        proof_tenant_id: str,
        proof_cell_id: str,
    ) -> dict[str, object]:
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        envelope_path = self._scratch_root / "database-recovery.envelope"
        encrypted_path = self._scratch_root / "database-recovery.encrypted"
        dump_path = self._scratch_root / "database-recovery.dump"
        try:
            envelopes: list[DatabaseRecoveryEnvelope] = []
            token: str | None = None
            while True:
                keys, token = await self._restore_store.list_page(
                    prefix="database-backup/", continuation_token=token
                )
                for key in keys:
                    if not key.endswith(".envelope"):
                        continue
                    await self._restore_store.download_file(key, envelope_path)
                    if envelope_path.stat().st_size > 64 * 1024:
                        raise DatabaseRecoveryVerificationError(
                            "database recovery envelope exceeds safety bound"
                        )
                    envelope = self._recovery_envelope_codec.open(envelope_path.read_bytes())
                    if (
                        envelope.identity.tenant_id == tenant_id
                        and envelope.identity.cell_id == cell_id
                    ):
                        envelopes.append(envelope)
                if token is None:
                    break
            if not envelopes:
                raise DatabaseRecoveryVerificationError(
                    "no authenticated remote database recovery envelope exists"
                )
            selected = max(envelopes, key=lambda value: value.created_at)
            head = await self._restore_store.head(selected.object_key)
            retained_until = (
                head.retain_until if head is not None and head.retain_until is not None else None
            )
            if retained_until is not None and retained_until.tzinfo is None:
                retained_until = retained_until.replace(tzinfo=UTC)
            if (
                head is None
                or head.size != selected.ciphertext_size
                or head.version_id != selected.object_version_id
                or retained_until is None
                or retained_until < selected.object_lock_until
            ):
                raise DatabaseRecoveryVerificationError("database recovery object is unavailable")
            metadata_sha256 = hashlib.sha256(
                json.dumps(head.metadata, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if metadata_sha256 != selected.metadata_sha256:
                raise DatabaseRecoveryVerificationError(
                    "database recovery provider metadata did not authenticate"
                )
            await self._restore_store.download_file(selected.object_key, encrypted_path)
            encrypted_path.chmod(0o600)
            if (
                encrypted_path.stat().st_size != selected.ciphertext_size
                or await asyncio.to_thread(PostgresLogicalBackup._sha256, encrypted_path)
                != selected.ciphertext_sha256
            ):
                raise DatabaseRecoveryVerificationError(
                    "database recovery ciphertext proof differs"
                )
            await asyncio.to_thread(
                self._cipher.decrypt,
                encrypted_path,
                dump_path,
                identity=selected.identity,
                wrapped_data_key=selected.wrapped_data_key,
                key_wrapper=self._key_wrapper,
            )
            await self._logical_backup.restore_and_verify_empty_scratch(
                dump_path,
                tenant_id=proof_tenant_id,
                cell_id=proof_cell_id,
            )
            return {
                "databaseRestored": True,
                "operationId": selected.identity.operation_id,
                "fenceGeneration": selected.identity.fence_generation,
                "ownerAuthenticated": True,
                "tenantResolved": True,
                "cellResolved": True,
            }
        finally:
            envelope_path.unlink(missing_ok=True)
            encrypted_path.unlink(missing_ok=True)
            dump_path.unlink(missing_ok=True)


class ScheduledDatabaseBackupWorkflow:
    """Bind the representative identity proof required by the central scheduler."""

    def __init__(
        self,
        workflow: DatabaseBackupWorkflow,
        *,
        proof_tenant_id: str,
        proof_cell_id: str,
    ) -> None:
        if not all(
            _OPAQUE_DATABASE_IDENTITY.fullmatch(value) for value in (proof_tenant_id, proof_cell_id)
        ):
            raise ValueError("scheduled database proof identity is invalid")
        self._workflow = workflow
        self._proof_tenant_id = proof_tenant_id
        self._proof_cell_id = proof_cell_id

    async def run(self, run: RunSnapshot, *, worker_id: str) -> dict[str, object]:
        return await self._workflow.run(
            run,
            worker_id=worker_id,
            proof_tenant_id=self._proof_tenant_id,
            proof_cell_id=self._proof_cell_id,
        )
