"""Case-insensitive-filesystem path identity: guard, census fold semantics,
canonicalization helper, and the casefold probe. Platform-free — exercises
both EXOMEM_CASEFOLD_PATHS=1 and =0 explicitly rather than relying on the
ambient filesystem, so Linux CI covers the fold branch too.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import memory_schema, relation_registry, semantic_contract, vault

_ID_A = "00000000-0000-0000-0000-000000000001"
_ID_B = "00000000-0000-0000-0000-000000000002"


def _source(*, exomem_id: str | None = None) -> str:
    fields = ["type: insight", "status: active"]
    if exomem_id is not None:
        fields.append(f"exomem_id: {exomem_id}")
    return "---\n" + "\n".join(fields) + "\n---\n\nBody.\n"


def _state(tmp_path: Path, rel_path: str, source: str) -> semantic_contract.SemanticPageState:
    return semantic_contract.build_page_state(
        tmp_path,
        rel_path,
        source,
        relation_registry=relation_registry.core_registry(),
        review_fingerprint="fingerprint",
    )


def _corpus(
    tmp_path: Path,
    *states: semantic_contract.SemanticPageState,
    identity_census: semantic_contract.StableIdentityCensus | None = None,
) -> semantic_contract.SemanticCorpusContext:
    return semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        states,
        registry=relation_registry.core_registry(),
        identity_census=identity_census
        if identity_census is not None
        else semantic_contract.StableIdentityCensus(
            tuple(
                semantic_contract.StableIdentityEntry(
                    state.path,
                    state.identity if state.identity_kind == "exomem_id" else None,
                )
                for state in states
            )
        ),
    )


def _evaluate(
    *,
    after: semantic_contract.SemanticPageState,
    before_corpus: semantic_contract.SemanticCorpusContext,
    after_corpus: semantic_contract.SemanticCorpusContext,
    operation: str = "edit",
) -> semantic_contract.SemanticContractResult:
    empty = memory_schema.ResolvedMemoryContracts(
        validation="strict",
        matched_contracts=(),
        constraints=(),
        conflicts=(),
    )
    return semantic_contract.evaluate(
        before=None,
        after=after,
        operation=operation,
        mode="precommit",
        before_contracts=empty,
        after_contracts=empty,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        before_review=None,
        after_review=None,
        grandfathered=False,
    )


def _filesystem_casefolds(directory: Path) -> bool:
    """Whether `directory`'s volume really opens one file under two spellings."""
    probe = directory / "_exomem_case_probe.tmp"
    probe.write_text("probe", encoding="utf-8")
    try:
        swapped = directory / "_EXOMEM_CASE_PROBE.TMP"
        return swapped.is_file() and os.path.samefile(probe, swapped)
    finally:
        probe.unlink()


@pytest.fixture(autouse=True)
def _clear_casefold_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EXOMEM_CASEFOLD_PATHS", raising=False)
    vault.reset_casefold_probe_cache()
    yield
    vault.reset_casefold_probe_cache()


# ---------------- guard policy: incident shape ----------------


def test_guard_ignores_case_variant_owner_when_folds_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POLLY-vs-Polly incident: same physical file, caller-cased path."""
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "1")
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Research/Polly/terms.md",
        _source(exomem_id=_ID_A),
    )
    census = semantic_contract.StableIdentityCensus(
        (
            semantic_contract.StableIdentityEntry(
                "Knowledge Base/Notes/Research/POLLY/terms.md", _ID_A
            ),
        )
    )
    corpus = _corpus(tmp_path, page, identity_census=census)

    result = _evaluate(after=page, before_corpus=corpus, after_corpus=corpus, operation="edit")

    assert "SEMANTIC_IDENTITY_DUPLICATE" not in {
        finding.code for finding in result.blocking_findings
    }


def test_guard_blocks_case_variant_owner_when_folds_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape as above, but with folding off: today's byte-for-byte behavior."""
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "0")
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Research/Polly/terms.md",
        _source(exomem_id=_ID_A),
    )
    census = semantic_contract.StableIdentityCensus(
        (
            semantic_contract.StableIdentityEntry(
                "Knowledge Base/Notes/Research/POLLY/terms.md", _ID_A
            ),
        )
    )
    corpus = _corpus(tmp_path, page, identity_census=census)

    result = _evaluate(after=page, before_corpus=corpus, after_corpus=corpus, operation="edit")

    assert "SEMANTIC_IDENTITY_DUPLICATE" in {
        finding.code for finding in result.blocking_findings
    }


