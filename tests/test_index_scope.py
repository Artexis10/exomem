"""Tests for the opt-in whole-vault semantic index (EXOMEM_INDEX_SCOPE).

Two guarantees, in tension:

- scope="vault" (opt-in): a note OUTSIDE ``Knowledge Base/`` becomes
  semantically searchable — it lands in the sidecar and a natural-language
  ``find`` that shares no literal tokens with it still returns it.
- scope="kb" (DEFAULT): byte-identical to the historical KB-only index — the
  same out-of-KB note is NOT embedded and NOT reachable semantically.

Light tests (scope resolution, walk selection, CLI dry-run) run without the
model. Heavy tests (real recall, incremental build, drift lockstep) import-skip
when sentence-transformers/torch are missing and re-enable embeddings, which the
suite-wide conftest disables by default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from exomem import embeddings, find_corpus, index_paths
from exomem import find as find_module

# A probe whose BODY + TITLE share NO stem with the query below, so the ONLY way
# it can surface is the semantic (vector) lane — never BM25/keyword/auto-widen.
_PROBE_REL = "PersonalVault/probe-out-of-kb-semantic.md"
_PROBE_TITLE = "Afternoon alertness and postprandial dips"
_PROBE_BODY = (
    "Sharp postprandial peaks blunt cognitive sharpness within the next hour. "
    "Steadier glucose curves track with steadier alertness and clearer thinking."
)
_PROBE_QUERY = "blood sugar swings and concentration"


def _write_out_of_kb(vault: Path) -> Path:
    """Drop the semantic probe OUTSIDE Knowledge Base/ (a sibling tree)."""
    p = vault / _PROBE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-06-27\n"
        f"updated: 2026-06-27\ntags: []\n---\n\n# {_PROBE_TITLE}\n\n{_PROBE_BODY}\n",
        encoding="utf-8",
    )
    return p


# ============================================================================
# Light tests — no model load
# ============================================================================


def test_index_scope_defaults_to_kb(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_INDEX_SCOPE", raising=False)
    assert embeddings.index_scope() == "kb"
    assert index_paths.index_scope() == "kb"
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    assert embeddings.index_scope() == "vault"
    assert index_paths.index_scope() == "vault"
    # Case-insensitive, and any unrecognized value falls back to the safe default.
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "VAULT")
    assert embeddings.index_scope() == "vault"
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "everything")
    assert embeddings.index_scope() == "kb"
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "")
    assert embeddings.index_scope() == "kb"


def test_index_paths_public_contract(vault) -> None:
    assert index_paths.sidecar_path(vault).name == ".embeddings.sqlite"
    assert index_paths.clip_sidecar_path(vault).name == ".clip.sqlite"
    assert index_paths.is_embeddable_path(Path("Knowledge Base/Notes/x.md"))
    assert not index_paths.is_embeddable_path(Path("Knowledge Base/log.md"))
    assert not index_paths.is_embeddable_path(Path("Knowledge Base/data.csv"))


def test_index_walk_kb_excludes_out_of_kb(vault, monkeypatch) -> None:
    """Default (kb) walk yields only Knowledge Base/ paths — the regression guard
    at the walk level."""
    _write_out_of_kb(vault)
    monkeypatch.delenv("EXOMEM_INDEX_SCOPE", raising=False)
    walked = {p.resolve().relative_to(vault.resolve()).as_posix()
              for p in index_paths.iter_index_markdown(vault)}
    assert walked, "kb walk should yield the fixture's KB notes"
    assert all(p.startswith("Knowledge Base/") for p in walked), (
        f"kb scope must not walk outside Knowledge Base/; got {sorted(walked)[:5]}"
    )
    assert _PROBE_REL not in walked


def test_index_walk_vault_includes_out_of_kb(vault, monkeypatch) -> None:
    """scope=vault walk covers the whole vault — the KB notes AND the out-of-KB probe."""
    _write_out_of_kb(vault)
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    walked = {p.resolve().relative_to(vault.resolve()).as_posix()
              for p in index_paths.iter_index_markdown(vault)}
    assert _PROBE_REL in walked, "vault scope must reach the out-of-KB probe; got probe missing"
    assert any(p.startswith("Knowledge Base/") for p in walked), (
        "vault scope should still include KB notes"
    )


def test_index_cli_dry_run_reports_scope(vault, monkeypatch, capsys) -> None:
    """`exomem index --dry-run --scope vault` reports the out-of-KB probe as pending
    without loading the model (dry-run stays light even with embeddings disabled)."""
    from exomem.__main__ import main

    _write_out_of_kb(vault)
    find_module.clear_cache()
    rc = main(["index", "--vault", str(vault), "--scope", "vault", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    # The JSON stats line is the last thing printed.
    import json
    stats = json.loads(out.strip().splitlines()[-1])
    assert stats["scope"] == "vault"
    assert stats["dry_run"] is True
    assert stats["files_to_embed"] >= 1
    assert stats["chunks_embedded"] == 0  # dry-run writes nothing


def test_index_cli_requires_vault(monkeypatch) -> None:
    from exomem.__main__ import main

    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)
    assert main(["index"]) == 2


# ============================================================================
# Heavy tests — load bge model. Gated by importorskip + env-var override.
# ============================================================================


pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")


@pytest.fixture
def embeddings_enabled(monkeypatch):
    """Lift the conftest-wide EXOMEM_DISABLE_EMBEDDINGS gate for these tests."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    embeddings._IMPORT_FAILED = False


