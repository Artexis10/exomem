from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from exomem import hosted_portability, reserved_paths

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/provisioner/src/exomem_provisioner/vault_fingerprint.py"


def _provisioner_module():
    spec = importlib.util.spec_from_file_location("exomem_provisioner_vault_fingerprint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_provisioner_fingerprint_matches_the_runtime_classification_contract(
    tmp_path: Path,
) -> None:
    provisioner = _provisioner_module()
    vault = tmp_path / "vault"
    for relative in (
        "Knowledge Base/index.md",
        "Knowledge Base/.review-state.json",
        "Knowledge Base/.graph-commit-receipts/0123456789abcdef01234567.json",
        ".exomem/schema/SKILL.md",
        "Media/original.png",
        "logs/runtime.log",
        "secrets/oauth-token.json",
        "tmp/incomplete.partial",
        "models/voice.bin",
        "Knowledge Base/.embeddings.sqlite",
        "hosted-init-operations/operation.json",
        ".unknown/cache.bin",
    ):
        _write(vault, relative, relative)

    assert provisioner.canonical_vault_fingerprint(vault) == (
        hosted_portability.canonical_vault_fingerprint(vault)
    )


@pytest.mark.parametrize("kb_dir", ["Memory", ".kb", "models"])
def test_provisioner_fingerprint_matches_a_custom_kb_directory(
    tmp_path: Path, monkeypatch, kb_dir: str,
) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", kb_dir)
    provisioner = _provisioner_module()
    vault = tmp_path / "vault"
    for relative in (
        f"{kb_dir}/index.md",
        f"{kb_dir}/.review-state.json",
        f"{kb_dir}/.graph-commit-receipts/0123456789abcdef01234567.json",
        f"{kb_dir}/_Collections/mode.json",
        f"{kb_dir}/_Collections/collections.sqlite",
        f"{kb_dir}/_Collections/candidate.sqlite",
        "Knowledge Base/.review-state.json",
    ):
        _write(vault, relative, relative)

    assert provisioner.canonical_vault_fingerprint(vault) == (
        hosted_portability.canonical_vault_fingerprint(vault)
    )


def test_provisioner_classifier_covers_every_reserved_descriptor_family(tmp_path: Path) -> None:
    samples = {
        "governance-tree": "_governance/policy.json",
        "consolidation-tree": "_consolidation/state.json",
        "governance-store": ".governance.sqlite",
        "embeddings-store": ".embeddings.sqlite",
        "clip-store": ".clip.sqlite",
        "lexical-store": ".lexical.sqlite",
        "graph-store": ".graph.sqlite",
        "claims-store": ".claims.sqlite",
        "references-store": ".references.sqlite",
        "refs-store": ".refs.sqlite",
        "freshness-store": ".freshness.sqlite",
        "deferred-index-store": ".deferred-index.json",
        "media-jobs-store": ".media-jobs.json",
        "idempotency-store": ".idempotency.jsonl",
        "voice-profile-store": ".voice_profiles.json",
        "graph-handoff": ".graph-sync.json",
        "graph-coordination": ".graph-coordination/lock",
        "graph-receipts": ".graph-commit-receipts/private.json",
        "review-state": ".review-state.json",
        "due-state": ".due-state.json",
        "lexical-rebuild": ".lexical.sqlite.rebuild-" + "a" * 32 + ".tmp",
        "lexical-quarantine": ".lexical.sqlite.quarantine-" + "b" * 32,
        "graph-rebuild": ".graph-rebuild-" + "c" * 64 + "-" + "d" * 24 + ".sqlite",
        "graph-reset": ".graph-reset-" + "e" * 24 + "/state.json",
        "authorization-projections": ".authorization-projections/state.json",
        "collection-store": "collections.sqlite",
        "collection-replica": "_Collections/candidate.sqlite",
        "batch-workspace": "Projects/.exomem-batch-" + "f" * 32 + "/state.json",
        "held-publication": "Projects/.exomem-held-publish-" + "0" * 32,
        "collection-publication": "Records/.exomem-collection-aside-" + "1" * 32 + "-0",
        "collection-snapshot": "_Collections/.exomem-collection-snapshot-" + "2" * 32 + ".sqlite",
        "collection-audit-spool": "Records/.exomem-collection-audit-" + "3" * 32 + ".sqlite",
        "connector-boundary": ".connector-boundary.json",
    }
    assert set(samples) == {descriptor.id for descriptor in reserved_paths._REGISTRY}
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/index.md", "canonical")
    for descriptor_id, relative in samples.items():
        _write(vault, f"Knowledge Base/{relative}", descriptor_id)
    for relative in (
        "_Collections/mode.json",
        "_Collections/collections.sqlite",
        "_Collections/collections.sqlite-wal",
        "Records/.exomem-collection-stage-" + "4" * 32,
        "Records/.exomem-collection-snapshot-" + "5" * 32 + ".sqlite-journal",
        "Records/.exomem-collection-audit-" + "6" * 32 + ".sqlite-shm",
    ):
        _write(vault, f"Knowledge Base/{relative}", relative)

    provisioner = _provisioner_module()
    assert provisioner.canonical_vault_fingerprint(vault) == (
        hosted_portability.canonical_vault_fingerprint(vault)
    )