@pytest.mark.parametrize("folds", ["0", "1"])
def test_guard_still_blocks_genuine_duplicate_regardless_of_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folds: str
) -> None:
    """Two real, distinctly-named files sharing an identity always block."""
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", folds)
    page = _state(tmp_path, "Knowledge Base/Notes/Insights/page.md", _source(exomem_id=_ID_A))
    duplicate = _state(
        tmp_path, "Knowledge Base/Notes/Insights/duplicate.md", _source(exomem_id=_ID_A)
    )
    corpus = _corpus(tmp_path, page, duplicate)

    result = _evaluate(
        after=page, before_corpus=corpus, after_corpus=corpus, operation="recover"
    )

    assert "SEMANTIC_IDENTITY_DUPLICATE" in {
        finding.code for finding in result.blocking_findings
    }


def test_identity_duplicate_detail_names_owner_paths(tmp_path: Path) -> None:
    page = _state(tmp_path, "Knowledge Base/Notes/Insights/page.md", _source(exomem_id=_ID_A))
    duplicate = _state(
        tmp_path, "Knowledge Base/Notes/Insights/duplicate.md", _source(exomem_id=_ID_A)
    )
    corpus = _corpus(tmp_path, page, duplicate)

    result = _evaluate(
        after=page, before_corpus=corpus, after_corpus=corpus, operation="recover"
    )

    finding = next(
        f for f in result.blocking_findings if f.code == "SEMANTIC_IDENTITY_DUPLICATE"
    )
    assert page.path in finding.detail
    assert duplicate.path in finding.detail


# ---------------- StableIdentityCensus.with_page fold semantics ----------------


def test_with_page_drops_case_variant_entries_only_when_folding(tmp_path: Path) -> None:
    old = semantic_contract.StableIdentityEntry(
        "Knowledge Base/Notes/Research/POLLY/terms.md", _ID_A
    )
    census = semantic_contract.StableIdentityCensus((old,))
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Research/Polly/terms.md",
        _source(exomem_id=_ID_A),
    )

    folded = census.with_page(page, casefold_paths=True)
    assert folded.paths_by_identity[_ID_A] == (page.path,)

    unfolded_default = census.with_page(page)
    assert set(unfolded_default.paths_by_identity[_ID_A]) == {old.path, page.path}

    unfolded_explicit = census.with_page(page, casefold_paths=False)
    assert set(unfolded_explicit.paths_by_identity[_ID_A]) == {old.path, page.path}


def test_with_page_exact_duplicate_path_replaced_regardless_of_folding(
    tmp_path: Path,
) -> None:
    old = semantic_contract.StableIdentityEntry("Knowledge Base/Notes/Insights/page.md", _ID_A)
    census = semantic_contract.StableIdentityCensus((old,))
    page = _state(tmp_path, "Knowledge Base/Notes/Insights/page.md", _source(exomem_id=_ID_B))

    folded = census.with_page(page, casefold_paths=True)
    assert folded.paths_by_identity == {_ID_B: (page.path,)}


# ---------------- vault_casefolds probe: cache + override ----------------


def test_vault_casefolds_env_override_short_circuits_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(vault, "_probe_casefolds", lambda root: calls.append(root) or True)

    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "1")
    assert vault.vault_casefolds(tmp_path) is True
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "0")
    assert vault.vault_casefolds(tmp_path) is False
    assert calls == []


def test_vault_casefolds_probe_is_cached_per_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    original = vault._probe_casefolds

    def counting(root: Path) -> bool:
        calls.append(root)
        return original(root)

    monkeypatch.setattr(vault, "_probe_casefolds", counting)

    vault.vault_casefolds(tmp_path)
    vault.vault_casefolds(tmp_path)
    assert len(calls) == 1

    vault.reset_casefold_probe_cache()
    vault.vault_casefolds(tmp_path)
    assert len(calls) == 2


# ---------------- canonical_vault_rel: fallback + KB re-spell ----------------


def test_canonical_vault_rel_fails_open_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_resolve(self: Path, *args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "resolve", raising_resolve)

    rel = vault.canonical_vault_rel(tmp_path, "Knowledge Base/Notes/Polly/x.md")

    assert rel == "Knowledge Base/Notes/Polly/x.md"


def test_canonical_vault_rel_fails_open_on_value_error(tmp_path: Path) -> None:
    rel = vault.canonical_vault_rel(tmp_path, "../outside.md")

    assert rel == "../outside.md"