def _rebuild(vault: Path) -> int:
    """Wipe+rebuild the sidecar for the CURRENT index scope (shared instance)."""
    return embeddings.get_embedding_index(vault).rebuild_all()


def _sidecar_paths(vault: Path) -> set[str]:
    metadata, _ = embeddings.get_embedding_index(vault).all_vectors()
    return {m[0] for m in metadata}


def test_vault_scope_semantic_find_returns_out_of_kb(
    vault, embeddings_enabled, monkeypatch
) -> None:
    """Requirement (a): with scope=vault, a note OUTSIDE Knowledge Base/ is both
    indexed and returned by a natural-language (semantic) find."""
    from exomem import bm25

    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    _write_out_of_kb(vault)
    find_module.clear_cache()

    # Build incrementally via the CLI-driving path (also exercises index_incremental).
    stats = embeddings.index_incremental(vault, log_fn=lambda *_a: None)
    assert stats["scope"] == "vault"
    assert _PROBE_REL in _sidecar_paths(vault), (
        "vault scope must embed the out-of-KB probe into the sidecar"
    )

    bm25.clear_cache()
    find_module.clear_cache()
    find_module._RESOLVER_CACHE.clear()
    hits = find_module.find(vault, query=_PROBE_QUERY, mode="hybrid",
                            scope="vault", limit=10)
    assert any(_PROBE_REL in h.path for h in hits), (
        f"scope=vault hybrid should surface the out-of-KB probe semantically; "
        f"got {[h.path for h in hits]}"
    )


def test_kb_scope_default_excludes_out_of_kb(
    vault, embeddings_enabled, monkeypatch
) -> None:
    """Requirement (b) — the critical regression guard: with the DEFAULT kb scope,
    the out-of-KB note is NOT in the vector index and the VECTOR lane never
    surfaces it.

    Isolated to the vector lane on purpose: BM25 ``scope="kb"`` auto-widen can
    surface out-of-KB files LEXICALLY (pre-existing behavior, independent of the
    vector index), so we probe with ``mode="vector", scope="kb-only"`` — pure
    vector, no auto-widen — which is exactly the surface my change governs.
    """
    from exomem import bm25

    monkeypatch.delenv("EXOMEM_INDEX_SCOPE", raising=False)  # default = kb
    _write_out_of_kb(vault)
    find_module.clear_cache()

    assert _rebuild(vault) > 0
    assert _PROBE_REL not in _sidecar_paths(vault), (
        "default kb scope must NOT embed an out-of-KB note (byte-identical guard)"
    )

    bm25.clear_cache()
    find_module.clear_cache()
    find_module._RESOLVER_CACHE.clear()
    hits = find_module.find(
        vault, query=_PROBE_QUERY, mode="vector", scope="kb-only", limit=10
    )
    assert not any(_PROBE_REL in h.path for h in hits), (
        f"default kb scope's vector lane must not surface the out-of-KB probe; "
        f"got {[h.path for h in hits]}"
    )


