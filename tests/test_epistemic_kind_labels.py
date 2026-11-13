"""Kind labels in any language: a vault gives a kind labels, and a `##` heading written in a
label names that kind."""

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


def _parse(markdown: str, registry):
    return semantic_units.parse_semantic_units(markdown, language_registry=registry)


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
        ({"fi": "1"}, "invalid_label", "holds no letter"),
        ("Väite", "invalid_labels", None),
    ],
    ids=[
        "yaml-boolean-tag", "not-a-language-tag", "not-a-heading", "joiner-at-the-start",
        "empty-heading", "digits-only", "not-a-map",
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
        # The refusal names the first character no heading label takes, by its code point, or
        # says that the label holds no letter.
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


@library_scope()
@pytest.mark.parametrize(
    ("proposal", "heading", "kind"),
    [(CLAIM_IN_FINNISH, "Väite", "claim"), (FINNISH, "Pöytäkirja", "protocol")],
)
def test_a_labelled_unit_keeps_its_address_through_read_validate_and_update(
    vault: Path, proposal: dict, heading: str, kind: str,
) -> None:
    """A unit found through a vault label can be edited with either public address form."""
    from exomem import find as find_module
    from exomem import freshness, lexstore

    page = vault / "Knowledge Base" / "Notes" / "Insights" / "julkaisu.md"
    path = page.relative_to(vault).as_posix()
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n\n"
        f"# Julkaisu\n\n## {heading}\n- id: release-day\n- source_note: Keep this row.\n\n"
        "The release ships on Friday.\n",
        encoding="utf-8",
    )
    semantic_language_registry.save_registry(vault, proposal)
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)
    hit = next(
        hit for hit in find_module.find(
            vault, query="release ships", scope="kb-only", result_level="unit", limit=10,
        )
        if hit.parent_path == path
    )
    exact = commands.op_read_memory(vault, path=path, unit_ref=hit.unit_ref)
    listed = commands.op_read_memory(vault, path=path, units=True)
    assert exact["status"] == "found"
    assert exact["unit"]["kind"] == kind
    assert len(listed["units"]) == 1
    assert listed["units"][0]["anchor"] == "release-day"
    assert listed["units"][0]["category"] == exact["unit"]["category"]
    assert listed["units"][0]["text"] == "The release ships on Friday."
    arguments = {
        "path": path, "category": "claim", "content": "The release ships on Monday.",
    }
    validated = commands.op_observe_memory(
        vault, operation="validate", unit_ref=hit.unit_ref,
        expected_fingerprint=exact["unit"]["fingerprint"],
        expected_hash=listed["content_hash"], **arguments,
    )
    assert validated["mutated"] is False
    assert "Friday" in page.read_text(encoding="utf-8")
    updated = commands.op_observe_memory(
        vault, operation="update", unit=listed["units"][0]["id"],
        transition_token=validated["semantic"]["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validated["semantic"]["transition_hash"],
        relation_review_reason="This release schedule has no related conclusion.", **arguments,
    )
    assert updated["unit"]["kind"] == kind
    assert updated["unit"]["unit_ref"] == hit.unit_ref
    assert updated["unit"]["fingerprint"] == validated["unit"]["fingerprint"]
    assert updated["unit"]["metadata"]["source_note"] == "Keep this row."
    reread = commands.op_read_memory(vault, path=path, unit_ref=hit.unit_ref)
    assert reread["unit"]["content"] == "The release ships on Monday."
    current = commands.op_read_memory(vault, path=path, units=True)["units"][0]
    assert current["anchor"] == listed["units"][0]["anchor"]
    assert current["id"] != listed["units"][0]["id"]
    found = find_module.find(
        vault, query="release ships", scope="kb-only", mode="keyword", graph=False,
        result_level="unit", limit=10,
    )
    assert [hit.excerpt for hit in found if hit.parent_path == path] == [
        "The release ships on Monday.",
    ]


@library_scope()
def test_a_label_change_updates_unit_ids_without_rebuilding_neutral_rows(vault: Path) -> None:
    """A new label must not need a parent reparse or lexical rebuild to become addressable."""
    from exomem import freshness, lexstore, semantic_index

    path = "Knowledge Base/Notes/Insights/label-change.md"
    (vault / path).write_text(
        "---\ntype: insight\n---\n# Label change\n\n"
        "## Pöytäkirja\n- id: local-protocol\n\nFollow the release protocol.\n",
        encoding="utf-8",
    )
    saved = semantic_language_registry.save_registry(vault, {
        "schema_version": 1, "categories": {},
        "kinds": {"protocol": {"description": "A repeatable protocol"}},
    })
    assert all(freshness.rebaseline(vault).values())
    lexstore.ensure_fresh(vault)
    neutral = semantic_index.current_parent_index_state(vault, path)
    assert commands.op_read_memory(vault, path=path, units=True)["units"] == []

    semantic_language_registry.save_registry(vault, FINNISH, expected_hash=saved["content_hash"])

    selected = commands.op_read_memory(vault, path=path, units=True)["units"]
    assert len(selected) == 1
    assert selected[0]["text"] == "Follow the release protocol."
    unchanged = semantic_index.current_parent_index_state(vault, path)
    assert unchanged.parent_generation == neutral.parent_generation
    assert unchanged.document.units == neutral.document.units == ()


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
        "Adding the fi label 'Huomiot' to claim: up to 2 stored headings 'Huomiot' on up to 2 "
        "pages start being claim units. Each count is an upper bound: a heading nested in "
        "another unit, or on a page outside the kind's scope, is no unit of its own. Up to 2 "
        "compact observations under those headings join the new units."
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
        "Removing the fi label 'Huomiot' from claim: up to 2 stored headings 'Huomiot' on up to "
        "2 pages stop being claim units. Each count is an upper bound: a heading nested in "
        "another unit, or on a page outside the kind's scope, is no unit of its own. Up to 2 "
        "compact observations under those headings become observations again."
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


@library_scope()
def test_the_count_sentence_is_an_upper_bound_on_the_units_a_label_save_makes(
    vault: Path,
) -> None:
    """A nested heading folds into its outer unit, and a scoped kind names no heading outside
    its scope, so the store counts more headings than become units."""
    from exomem import freshness, lexstore, memory_schema

    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    pages = {
        "väitteet.md": "---\ntype: insight\n---\n\n# Väitteet\n\n## Väite\n\n"
        "- [fact] The release ships on Friday.\n- [fact] The tests pass.\n\n## Väite\n",
        "sisäkkäin.md": "---\ntype: insight\n---\n\n# Sisäkkäin\n\n## Väite\n\nThe suite runs.\n\n"
        "### Väite\n\n- [fact] It runs nightly.\n- [fact] It runs on push.\n",
        "beta.md": "---\ntype: insight\nprojects: [beta]\n---\n\n# Beta\n\n## Pöytäkirja\n\nStep one.\n",
        "alpha.md": "---\ntype: insight\nprojects: [alpha]\n---\n\n# Alpha\n\n## Pöytäkirja\n\n"
        "Step one.\n",
    }
    for name, text in pages.items():
        (notes / name).write_text(text, encoding="utf-8")
    protocol = {"description": "A repeatable protocol", "scope": {"projects": ["alpha"]}}
    created = semantic_language_registry.save_registry(
        vault, {"schema_version": 1, "categories": {}, "kinds": {"protocol": protocol}}
    )
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)
    labelled = {
        "schema_version": 1, "categories": {}, "kinds": {
            "claim": {"labels": {"fi": "Väite"}},
            "protocol": {**protocol, "labels": {"fi": "Pöytäkirja"}},
        },
    }

    def parsed(registry) -> list[tuple[str, str]]:
        return [
            (item["form"], item["kind"])
            for item in memory_schema.category_observations(vault, registry=registry)
            if item["path"].removeprefix("Knowledge Base/Notes/") in pages
        ]

    before = parsed(semantic_language_registry.load_registry(vault))
    saved = commands.op_schema_memory(
        vault, operation="infer", subject="categories", save=True, proposal=labelled,
        expected_hash=created["content_hash"],
    )
    after = parsed(semantic_language_registry.load_registry(vault))

    assert saved["outcome"] == (
        "Adding the fi label 'Väite' to claim: up to 3 stored headings 'Väite' on up to 2 pages "
        "start being claim units. Each count is an upper bound: a heading nested in another "
        "unit, or on a page outside the kind's scope, is no unit of its own. Up to 6 compact "
        "observations under those headings join the new units. "
        "Adding the fi label 'Pöytäkirja' to protocol: up to 2 stored headings 'Pöytäkirja' on "
        "up to 2 pages start being protocol units. Each count is an upper bound: a heading "
        "nested in another unit, or on a page outside the kind's scope, is no unit of its own."
    )
    absorbed = before.count(("compact", "observation")) - after.count(("compact", "observation"))
    assert (after.count(("rich", "claim")), absorbed) == (2, 4)  # within the bounds 3 and 6
    assert after.count(("rich", "protocol")) == 1  # within the bound 2