def test_canonical_vault_rel_respells_kb_segment_via_monkeypatched_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path

    def fake_resolve(self: Path, *args, **kwargs):
        if self == root:
            return root
        return root / "knowledge base" / "Notes" / "x.md"

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    rel = vault.canonical_vault_rel(root, "Knowledge Base/Notes/x.md")

    assert rel == "Knowledge Base/Notes/x.md"


def test_canonical_vault_rel_is_identity_transform_for_already_canonical_path(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "Knowledge Base" / "Notes"
    kb.mkdir(parents=True)
    (kb / "x.md").write_text("body", encoding="utf-8")

    rel = vault.canonical_vault_rel(tmp_path, "Knowledge Base/Notes/x.md")

    assert rel == "Knowledge Base/Notes/x.md"


def test_canonical_vault_rel_respells_against_real_on_disk_directories(
    tmp_path: Path,
) -> None:
    """Prove the re-spell, not just that it is harmless.

    The two tests above assert output == input, so they would still pass if
    `canonical_vault_rel` were `return rel`; they are kept as fail-open guards.
    Here the caller's casing matches no directory on disk, so only a real
    re-spell produces the expected answer. Meaningful only where the volume
    actually folds — on a case-sensitive one `KNOWLEDGE BASE/notes/` is simply
    a different, nonexistent path — hence the gate.
    """
    if not _filesystem_casefolds(tmp_path):
        pytest.skip("the volume backing tmp_path is case-sensitive — nothing to re-spell")
    (tmp_path / "Knowledge Base" / "Notes").mkdir(parents=True)

    rel = vault.canonical_vault_rel(tmp_path, "KNOWLEDGE BASE/notes/x.md")

    # Both existing directories come back in their on-disk spelling; the
    # not-yet-existing leaf is preserved verbatim.
    assert rel == "Knowledge Base/Notes/x.md"


# ------------- canonical_vault_rel: the casing-ONLY security invariant -------------
#
# `Path.resolve()` re-spells to real on-disk casing, but it also FOLLOWS
# SYMLINKS (and expands junctions / 8.3 short names, and collapses `..`).
# `hosted_transfer_routes._open_bounded_vault_file` re-opens the returned
# rel-form component by component under `O_NOFOLLOW` precisely so that a
# symlink raises ELOOP -> INVALID_PATH -> HTTP 400. If resolution is allowed to
# hand back the link's *target*, the guard is shown a real file and serves it:
# the symlink is laundered past the check that exists to reject it. So the
# canonical form may be adopted only when the sole difference is casing.


def _symlinks_available(directory: Path) -> bool:
    """Whether this runner can actually create a symlink (Windows: privilege)."""
    target = directory / "_exomem_symlink_probe_target.tmp"
    link = directory / "_exomem_symlink_probe_link.tmp"
    target.write_text("probe", encoding="utf-8")
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError):
        return False
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    return True


def _seed_symlinked_note(root: Path, *, link: str, target: str) -> None:
    notes = root / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / target).write_text("SECRET", encoding="utf-8")
    (notes / link).symlink_to(target)


def test_is_casing_only_rewrite_accepts_a_pure_recasing() -> None:
    assert vault.is_casing_only_rewrite(
        "Knowledge Base/Notes/Research/POLLY/terms.md",
        "Knowledge Base/Notes/Research/Polly/terms.md",
    )
    assert vault.is_casing_only_rewrite("Knowledge Base/Notes/x.md", "Knowledge Base/Notes/x.md")


def test_is_casing_only_rewrite_rejects_a_different_file() -> None:
    # A symlink swapped for its target: same directory, different name.
    assert not vault.is_casing_only_rewrite(
        "Knowledge Base/Notes/shared.md", "Knowledge Base/Notes/link.md"
    )
    # A `..` collapse is not a re-casing either.
    assert not vault.is_casing_only_rewrite(
        "Knowledge Base/Notes/x.md", "Knowledge Base/../Knowledge Base/Notes/x.md"
    )


def test_is_casing_only_rewrite_rejects_casefold_length_expansions() -> None:
    """`casefold()` alone is not enough: it is not length-preserving.

    `'ß'.casefold() == 'ss'`, `'ﬁ'` folds to `'fi'`, `'İ'` to `'i'` plus a
    combining dot. Those spellings name a *genuinely different* file, so a bare
    casefold comparison would call `straße.md -> STRASSE.md` a casing-only
    rewrite and launder that symlink. The pre-fold length guard rejects them.
    """
    assert "straße.md".casefold() == "STRASSE.md".casefold()  # the trap
    assert not vault.is_casing_only_rewrite("STRASSE.md", "straße.md")
    assert not vault.is_casing_only_rewrite("fish.md", "ﬁsh.md")


