from __future__ import annotations

import pytest

from exomem_provisioner.repository import RollforwardEvidenceSnapshot
from exomem_provisioner.rollforward_evidence import build_rollforward_evidence


def test_rollforward_evidence_artifact_is_exact_and_content_free() -> None:
    artifact = build_rollforward_evidence(
        RollforwardEvidenceSnapshot(
            external_operation_id="rollforward-alpha",
            cell_id="cell-alpha",
            before_vault_sha256="a" * 64,
            after_vault_sha256="a" * 64,
            evidence_sha256="b" * 64,
        )
    )

    assert artifact == {
        "artifact": "exomem-hosted-rollforward-evidence",
        "schemaVersion": 1,
        "operationId": "rollforward-alpha",
        "cellId": "cell-alpha",
        "code": "rollforward_preserved",
        "beforeVaultSha256": "a" * 64,
        "afterVaultSha256": "a" * 64,
        "evidenceSha256": "b" * 64,
    }


@pytest.mark.parametrize("field", ["before_vault_sha256", "after_vault_sha256", "evidence_sha256"])
def test_rollforward_evidence_rejects_invalid_digest(field: str) -> None:
    values = {
        "external_operation_id": "rollforward-alpha",
        "cell_id": "cell-alpha",
        "before_vault_sha256": "a" * 64,
        "after_vault_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
    }
    values[field] = "not-a-digest"

    with pytest.raises(ValueError, match="invalid"):
        build_rollforward_evidence(RollforwardEvidenceSnapshot(**values))
