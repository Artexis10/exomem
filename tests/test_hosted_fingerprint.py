from __future__ import annotations

import json
from pathlib import Path

from exomem import hosted_fingerprint


def test_hosted_fingerprint_writes_only_a_content_free_termination_record(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "private tenant title.md").write_text("private tenant body", encoding="utf-8")
    output = tmp_path / "termination.log"

    status = hosted_fingerprint.run(vault_root=vault, output_path=output)

    assert status == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "artifact": "exomem-hosted-vault-fingerprint",
        "schemaVersion": 1,
        "sha256": hosted_fingerprint.canonical_vault_fingerprint(vault),
    }
    rendered = output.read_text(encoding="utf-8")
    assert "private tenant title" not in rendered
    assert "private tenant body" not in rendered


def test_hosted_fingerprint_fails_closed_without_exposing_portability_details(
    tmp_path: Path,
) -> None:
    output = tmp_path / "termination.log"

    status = hosted_fingerprint.run(vault_root=tmp_path / "missing", output_path=output)

    assert status == 1
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "artifact": "exomem-hosted-vault-fingerprint",
        "schemaVersion": 1,
        "error": "vault-fingerprint-failed",
    }


def test_hosted_fingerprint_cli_accepts_no_runtime_arguments(monkeypatch, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "termination.log"
    monkeypatch.setattr(hosted_fingerprint, "VAULT_ROOT", vault)
    monkeypatch.setattr(hosted_fingerprint, "TERMINATION_LOG", output)

    assert hosted_fingerprint.main([]) == 0
    assert hosted_fingerprint.main(["--vault-root", str(vault)]) == 2
