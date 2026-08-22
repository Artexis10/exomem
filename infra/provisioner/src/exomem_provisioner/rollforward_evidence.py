"""Read-only, content-free proof for a completed runtime rollforward."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from .config import ProvisionerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .logging import configure_content_free_logging
from .main import _require_production_database
from .repository import OperationRepository, RollforwardEvidenceSnapshot

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_rollforward_evidence(snapshot: RollforwardEvidenceSnapshot) -> dict[str, Any]:
    """Build the exact public evidence artifact from a redacted snapshot."""

    if not _OPAQUE.fullmatch(snapshot.external_operation_id) or not _OPAQUE.fullmatch(
        snapshot.cell_id
    ):
        raise ValueError("rollforward evidence identity is invalid")
    if any(
        not _SHA256.fullmatch(digest)
        for digest in (
            snapshot.before_vault_sha256,
            snapshot.after_vault_sha256,
            snapshot.evidence_sha256,
        )
    ):
        raise ValueError("rollforward evidence digest is invalid")
    return {
        "artifact": "exomem-hosted-rollforward-evidence",
        "schemaVersion": 1,
        "operationId": snapshot.external_operation_id,
        "cellId": snapshot.cell_id,
        "code": "rollforward_preserved",
        "beforeVaultSha256": snapshot.before_vault_sha256,
        "afterVaultSha256": snapshot.after_vault_sha256,
        "evidenceSha256": snapshot.evidence_sha256,
    }


async def _load(external_operation_id: str) -> dict[str, Any]:
    settings = ProvisionerSettings()  # type: ignore[call-arg]
    _require_production_database(settings)
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )
    try:
        if not await database.ready():
            raise RuntimeError("provisioner database is unavailable")
        return build_rollforward_evidence(
            await repository.load_rollforward_evidence(external_operation_id)
        )
    finally:
        await database.dispose()


def run_rollforward_evidence() -> None:
    arguments = sys.argv[1:]
    if arguments in (["-h"], ["--help"]):
        print("usage: exomem-provisioner-rollforward-evidence OPERATION_ID")
        return
    if len(arguments) != 1 or not _OPAQUE.fullmatch(arguments[0]):
        raise SystemExit(2)
    configure_content_free_logging()
    try:
        artifact = asyncio.run(_load(arguments[0]))
    except Exception:  # noqa: BLE001 - kubectl caller receives one redacted failure
        raise SystemExit(1) from None
    print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
