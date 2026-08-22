from __future__ import annotations

import json
from pathlib import Path

from exomem_provisioner import vault_fingerprint


def _write(path: Path, value: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode() if isinstance(value, str) else value)


def test_fingerprint_excludes_only_versioned_disposable_state(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "Knowledge Base/index.md", "# Preserved\n")
    _write(vault / "Knowledge Base/.embeddings.sqlite", b"old-index")
    _write(vault / "logs/runtime.log", "old-log")

    before = vault_fingerprint.canonical_vault_fingerprint(vault)
    _write(vault / "Knowledge Base/.embeddings.sqlite", b"rebuilt-index")
    _write(vault / "logs/runtime.log", "new-log")
    after_disposable = vault_fingerprint.canonical_vault_fingerprint(vault)
    _write(vault / "Knowledge Base/index.md", "# Changed\n")
    after_canonical = vault_fingerprint.canonical_vault_fingerprint(vault)

    assert before == after_disposable
    assert before != after_canonical


def test_fingerprint_command_emits_only_a_bounded_termination_record(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "private tenant title.md", "private tenant body")
    output = tmp_path / "termination.log"

    assert vault_fingerprint.run(vault_root=vault, output_path=output) == 0

    value = json.loads(output.read_text(encoding="utf-8"))
    assert value == {
        "artifact": "exomem-hosted-vault-fingerprint",
        "schemaVersion": 1,
        "sha256": vault_fingerprint.canonical_vault_fingerprint(vault),
    }
    assert "private tenant" not in output.read_text(encoding="utf-8")


def test_fingerprint_fails_closed_on_a_symlink(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (vault / "unsafe.md").symlink_to(outside)
    output = tmp_path / "termination.log"

    assert vault_fingerprint.run(vault_root=vault, output_path=output) == 1
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "artifact": "exomem-hosted-vault-fingerprint",
        "schemaVersion": 1,
        "error": "vault-fingerprint-failed",
    }


def test_fingerprint_cli_accepts_no_runtime_arguments(monkeypatch, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "termination.log"
    monkeypatch.setattr(vault_fingerprint, "VAULT_ROOT", vault)
    monkeypatch.setattr(vault_fingerprint, "TERMINATION_LOG", output)

    assert vault_fingerprint.main([]) == 0
    assert vault_fingerprint.main(["--vault-root", str(vault)]) == 2