def test_canonical_vault_rel_returns_input_unchanged_when_resolution_relocates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform-free twin of the symlink test below.

    Stands in for any resolution that changes *which file* the path names —
    symlink, junction, 8.3 short name — without needing the privilege to make
    one, so every runner covers the invariant.
    """
    root = tmp_path

    def laundering_resolve(self: Path, *args, **kwargs):
        if self == root:
            return root
        return root / "Knowledge Base" / "Notes" / "shared.md"

    monkeypatch.setattr(Path, "resolve", laundering_resolve)

    rel = vault.canonical_vault_rel(root, "Knowledge Base/Notes/link.md")

    assert rel == "Knowledge Base/Notes/link.md"


def test_canonical_vault_rel_returns_input_unchanged_for_a_symlink(tmp_path: Path) -> None:
    """The regression: a symlink must not be laundered into its target.

    `tests/test_hosted_private_routes.py::
    test_hosted_transfer_scope_expiry_cross_cell_and_download_isolation` is the
    specification — it requires the download route to answer 400/INVALID_PATH
    for `Knowledge Base/Notes/link.md -> shared.md`. That only holds while the
    rel-form handed to the `O_NOFOLLOW` re-open still says `link.md`.
    """
    if not _symlinks_available(tmp_path):
        pytest.skip("this runner cannot create symlinks (Windows privilege)")
    _seed_symlinked_note(tmp_path, link="link.md", target="shared.md")

    rel = vault.canonical_vault_rel(tmp_path, "Knowledge Base/Notes/link.md")

    assert rel == "Knowledge Base/Notes/link.md"


def test_canonical_vault_rel_returns_input_unchanged_for_a_same_length_symlink(
    tmp_path: Path,
) -> None:
    """Prove the case comparison rejects it, not merely the length guard.

    `link.md` and `shrd.md` are the same length, so only a real
    case-insensitive comparison of the two spellings can tell them apart.
    """
    if not _symlinks_available(tmp_path):
        pytest.skip("this runner cannot create symlinks (Windows privilege)")
    _seed_symlinked_note(tmp_path, link="link.md", target="shrd.md")

    rel = vault.canonical_vault_rel(tmp_path, "Knowledge Base/Notes/link.md")

    assert rel == "Knowledge Base/Notes/link.md"


def test_canonical_vault_rel_result_is_always_case_equal_to_its_input(tmp_path: Path) -> None:
    """The invariant itself, asserted directly rather than via one expected value.

    Whatever `canonical_vault_rel` returns for a symlinked path, it must be
    case-insensitively equal to what the caller asked for — i.e. only the
    casing may ever change.
    """
    if not _symlinks_available(tmp_path):
        pytest.skip("this runner cannot create symlinks (Windows privilege)")
    _seed_symlinked_note(tmp_path, link="link.md", target="shared.md")
    requested = "Knowledge Base/Notes/link.md"

    rel = vault.canonical_vault_rel(tmp_path, requested)

    assert rel.casefold() == requested.casefold()


def test_resolve_under_vault_does_not_launder_a_symlink(tmp_path: Path) -> None:
    """The boundary `_open_bounded_vault_file` actually calls."""
    if not _symlinks_available(tmp_path):
        pytest.skip("this runner cannot create symlinks (Windows privilege)")
    _seed_symlinked_note(tmp_path, link="link.md", target="shared.md")
    requested = "Knowledge Base/Notes/link.md"

    _candidate, rel = vault.resolve_under_vault(
        tmp_path, requested, must_exist=True, must_be_file=True
    )

    assert rel == requested
    assert rel.casefold() == requested.casefold()


def test_edit_resolve_does_not_launder_a_symlink(tmp_path: Path) -> None:
    """`edit._resolve` mirrors `canonical_vault_rel` and had the same hole."""
    if not _symlinks_available(tmp_path):
        pytest.skip("this runner cannot create symlinks (Windows privilege)")
    from exomem import edit as edit_module

    _seed_symlinked_note(tmp_path, link="link.md", target="shared.md")
    requested = "Knowledge Base/Notes/link.md"

    _candidate, rel = edit_module._resolve(tmp_path, requested)

    assert rel == requested
    assert rel.casefold() == requested.casefold()
