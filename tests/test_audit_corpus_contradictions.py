"""corpus_contradictions audit: corpus-wide sweep for near-but-not-dup conclusion pairs.

A measurement-only review queue over the existing vector sidecar — it surfaces
deduped, unordered pairs of ACTIVE read-write COMPILED conclusions whose max
chunk-cosine lands in the band [floor, dup_threshold); never auto-acts. Gated by
EXOMEM_DISABLE_EMBEDDINGS (the suite default). The band edges are tunable via
EXOMEM_CONTRADICTION_FLOOR / EXOMEM_DUP_THRESHOLD, so the semantic tests place a
near-identical pair in or out of the band deterministically (identical heading+body
→ cosine 1.0, so a ceiling above 1.0 keeps the pair in band; a ceiling below it makes
the pair a near-dup that the band excludes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import find as find_module


# Distinctive nonsense body so the planted pair doesn't collide with fixture notes.
_SHARED_BODY = "\n\n".join(
    f"Zylo narwhal quokka substrate measure-not-judge clause number {i}" for i in range(6)
)


def _seed(
    vault: Path,
    rel: str,
    *,
    type_: str = "insight",
    status: str = "active",
    heading: str = "Shared Claim",
    body: str = _SHARED_BODY,
) -> str:
    """Write a minimal compiled page at KB-relative `rel`. Returns the sidecar-form
    key (vault-relative WITH 'Knowledge Base/' prefix and .md)."""
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {type_}\nstatus: {status}\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {heading}\n\n{body}\n",
        encoding="utf-8",
    )
    return f"Knowledge Base/{rel}"


def _cc_findings(vault: Path):
    return audit_module.audit(vault, categories=["corpus_contradictions"]).findings


def _pairs(findings) -> set[tuple[str, str]]:
    return {tuple(sorted(f.paths)) for f in findings if f.paths}


# ---------------- registration / gating (torch-free) ----------------


def test_corpus_contradictions_registered() -> None:
    assert "corpus_contradictions" in audit_module.ALL_CATEGORIES


def test_unknown_category_still_rejected(vault: Path) -> None:
    with pytest.raises(ValueError):
        audit_module.audit(vault, categories=["does_not_exist"])


def test_noop_when_embeddings_disabled(vault: Path) -> None:
    # Suite-wide EXOMEM_DISABLE_EMBEDDINGS is set → the sweep short-circuits to []
    # without loading torch or touching a sidecar, even with near-identical pages.
    _seed(vault, "Notes/Insights/cc-a.md")
    _seed(vault, "Notes/Insights/cc-b.md")
    find_module.clear_cache()
    assert _cc_findings(vault) == []


# ---------------- semantic sweep (model-loading) ----------------

pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")

from exomem import embeddings  # noqa: E402


@pytest.fixture
def embeddings_enabled(monkeypatch):
    """Lift the conftest-wide EXOMEM_DISABLE_EMBEDDINGS gate for these tests."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    embeddings._IMPORT_FAILED = False


def _build(vault: Path) -> None:
    find_module.clear_cache()
    embeddings.EmbeddingIndex(vault).rebuild_all()


def test_flags_deduped_pair_in_band(vault: Path, embeddings_enabled, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.5")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "1.01")  # ceiling > 1.0 → near-identical lands in band
    a = _seed(vault, "Notes/Insights/cc-a.md")
    b = _seed(vault, "Notes/Insights/cc-b.md")
    _build(vault)

    findings = _cc_findings(vault)
    mine = [f for f in findings if set(f.paths or []) == {a, b}]
    assert len(mine) == 1, [f.as_dict() for f in findings]  # deduped: one pair, not A→B AND B→A
    f = mine[0]
    assert f.category == "corpus_contradictions"
    assert f.severity == "info"
    assert f.path == min(a, b)            # canonical: smaller rel_path
    assert f.paths == sorted([a, b])
    assert f.meta["cosine"] >= 0.5
    assert "review" in (f.proposed_fix or "").lower()
    assert "never auto" in (f.proposed_fix or "").lower()


def test_above_ceiling_dup_excluded(vault: Path, embeddings_enabled, monkeypatch) -> None:
    # cosine ~1.0 >= ceiling 0.9 → a near-duplicate, NOT a contradiction: excluded.
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.5")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.9")
    a = _seed(vault, "Notes/Insights/cc-dup-a.md")
    b = _seed(vault, "Notes/Insights/cc-dup-b.md")
    _build(vault)
    assert not [f for f in _cc_findings(vault) if set(f.paths or []) == {a, b}]


def test_below_floor_excluded(vault: Path, embeddings_enabled, monkeypatch) -> None:
    # Unrelated bodies → cosine well below floor 0.999 → not in band: excluded.
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.999")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "1.01")
    a = _seed(
        vault, "Notes/Insights/cc-far-a.md",
        heading="Alpha", body="Photosynthesis converts sunlight into sugars inside chloroplasts.",
    )
    b = _seed(
        vault, "Notes/Insights/cc-far-b.md",
        heading="Beta", body="The quarterly invoice was paid by wire transfer last Tuesday.",
    )
    _build(vault)
    assert not [f for f in _cc_findings(vault) if set(f.paths or []) == {a, b}]


def test_inverted_band_disabled(vault: Path, embeddings_enabled, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.95")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.90")  # floor >= ceiling → band disabled
    _seed(vault, "Notes/Insights/cc-inv-a.md")
    _seed(vault, "Notes/Insights/cc-inv-b.md")
    _build(vault)
    assert _cc_findings(vault) == []


def test_excludes_non_active_compiled_rw_endpoints(
    vault: Path, embeddings_enabled, monkeypatch
) -> None:
    # Four near-identical pages, but only ONE is an active read-write compiled
    # conclusion. The superseded / raw-source / readonly twins are never eligible
    # endpoints, so no surfaced pair includes any of them.
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.5")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "1.01")
    _seed(vault, "Notes/Insights/cc-active.md")
    superseded = _seed(vault, "Notes/Insights/cc-sup.md", status="superseded")
    source = _seed(vault, "Sources/Other/cc-src.md", type_="source")
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Products\nexcluded: []\n", encoding="utf-8"
    )
    readonly = _seed(vault, "Products/cc-ro.md")
    _build(vault)

    pairs = _pairs(_cc_findings(vault))
    for excluded in (superseded, source, readonly):
        assert not any(excluded in pair for pair in pairs), (excluded, pairs)