def test_index_incremental_is_idempotent_and_prunes(
    vault, embeddings_enabled, monkeypatch
) -> None:
    """Second run embeds nothing (idempotent); deleting a file prunes its rows
    on the next run without a wipe."""
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    probe = _write_out_of_kb(vault)
    find_module.clear_cache()

    first = embeddings.index_incremental(vault, log_fn=lambda *_a: None)
    assert first["files_to_embed"] >= 1
    assert _PROBE_REL in _sidecar_paths(vault)

    # Re-run with no changes → nothing to embed (skips up-to-date rows).
    find_module.clear_cache()
    second = embeddings.index_incremental(vault, log_fn=lambda *_a: None)
    assert second["files_to_embed"] == 0, (
        f"a clean re-run must embed nothing; got {second}"
    )
    assert second["chunks_embedded"] == 0

    # Delete the probe → next run prunes its row-set (no model load needed).
    probe.unlink()
    find_module.clear_cache()
    third = embeddings.index_incremental(vault, log_fn=lambda *_a: None)
    assert third["files_pruned"] >= 1
    assert _PROBE_REL not in _sidecar_paths(vault), (
        "a deleted file's rows must be pruned from the sidecar"
    )


def test_audit_drift_lockstep_follows_index_scope(
    vault, embeddings_enabled, monkeypatch
) -> None:
    """Drift detection must match the index scope: an out-of-KB never-embedded file
    is flagged under vault scope and NOT under kb scope."""
    from exomem import audit as audit_module

    _write_out_of_kb(vault)
    # Seed a sidecar so _check_embedding_drift runs its never-embedded scan
    # (it returns early when the sidecar file doesn't exist).
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "kb")
    find_module.clear_cache()
    assert _rebuild(vault) > 0  # KB-only sidecar; probe intentionally absent

    # kb scope: the out-of-KB probe is out of scope → not flagged.
    kb_flagged = {
        f.path for f in audit_module._check_embedding_drift(vault)
    }
    assert not any(_PROBE_REL in p for p in kb_flagged), (
        f"kb-scope drift must not flag out-of-KB files; got {kb_flagged}"
    )

    # vault scope: same sidecar, but the probe is now in scope and never embedded
    # → flagged as drift so reconcile/index can pick it up.
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    find_module.clear_cache()
    vault_flagged = {
        f.path for f in audit_module._check_embedding_drift(vault)
    }
    assert any(_PROBE_REL in p for p in vault_flagged), (
        f"vault-scope drift must flag the never-embedded out-of-KB probe; "
        f"got {vault_flagged}"
    )



# ============================================================================
# One-path membership: the answer the walk would give, without the walk
# ============================================================================
#
# The write advisory needs the walk's verdict for the handful of sidecar paths
# that reach a draft's top-k, and used to walk the whole corpus per write to
# build a set it then probed a few times (~0.5 s of an advisory at ~3,000
# pages). `walk_md_admits` / `index_markdown_admitter` answer for one path. They
# are only worth having if they agree with the walk on every path, so these
# pin agreement over a tree built to hit every rule the walk applies.


