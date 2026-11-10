"""Kind labels in any language, as slice 6 of `make-epistemic-language-dynamic` adds them.

This module began as the before-picture, in the style of `test_epistemic_kind_parity`: every
`# CORE` value never changes, and a `# FLIPS slice6` value is the one that the slice edited on
purpose. The comment on each flipped line names the value it held before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, semantic_language_registry, semantic_units
from exomem.governance.principal import library_scope

FINNISH = {
    "schema_version": 1,
    "categories": {},
    "kinds": {"protocol": {"description": "A repeatable protocol", "labels": {"fi": "Pöytäkirja"}}},
}


def _codes(registry) -> list[str]:
    return sorted(finding["code"] for finding in registry.findings)


def _parse(markdown: str, registry):
    return semantic_units.parse_semantic_units(markdown, language_registry=registry)


def test_a_kind_entry_takes_a_labels_field() -> None:
    registry = semantic_language_registry.load_registry(proposal=FINNISH)

    assert _codes(registry) == []  # FLIPS slice6: was ["unknown_field"] (a kind took no labels)


def test_a_finnish_heading_is_the_same_kind() -> None:
    registry = semantic_language_registry.load_registry(proposal=FINNISH)

    document = _parse("## Pöytäkirja\n\nFollow the steps.\n", registry)

    assert [unit.kind for unit in document.units] == ["protocol"]  # FLIPS slice6: was [] (no label)


def test_an_ascii_heading_resolves_to_its_shipped_kind() -> None:
    document = _parse(
        "## Open Question\n\nWhat ships?\n\n## claims\n\nIt ships.\n",
        semantic_language_registry.core_registry(),
    )

    assert [unit.kind for unit in document.units] == ["open_question", "claim"]  # CORE


def test_a_categories_save_adds_a_kind_label(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    current = {
        "schema_version": 1, "categories": {},
        "kinds": {"protocol": {"description": "A repeatable protocol"}},
    }
    created = semantic_language_registry.save_registry(vault, current)

    with library_scope():  # FLIPS slice6: was refused with INVALID_SEMANTIC_LANGUAGE_REGISTRY
        commands.op_schema_memory(
            vault, operation="infer", subject="categories", save=True, proposal=FINNISH,
            expected_hash=created["content_hash"],
        )

    saved = semantic_language_registry.load_registry(vault)
    assert saved.resolve_heading("Pöytäkirja").resolved == "protocol"  # FLIPS slice6: was None


# ---------------------------------------------------------------------------
# What slice 6 adds
# ---------------------------------------------------------------------------

CLAIM_IN_FINNISH = {
    "schema_version": 1, "categories": {}, "kinds": {"claim": {"labels": {"fi": "Väite"}}},
}


def _kinds(markdown: str, registry) -> list[str]:
    return [unit.kind for unit in _parse(markdown, registry).units]


def test_a_vault_label_on_a_shipped_kind_names_it_and_keeps_its_traits() -> None:
    registry = semantic_language_registry.load_registry(proposal=CLAIM_IN_FINNISH)

    # The same heading in capitals, and typed with a combining accent, is the same kind.
    for heading in ("Väite", "VÄITE", "Va\u0308ite"):
        document = _parse(f"## {heading}\n- verdict: refuted\n\nIt ships.\n", registry)
        assert [unit.kind for unit in document.units] == ["claim"], heading
        assert [error.code for error in document.errors] == [], heading
        assert document.units[0].verdict == "refuted", heading
    assert registry.has("claim", "assertion")
    assert registry.has("claim", "page_claim")
    # A stored heading keeps the form it was authored in: nothing is re-keyed.
    assert _parse("## Väite\n\nIt ships.\n", registry).units[0].kind_key == "väite"


def test_a_label_on_a_vault_kind_names_that_kind() -> None:
    registry = semantic_language_registry.load_registry(proposal=FINNISH)

    assert _kinds("## Pöytäkirja\n\nFollow the steps.\n", registry) == ["protocol"]
    assert registry.resolve_kind("pöytäkirja").resolved == "protocol"


def test_a_heading_without_a_label_stays_no_kind() -> None:
    registry = semantic_language_registry.load_registry(proposal=CLAIM_IN_FINNISH)

    assert _kinds("## Väitteet\n\nIt ships.\n", registry) == []
    assert _kinds("## Overview\n\nIt ships.\n", registry) == []


def test_every_ascii_heading_resolves_as_it_did_without_vault_labels() -> None:
    plain = semantic_language_registry.core_registry()
    labelled = semantic_language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {},
        "kinds": {
            "claim": {"labels": {"fi": "Väite"}},
            "protocol": {"description": "A repeatable protocol", "labels": {"fi": "Pöytäkirja"}},
        },
    })
    headings = [
        "Claim", "CLAIMS", "open question", "Open-Question", "open_questions", "Timeline", "TODO",
        "Observation", "Overview", "Summary", "Notes", "", " ", "-", "Straße",
    ]

    for heading in headings:
        assert labelled.resolve_heading(heading).resolved == plain.resolve_heading(heading).resolved
        assert labelled.resolve_kind(heading).resolved == plain.resolve_kind(heading).resolved


def test_a_label_of_the_compact_kind_is_no_heading() -> None:
    registry = semantic_language_registry.load_registry(
        proposal={"schema_version": 1, "categories": {}, "kinds": {"observation": {"labels": {"fi": "Havainto"}}}}
    )

    assert _kinds("## Havainto\n\nSomething seen.\n", registry) == []
    assert registry.resolve_kind("Havainto").resolved == "observation"


def test_every_shipped_label_names_its_own_kind_so_no_page_parses_differently() -> None:
    core = semantic_language_registry.core_registry()

    for key, kind in core.core_kinds.items():
        assert dict(kind.labels).get("en"), key
        for _language, label in kind.labels:
            assert core.resolve_kind(label).resolved == key, (key, label)
            if key != "observation":
                assert core.resolve_heading(label).resolved == key, (key, label)
                assert semantic_blocks_kind(label) == key, (key, label)


def semantic_blocks_kind(label: str) -> str | None:
    from exomem import semantic_blocks

    return semantic_blocks.normalize_block_type(label)


@pytest.mark.parametrize(
    ("kinds", "colliding"),
    [
        ({"protocol": {"description": "A protocol"}, "claim": {"labels": {"fi": "Protocol"}}}, "kinds.claim.labels.fi"),
        ({"protocol": {"description": "A protocol", "heading_aliases": ["rule"]}, "claim": {"labels": {"fi": "Rule"}}}, "kinds.claim.labels.fi"),
        ({"protocol": {"description": "A protocol", "aliases": ["procedure_x"]}, "claim": {"labels": {"fi": "Procedure x"}}}, "kinds.claim.labels.fi"),
        ({"protocol": {"description": "A protocol", "labels": {"fi": "Sääntö"}}, "claim": {"labels": {"fi": "sääntö"}}}, "kinds.protocol.labels.fi"),
        ({"claim": {"labels": {"fi": "Finding"}}}, "kinds.claim.labels.fi"),
        ({"claim": {"labels": {"fi": "Findings"}}}, "kinds.claim.labels.fi"),
    ],
    ids=[
        "vault-key", "vault-heading-alias", "vault-alias", "another-label", "shipped-key",
        "shipped-heading-alias",
    ],
)
def test_a_label_that_names_another_kind_is_refused_by_validate_and_by_the_save(
    tmp_path: Path, kinds: dict, colliding: str
) -> None:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    created = semantic_language_registry.save_registry(
        vault, {"schema_version": 1, "categories": {}, "kinds": {}}
    )
    proposal = {"schema_version": 1, "categories": {}, "kinds": kinds}

    validated = commands.op_schema_memory(
        vault, operation="validate", subject="categories", proposal=proposal
    )
    with library_scope(), pytest.raises(ValueError) as refused:
        commands.op_schema_memory(
            vault, operation="infer", subject="categories", save=True, proposal=proposal,
            expected_hash=created["content_hash"],
        )

    found = [item for item in validated["findings"] if item["code"] == "kind_label_collision"]
    assert [item["path"] for item in found] == [colliding]
    assert validated["valid"] is False
    assert str(refused.value).startswith("INVALID_SEMANTIC_LANGUAGE_REGISTRY")
    assert found[0]["detail"] in str(refused.value)
    assert "is already the key, an alias or a label of" in found[0]["detail"]


def test_an_entry_under_a_shipped_key_that_carries_more_than_labels_stays_a_collision() -> None:
    registry = semantic_language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {},
        "kinds": {"claim": {"description": "Mine", "labels": {"fi": "Väite"}}},
    })

    assert [(f["code"], f["path"]) for f in registry.findings] == [
        ("canonical_collision", "kinds.claim")
    ]


@pytest.mark.parametrize(
    ("language", "label"),
    [("hi", "दावा"), ("tr", "İddia"), ("fa", "فرضیه\u200cها")],
    ids=["combining-marks", "casefold-adds-a-mark", "joiner-inside-a-word"],
)
def test_a_label_in_a_script_with_marks_or_joiners_parses_as_its_kind(
    language: str, label: str
) -> None:
    """Catches a label rule that accepts letters only: Hindi vowel signs are marks, casefolding
    a Turkish dotted capital I adds one, and Persian writes a joiner inside a word."""
    registry = semantic_language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {}, "kinds": {"claim": {"labels": {language: label}}},
    })

    assert registry.findings == ()
    assert _kinds(f"## {label}\n\nIt ships.\n", registry) == ["claim"]


@pytest.mark.parametrize(
    ("labels", "code", "named"),
    [
        ({False: "Påstand"}, "invalid_label", None),  # YAML reads an unquoted `no:` as False
        ({"English": "Claim"}, "invalid_label", None),
        ({"fi": "Väite?"}, "invalid_label", "U+003F"),
        ({"hi": "\u200dदावा"}, "invalid_label", "U+200D"),
        ({"fi": "---"}, "invalid_label", None),
        ("Väite", "invalid_labels", None),
    ],
    ids=[
        "yaml-boolean-tag", "not-a-language-tag", "not-a-heading", "joiner-at-the-start",
        "empty-heading", "not-a-map",
    ],
)
def test_a_label_that_cannot_be_a_heading_is_refused(
    labels: object, code: str, named: str | None
) -> None:
    registry = semantic_language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {}, "kinds": {"claim": {"labels": labels}},
    })

    assert [finding["code"] for finding in registry.findings] == [code]
    if named is not None:
        # The refusal names the first character no heading label takes, by its code point.
        assert named in registry.findings[0]["detail"]
    # A refused registry names no vault kind, as for every other error finding.
    assert registry.resolve_heading("Väite").resolved is None


@library_scope()
def test_a_finnish_heading_is_a_claim_in_a_search_hit(vault: Path) -> None:
    from exomem import find as find_module
    from exomem import freshness, lexstore

    page = vault / "Knowledge Base" / "Notes" / "julkaisu.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: insight\nprojects: [alpha]\n---\n\n# Julkaisu\n\n"
        "## Väite\n\nThe release ships on Friday.\n",
        encoding="utf-8",
    )
    semantic_language_registry.save_registry(vault, CLAIM_IN_FINNISH)
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)

    def hits(kind: str) -> list[tuple[str, str]]:
        found = find_module.find(
            vault, query="release ships", scope="kb-only", result_level="unit",
            filters={"unit.kind": {"$eq": kind}}, limit=10,
        )
        return [
            (hit.kind, hit.category_raw) for hit in found
            if hit.parent_path == "Knowledge Base/Notes/julkaisu.md"
        ]

    assert hits("claim") == [("claim", "Väite")]
    assert hits("decision") == []


# ---------------------------------------------------------------------------
# Saving a label, and reading it back
# ---------------------------------------------------------------------------


def _saved_vault(tmp_path: Path, kinds: dict) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    created = semantic_language_registry.save_registry(
        vault, {"schema_version": 1, "categories": {}, "kinds": kinds}
    )
    return vault, created["content_hash"]


def _save(vault: Path, expected_hash: str, kinds: dict) -> None:
    with library_scope():
        commands.op_schema_memory(
            vault, operation="infer", subject="categories", save=True,
            proposal={"schema_version": 1, "categories": {}, "kinds": kinds},
            expected_hash=expected_hash,
        )


def test_a_categories_save_adds_and_removes_a_labels_only_entry_for_a_shipped_kind(
    tmp_path: Path,
) -> None:
    vault, before = _saved_vault(tmp_path, {})

    _save(vault, before, CLAIM_IN_FINNISH["kinds"])
    added = semantic_language_registry.load_registry(vault)
    assert added.resolve_heading("Väite").resolved == "claim"
    assert added.kinds == {}  # the entry labels the shipped kind; it defines no vault kind

    # Removing the entry is no drop: the shipped kind still resolves without it.
    _save(vault, added.content_hash, {})
    assert semantic_language_registry.load_registry(vault).resolve_heading("Väite").resolved is None


def test_a_categories_save_changes_the_labels_of_a_vault_kind(tmp_path: Path) -> None:
    vault, before = _saved_vault(tmp_path, FINNISH["kinds"])
    relabelled = {"protocol": {"description": "A repeatable protocol", "labels": {"sv": "Protokoll"}}}

    _save(vault, before, relabelled)

    saved = semantic_language_registry.load_registry(vault)
    assert saved.resolve_heading("Protokoll").resolved == "protocol"
    assert saved.resolve_heading("Pöytäkirja").resolved is None


@pytest.mark.parametrize(
    "kinds",
    [
        {"protocol": {"description": "Another description", "labels": {"fi": "Pöytäkirja"}}},
        {
            "protocol": {"description": "A repeatable protocol", "labels": {"fi": "Pöytäkirja"}},
            "playbook": {"description": "A playbook"},
        },
        {},
    ],
    ids=["description-changed", "kind-added", "kind-removed"],
)
def test_a_categories_save_still_refuses_every_other_kind_change(tmp_path: Path, kinds: dict) -> None:
    vault, before = _saved_vault(tmp_path, FINNISH["kinds"])

    with pytest.raises(ValueError, match="CATEGORY_SAVE_KIND_CHANGE"):
        _save(vault, before, kinds)


def test_the_schema_read_lists_each_kinds_labels_and_its_diff_reports_a_label_change(
    tmp_path: Path,
) -> None:
    vault, _hash = _saved_vault(tmp_path, {**CLAIM_IN_FINNISH["kinds"], **FINNISH["kinds"]})

    read = commands.op_schema_memory(vault, operation="infer", subject="categories")
    diff = commands.op_schema_memory(
        vault, operation="diff", subject="categories",
        proposal={"schema_version": 1, "categories": {}, "kinds": {
            "claim": {"labels": {"fi": "Väite", "sv": "Påstående"}},
            "protocol": FINNISH["kinds"]["protocol"],
        }},
    )

    kinds = {entry["key"]: entry for entry in read["kinds"]}
    assert kinds["claim"]["labels"] == {"en": "Claim", "fi": "Väite"}
    assert kinds["claim"]["shipped"] is True
    assert kinds["protocol"]["labels"] == {"fi": "Pöytäkirja"}
    assert kinds["protocol"]["shipped"] is False
    assert diff["kind_labels_changed"] is True
    assert diff["changes"]["kind_labels"] == {
        "added": {"claim": {"sv": "Påstående"}}, "removed": {},
    }
    assert diff["kinds_changed"] is False


def test_the_diff_reports_a_removed_labels_only_entry_as_removed_labels_not_a_removed_kind(
    tmp_path: Path,
) -> None:
    vault, _hash = _saved_vault(tmp_path, CLAIM_IN_FINNISH["kinds"])

    diff = commands.op_schema_memory(
        vault, operation="diff", subject="categories",
        proposal={"schema_version": 1, "categories": {}, "kinds": {}},
    )

    assert diff["changes"]["kinds"]["removed"] == []
    assert diff["kinds_changed"] is False
    assert diff["changes"]["kind_labels"] == {"added": {}, "removed": {"claim": {"fi": "Väite"}}}


# ---------------------------------------------------------------------------
# What a label save does to the stored headings, said before and at the save
# ---------------------------------------------------------------------------

NOTES_AS_CLAIM = {
    "schema_version": 1, "categories": {}, "kinds": {"claim": {"labels": {"fi": "Huomiot"}}},
}


def _pages_with_a_notes_heading(vault: Path, *, kinds: dict, indexed: bool) -> str:
    """Two pages with a `## Huomiot` heading, two compact observations under one of them, and a
    saved registry with `kinds`; the registry's hash."""
    from exomem import freshness, lexstore

    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "julkaisu.md").write_text(
        "---\ntype: insight\n---\n\n# Julkaisu\n\n## Huomiot\n\n"
        "- [fact] The release ships on Friday.\n- [fact] The tests pass.\n",
        encoding="utf-8",
    )
    (notes / "testit.md").write_text(
        "---\ntype: insight\n---\n\n# Testit\n\n## Huomiot\n\nThe suite runs nightly.\n",
        encoding="utf-8",
    )
    created = semantic_language_registry.save_registry(
        vault, {"schema_version": 1, "categories": {}, "kinds": kinds}
    )
    if indexed:
        entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
        freshness.seed(vault, "kb", entries)
        freshness.seed(vault, "vault", entries)
        lexstore.ensure_fresh(vault)
    return created["content_hash"]


