from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def test_provisioner_fingerprint_matches_a_custom_kb_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Memory")
    provisioner = _provisioner_module()
    vault = tmp_path / "vault"
    for relative in (
        "Memory/index.md",
        "Memory/.review-state.json",
        "Memory/.graph-commit-receipts/0123456789abcdef01234567.json",
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
        "batch-workspace": "Projects/.exomem-batch-" + "f" * 32 + "/state.json",
        "held-publication": "Projects/.exomem-held-publish-" + "0" * 32,
    }
    assert set(samples) == {descriptor.id for descriptor in reserved_paths._REGISTRY}
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/index.md", "canonical")
    for descriptor_id, relative in samples.items():
        _write(vault, f"Knowledge Base/{relative}", descriptor_id)

    provisioner = _provisioner_module()
    assert provisioner.canonical_vault_fingerprint(vault) == (
        hosted_portability.canonical_vault_fingerprint(vault)
    )