def _adversarial_kb(root: Path) -> tuple[Path, list[Path]]:
    """A KB tree exercising each walk rule. Returns (kb, candidate paths)."""
    kb = root / "Knowledge Base"

    def put(relative: str, text: str = "# Page\n\nBody.\n") -> Path:
        path = kb / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    candidates = [
        put("Notes/plain.md"),
        put("Notes/Deep/Deeper/nested.md"),
        put("Notes/SHOUTING.MD"),
        put("Notes/not-markdown.txt"),
        put("Notes/copy.sync-conflict-20260101-000000.md"),
        put("_trash/trashed.md"),
        put("Notes/_archive/archived.md"),
        put(".exomem-batch-1234/staged.md"),
        put("_Consolidation/reserved.md"),
        put("Notes/trailing./aliased.md"),
    ]
    first = put("Notes/hard-one.md")
    second = kb / "Notes" / "hard-two.md"
    try:
        os.link(first, second)
        candidates += [first, second]
    except OSError:
        candidates.append(first)
    try:
        (kb / "Notes" / "linked-dir").symlink_to(kb / "Notes" / "Deep", target_is_directory=True)
        (kb / "Notes" / "linked.md").symlink_to(kb / "Notes" / "plain.md")
        candidates += [
            kb / "Notes" / "linked-dir" / "Deeper" / "nested.md",
            kb / "Notes" / "linked.md",
        ]
    except OSError:
        pass
    candidates += [
        kb / "Notes" / "missing.md",
        kb / "Notes" / "PLAIN.md",  # a different spelling of a real page
        kb / "Notes" / ".." / "Notes" / "plain.md",
        root / "outside.md",
    ]
    return kb, candidates


def test_walk_md_admits_agrees_with_walk_md_on_every_rule(tmp_path: Path) -> None:
    kb, candidates = _adversarial_kb(tmp_path)
    walked = set(find_corpus.walk_md(kb))
    assert kb / "Notes" / "plain.md" in walked  # the tree is not vacuous
    shared: dict = {}

    for path in candidates:
        expected = path in walked
        assert find_corpus.walk_md_admits(kb, path, {}) is expected, path
        assert find_corpus.walk_md_admits(kb, path, shared) is expected, path


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="directory permission bits do not bind here",
)
def test_walk_md_admits_refuses_what_the_walk_cannot_list(tmp_path: Path) -> None:
    kb = tmp_path / "Knowledge Base"
    hidden = kb / "Notes" / "Locked" / "inside.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("# Inside\n", encoding="utf-8")
    locked = hidden.parent
    locked.chmod(0o100)  # traversable, not listable: lstat works, the walk cannot enumerate
    try:
        assert hidden not in set(find_corpus.walk_md(kb))
        assert find_corpus.walk_md_admits(kb, hidden, {}) is False
    finally:
        locked.chmod(0o755)


def test_walk_md_admits_never_lists_a_reserved_tree(tmp_path: Path, monkeypatch) -> None:
    kb = tmp_path / "Knowledge Base"
    private = kb / "_Consolidation" / "private.md"
    private.parent.mkdir(parents=True)
    private.write_text("# Private\n", encoding="utf-8")
    real_listdir = os.listdir

    def guarded(path="."):
        if Path(path) == private.parent:
            pytest.fail("reserved tree reached child enumeration")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", guarded)

    assert find_corpus.walk_md_admits(kb, private, {}) is False


def test_index_markdown_admitter_agrees_with_the_index_walk(vault: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_INDEX_SCOPE", raising=False)
    _write_out_of_kb(vault)
    _kb, _candidates = _adversarial_kb(vault)
    records = vault / "Knowledge Base" / "Records" / "Health" / "items"
    records.mkdir(parents=True)
    (records / "raw.md").write_text("raw record", encoding="utf-8")
    walked = {
        index_paths.rel_to_vault(vault, path)
        for path in index_paths.iter_index_markdown(vault)
    }
    every_markdown = {
        index_paths.rel_to_vault(vault, Path(directory) / name)
        for directory, _dirs, names in os.walk(vault)
        for name in names
        if name.lower().endswith(".md")
    }
    probes = every_markdown | {
        "Knowledge Base/Notes/missing.md",
        "Knowledge Base//Notes/plain.md",
        "Knowledge Base/Notes/../Notes/plain.md",
        "/Knowledge Base/Notes/plain.md",
        "Knowledge Base\\Notes\\plain.md",
    }
    admits = index_paths.index_markdown_admitter(vault)
    assert admits is not None
    assert "Knowledge Base/Notes/plain.md" in walked

    disagreements = sorted(probe for probe in probes if admits(probe) != (probe in walked))

    assert disagreements == []


def test_index_markdown_admitter_defers_to_the_walk_in_vault_scope(vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    assert index_paths.index_markdown_admitter(vault) is None