@library_scope()
def test_a_label_save_leads_with_the_headings_it_makes_units_as_its_validation_does(
    vault: Path,
) -> None:
    expected_hash = _pages_with_a_notes_heading(vault, kinds={}, indexed=True)

    validated = commands.op_schema_memory(
        vault, operation="validate", subject="categories", proposal=NOTES_AS_CLAIM,
    )
    saved = commands.op_schema_memory(
        vault, operation="infer", subject="categories", save=True, proposal=NOTES_AS_CLAIM,
        expected_hash=expected_hash,
    )

    assert next(iter(saved)) == "outcome"
    assert saved["outcome"] == (
        "Adding the fi label 'Huomiot' to claim: 2 stored headings 'Huomiot' on 2 pages start "
        "being claim units. The 2 compact observations under them join those units."
    )
    assert validated["outcome"] == saved["outcome"]


@library_scope()
def test_a_label_removal_leads_with_the_headings_that_stop_being_units(vault: Path) -> None:
    expected_hash = _pages_with_a_notes_heading(
        vault, kinds=NOTES_AS_CLAIM["kinds"], indexed=True
    )

    saved = commands.op_schema_memory(
        vault, operation="infer", subject="categories", save=True,
        proposal={"schema_version": 1, "categories": {}, "kinds": {}},
        expected_hash=expected_hash,
    )

    assert saved["outcome"] == (
        "Removing the fi label 'Huomiot' from claim: 2 stored headings 'Huomiot' on 2 pages stop "
        "being claim units. The 2 compact observations under them become observations of their "
        "own again."
    )


@library_scope()
def test_a_label_save_says_the_count_is_unknown_when_the_store_is_not_current(
    vault: Path,
) -> None:
    expected_hash = _pages_with_a_notes_heading(vault, kinds={}, indexed=False)

    saved = commands.op_schema_memory(
        vault, operation="infer", subject="categories", save=True, proposal=NOTES_AS_CLAIM,
        expected_hash=expected_hash,
    )

    assert saved["outcome"] == (
        "Adding the fi label 'Huomiot' to claim: the stored headings 'Huomiot' start being claim "
        "units; how many is unknown, because the lexical store is not current."
    )
