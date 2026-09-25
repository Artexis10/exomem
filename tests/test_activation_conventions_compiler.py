"""Tasks 2.1-2.5 — the compiler reads the activation-conventions registry.

Integration coverage over the REAL `WorkingSetIndex` + `working_set_resolve`
+ `working_set_state` + `working_set_runtime` stack, never a hand-authored
packet: anchor membership and the walk's skip list come from the effective
conventions, categories consult the semantic-language registry first,
stopwords/the rarity threshold are read once per build and shared by turn
matching and derived-name admission, and a conventions edit rebuilds the
sidecar exactly like a schema-version mismatch.
"""

from __future__ import annotations

from pathlib import Path

from exomem import (
    activation_conventions,
    commands,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)
from exomem.kbdir import kb_dirname
from exomem.ranking_config import RankingConfig


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _override_conventions(vault: Path, text: str) -> None:
    path = activation_conventions.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    activation_conventions.clear_cache()


def _reset(vault: Path) -> None:
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()


# --------------------------------------------------------------------------- #
# 2.1 — membership, skip list, governance trees, archived anchors
# --------------------------------------------------------------------------- #


def test_a_staged_upload_is_not_an_anchor(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "_Staging" / "draft.md",
        "---\nstatus: active\ntags: [hub]\n---\n\n# Draft\n\nUnvetted.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert not any(a.path.endswith("_Staging/draft.md") for a in index.anchors())


def test_a_template_tagged_hub_is_not_an_anchor(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Templates" / "hub-template.md",
        "---\nstatus: active\ntags: [hub]\n---\n\n# Hub template\n\nExample frontmatter.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert not any(a.path.endswith("Templates/hub-template.md") for a in index.anchors())


def test_an_archived_hub_stays_an_anchor(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "_archive" / "old-hub.md",
        "---\nstatus: superseded\ntags: [hub]\n---\n\n# Old hub\n\nArchived but resolvable.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("_archive/old-hub.md")]
    assert matches, "an archived anchor must remain in the catalogue"
    assert matches[0].lifecycle == "superseded"


def test_a_page_inside_the_governance_trees_is_never_an_anchor(vault: Path) -> None:
    """The one other intended difference from today: `_Schema`, `_Governance`
    and `_Adoption` are never anchors, whatever their tags or type."""
    kb = vault / kb_dirname()
    for tree in ("_Schema", "_Governance", "_Adoption"):
        _write(
            kb / tree / "doc.md",
            "---\nstatus: active\ntags: [hub]\ntype: entity\n---\n\n# Doc\n\nGovernance material.\n",
        )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    for tree in ("_Schema", "_Governance", "_Adoption"):
        assert not any(a.path.endswith(f"{tree}/doc.md") for a in index.anchors())


def test_case_insensitive_products_match_pinned_on_lowercase_folder(vault: Path) -> None:
    """The one intended difference decision 3 names: `Products`/`Systems`
    match case-insensitively."""
    kb = vault / kb_dirname()
    _write(
        kb / "products" / "gadget.md",
        "---\ntitle: Gadget\nstatus: active\n---\n\n# Gadget\n\nA lowercase-folder resource.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("products/gadget.md")]
    assert matches and matches[0].kind == "resource"


def test_a_vault_names_its_own_resource_folder_end_to_end(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Equipment" / "Field Recorder.md",
        "---\ntitle: Field Recorder\nstatus: active\n---\n\n# Field Recorder\n\nA portable recorder.\n",
    )
    _override_conventions(vault, "schema_version: 1\nanchors:\n  resource:\n    add_folders: [Equipment]\n")
    _reset(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("Equipment/Field Recorder.md")]
    assert matches and matches[0].kind == "resource"


# --------------------------------------------------------------------------- #
# 2.2 — categories through the semantic-language registry first
# --------------------------------------------------------------------------- #


def test_no_built_in_category_label_ever_resolves_differently_through_the_registry() -> None:
    """The universal form of 'no category earned today is lost' (task 2.2):
    for every label the shipped `_CATEGORY_BY_LABEL` map ever assigned a
    category to, the shipped semantic-language registry either agrees or is
    excluded (unregistered/invalid/scope-violated) and falls through to that
    same map — never a DIFFERENT category. Exhaustive over the whole map,
    which is what actually backs the claim over the scaffold vault and the
    audit corpus (both draw their headings from the same closed vocabulary),
    rather than an empirical sweep that could miss a heading neither corpus
    happens to use."""
    from exomem import semantic_language_registry as slr
    from exomem.working_set_index import _CATEGORY_BY_LABEL

    registry = slr.load_registry(None)
    excluded = {"unregistered", "registry_invalid", "scope_violation"}
    for label, expected in _CATEGORY_BY_LABEL.items():
        resolution = registry.resolve_category(label)
        if resolution.status in excluded:
            continue
        assert resolution.resolved == expected, (label, resolution)


def test_next_steps_still_maps_to_action(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "widget.md",
        "---\ntitle: Widget\nstatus: active\n---\n\n# Widget\n\n## Next Steps\n\nDo the thing.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("Products/widget.md")]
    assert matches and matches[0].categories == ("action",)


def test_a_vault_category_alias_earns_its_category(vault: Path) -> None:
    from exomem import semantic_language_registry as slr

    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "gizmo.md",
        "---\ntitle: Gizmo\nstatus: active\n---\n\n# Gizmo\n\n## Zusammenfassung\n\nGerman heading.\n",
    )
    slr_path = slr.registry_path(vault)
    slr_path.parent.mkdir(parents=True, exist_ok=True)
    slr_path.write_text(
        "schema_version: 1\n"
        "categories:\n"
        "  custom_summary:\n"
        "    description: A vault-specific summary category.\n"
        "    aliases: [zusammenfassung]\n",
        encoding="utf-8",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("Products/gizmo.md")]
    assert matches and "custom_summary" in matches[0].categories


def test_a_broken_semantic_language_override_does_not_fabricate_a_category(vault: Path) -> None:
    """`registry_invalid` sets `LabelResolution.resolved` to the raw label's
    OWN key, not to nothing — taking it at face value would inject the raw
    heading text as a fabricated category the moment a vault's semantic-
    language override happened to be broken."""
    from exomem import semantic_language_registry as slr

    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "broken.md",
        "---\ntitle: Broken\nstatus: active\n---\n\n# Broken\n\n## Zusammenfassung\n\nHeading.\n",
    )
    slr_path = slr.registry_path(vault)
    slr_path.parent.mkdir(parents=True, exist_ok=True)
    # A category shadowing a core name is `registry_invalid` (missing the
    # required description on top of the shadow warning).
    slr_path.write_text("schema_version: 1\ncategories:\n  fact: {}\n", encoding="utf-8")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    matches = [a for a in index.anchors() if a.path.endswith("Products/broken.md")]
    assert matches
    assert "zusammenfassung" not in matches[0].categories


# --------------------------------------------------------------------------- #
# 2.4 — stopwords and the rarity threshold, read once, shared by both users
# --------------------------------------------------------------------------- #


def test_a_stopword_removes_a_derived_alias_in_one_build(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "orchard.md",
        "---\ntitle: Orchard (blue trim)\nstatus: active\n---\n\n# Orchard (blue trim)\n\nOne of a kind.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    before = next(a for a in index.anchors() if a.path.endswith("orchard.md"))
    assert "orchard" in before.aliases

    _override_conventions(vault, "schema_version: 1\nstopwords:\n  add: [orchard]\n")
    _reset(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    after = next(a for a in index.anchors() if a.path.endswith("orchard.md"))
    # "orchard" is now a stopword and the derived name's ONLY token: the
    # derived-name admission (`working_set_index.derived_short_name`) and
    # turn matching (`working_set_resolve.candidates_for`) read the SAME
    # effective stopword list this build loaded once.
    assert "orchard" not in after.aliases


def test_the_resolver_floor_of_two_holds_even_if_ranking_config_asks_for_one() -> None:
    row = working_set_resolve.AnchorFacts(
        anchor_id="a.md",
        path="a.md",
        ref=None,
        title="Alpha Beta",
        kind="hub",
        lifecycle="active",
        aliases=(),
        terms=("alpha", "beta", "notes"),
        categories=(),
        neighbourhood=frozenset(),
    )
    config = RankingConfig(working_set_lexical_min_terms=1)
    # One shared word with the config asking for one: the floor of two holds,
    # so the single shared term is never `lexical_overlap`.
    single = working_set_resolve.candidates_for(
        working_set_resolve.analyze_turn("tell me about beta"), (row,), config=config
    )
    assert all("lexical_overlap" not in candidate.evidence for candidate in single)
    # Two shared name words clear the floor.
    both = working_set_resolve.candidates_for(
        working_set_resolve.analyze_turn("tell me about alpha beta"), (row,), config=config
    )
    assert both
    assert "lexical_overlap" in both[0].evidence


# --------------------------------------------------------------------------- #
# 2.5 — a conventions edit rebuilds the sidecar, busts the cache, stales tokens
# --------------------------------------------------------------------------- #


def test_conventions_hash_is_stored_in_the_sidecar_meta_and_mismatch_wipes(vault: Path) -> None:
    import sqlite3

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    conn = sqlite3.connect(index.path)
    stored = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('schema_version', 'conventions_hash')"))
    conn.close()
    assert int(stored["schema_version"]) == working_set_index.SCHEMA_VERSION
    shipped_hash = activation_conventions.shipped_conventions().conventions_hash
    assert stored["conventions_hash"] == shipped_hash

    generation_before = index.generation()
    _override_conventions(vault, "schema_version: 1\nstopwords:\n  add: [zzzznotaword]\n")
    index2 = working_set_index.WorkingSetIndex(vault)
    index2.rebuild()
    assert index2.generation() > generation_before
    conn = sqlite3.connect(index2.path)
    stored2 = dict(conn.execute("SELECT key, value FROM meta WHERE key = 'conventions_hash'"))
    conn.close()
    assert stored2["conventions_hash"] != shipped_hash


def test_edit_with_no_vault_write_still_rebuilds_the_sidecar_and_stales_a_token(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "widget.md",
        "---\ntitle: Widget\nstatus: active\n---\n\n# Widget\n\nA thing about widgets.\n",
    )
    turn = "tell me about the widget"
    packet1 = commands.op_activate_context(vault, turn=turn)
    token = packet1.get("continuity")
    assert token
    hash_before = packet1["generation"]["conventions_hash"]

    # An override edit — no vault content file touched at all.
    _override_conventions(vault, "schema_version: 1\nstopwords:\n  add: [zzzznotaword2]\n")

    packet2 = commands.op_activate_context(vault, turn=turn, continuity=token)
    assert packet2["generation"]["conventions_hash"] != hash_before
    assert packet2["generation"]["continuity"] == working_set_runtime.CONTINUITY_STALE


def test_no_cached_packet_is_served_across_a_conventions_edit(vault: Path) -> None:
    kb = vault / kb_dirname()
    _write(
        kb / "Products" / "widget.md",
        "---\ntitle: Widget\nstatus: active\nvalue: 5 units\n---\n\n# Widget\n\nA thing.\n",
    )
    turn = "tell me about the widget"
    packet1 = commands.op_activate_context(vault, turn=turn)
    assert packet1["current_state"][0]["statement"] == "status: active"

    _override_conventions(vault, "schema_version: 1\nstate:\n  prefer_state_fields: [value]\n")
    # Deliberately no `working_set_runtime.reset_caches_for_tests()`: the
    # cache key alone must bust the stale entry.
    packet2 = commands.op_activate_context(vault, turn=turn)
    assert packet2["current_state"][0]["statement"] == "value: 5 units"
