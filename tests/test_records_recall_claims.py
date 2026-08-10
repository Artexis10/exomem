"""Records must never enter claim extraction or stale claim egress."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from exomem import claims, embeddings, recall_policy
from exomem import find as find_module


def _row(rel: str) -> tuple[str, str, str, np.ndarray, str, str, float]:
    vector = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    vector[0] = 1.0
    return (rel, "Sensitive record\n\nNever recall this.", "checksum", vector, "insight", "active", 1.0)


def _record(vault: Path, name: str = "session.md") -> Path:
    path = vault / "Knowledge Base" / "Records" / "Health" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not parseable as a compiled note\n", encoding="utf-8")
    return path


def test_raw_record_incremental_update_purges_without_parsing_or_feature_gates(
    vault: Path, monkeypatch
) -> None:
    path = _record(vault)
    rel = "Knowledge Base/Records/Health/session.md"
    index = claims.ClaimIndex(vault)
    index.upsert_many([_row(rel)])
    monkeypatch.delenv("EXOMEM_CLAIM_LEVEL", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("raw Record parsed")))

    claims.upsert_claims_after_write(vault, [path])

    assert rel not in index.checksums()


def test_claim_egress_refuses_a_legacy_raw_record_before_sidecar_or_parse(
    vault: Path, monkeypatch
) -> None:
    _record(vault)
    rel = "Knowledge Base/Records/Health/session.md"
    index = claims.ClaimIndex(vault)
    index.upsert_many([_row(rel)])
    monkeypatch.setattr(index, "get_row", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("stale row read")))
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("raw Record parsed")))

    assert claims.claim_text_for_page(vault, rel, index=index) is None


def test_model_free_delete_many_never_creates_an_absent_claim_sidecar(vault: Path) -> None:
    assert not claims.sidecar_path(vault).exists()

    claims.delete_after_remove(vault, ["Knowledge Base/Records/Health/session.md"])

    assert not claims.sidecar_path(vault).exists()


def test_access_transition_refuses_stale_claim_row(vault: Path) -> None:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / "private.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Private\n\n## Claim\n\nDo not expose.\n",
        encoding="utf-8",
    )
    rel = "Knowledge Base/Notes/Insights/private.md"
    index = claims.ClaimIndex(vault)
    index.upsert_many([_row(rel)])
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Notes/Insights/private.md\n", encoding="utf-8"
    )

    assert claims.claim_text_for_page(vault, rel, index=index) is None


def test_incremental_race_does_not_publish_after_policy_changes(
    vault: Path, monkeypatch
) -> None:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / "race.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Race\n\n## Claim\n\nMust remain private.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    def _race_embed(texts, *, is_query=False):
        (vault / "Knowledge Base" / "_access.yaml").write_text(
            "excluded:\n  - Notes/Insights/race.md\n", encoding="utf-8"
        )
        return np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_texts", _race_embed)
    claims.upsert_claims_after_write(vault, [path])

    assert "Knowledge Base/Notes/Insights/race.md" not in claims.ClaimIndex(vault).checksums()


def test_all_claims_reads_only_admitted_source_paths(vault: Path) -> None:
    record = _record(vault)
    note = vault / "Knowledge Base" / "Notes" / "Insights" / "ordinary.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Ordinary\n\n## Claim\n\nSafe claim.\n",
        encoding="utf-8",
    )
    index = claims.ClaimIndex(vault)
    index.replace_all(
        [
            _row("Knowledge Base/Records/Health/session.md"),
            _row("Knowledge Base/Notes/Insights/ordinary.md"),
        ],
        identity=recall_policy.recall_policy_identity(vault),
    )

    metadata, _matrix = index.all_claims()

    assert [row[0] for row in metadata] == ["Knowledge Base/Notes/Insights/ordinary.md"]
    assert record.exists()


def test_full_claim_rebuild_never_parses_a_raw_record(vault: Path, monkeypatch) -> None:
    record = _record(vault)
    note = vault / "Knowledge Base" / "Notes" / "Insights" / "compiled.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Compiled\n\n## Claim\n\nAdmitted claim.\n",
        encoding="utf-8",
    )
    original_get = find_module._CACHE.get

    def _no_record_parse(path, *args, **kwargs):
        if path == record:
            raise AssertionError("raw Record parsed")
        return original_get(path, *args, **kwargs)

    monkeypatch.setattr(find_module._CACHE, "get", _no_record_parse)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query=False: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )

    index = claims.ClaimIndex(vault)
    assert index.rebuild_all() >= 1
    assert index.get_row("Knowledge Base/Notes/Insights/compiled.md") is not None
    assert "Knowledge Base/Records/Health/session.md" not in index.checksums()


def test_full_claim_rebuild_invalidates_complete_sidecar_when_admitted_page_appears(
    vault: Path, monkeypatch
) -> None:
    baseline = vault / "Knowledge Base" / "Notes" / "Insights" / "baseline.md"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Baseline\n\n## Claim\n\nAlready present.\n",
        encoding="utf-8",
    )
    index = claims.ClaimIndex(vault)
    index.replace_all(
        [_row("Knowledge Base/Notes/Insights/baseline.md")],
        identity=recall_policy.recall_policy_identity(vault),
    )
    assert index._recall_identity_current() is True

    def _late_page(texts, *, is_query=False):
        late = vault / "Knowledge Base" / "Notes" / "Insights" / "late.md"
        late.write_text(
            "---\ntype: insight\nstatus: active\n---\n\n# Late\n\n## Claim\n\nMust be included.\n",
            encoding="utf-8",
        )
        return np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_texts", _late_page)

    assert index.rebuild_all() == 0
    assert index._recall_identity_current() is False


def test_incremental_claim_write_falls_back_live_until_rebuild_restores_completeness(
    vault: Path, monkeypatch
) -> None:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / "ordinary-live.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Ordinary\n\n## Claim\n\nFirst claim.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query=False: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    index = claims.ClaimIndex(vault)
    assert index.rebuild_all() >= 1
    assert index._recall_identity_current() is True

    path.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Ordinary\n\n## Claim\n\nSecond claim.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    claims.upsert_claims_after_write(vault, [path])

    assert index._recall_identity_current() is False
    assert "Second claim." in (claims.claim_text_for_page(vault, "Knowledge Base/Notes/Insights/ordinary-live.md") or "")
    assert index.rebuild_all() >= 1
    assert index._recall_identity_current() is True
