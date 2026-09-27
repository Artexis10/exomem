"""Task 1.1 — the versioned activation-conventions registry.

Mirrors `test_context_roles.py`'s load-contract coverage, plus the digest
contract this registry adds: the digest is taken over the EFFECTIVE
conventions (the values in force after bounds and findings are applied),
never over the override file's bytes, so two files that resolve to the same
conventions share a digest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import activation_conventions as ac
from exomem.working_set_index import (
    _RAW_MATERIAL_FOLDERS,
    _SKIP_DIR_NAMES,
    RARE_TERM_MAX_ANCHORS,
    STOPWORDS,
)
from exomem.working_set_state import _DATE_FIELDS, _STATE_FIELDS


def _override(vault: Path, payload: object) -> None:
    path = ac.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    ac.clear_cache()


# --------------------------------------------------------------------------- #
# Shipped-equals-previous-constants
# --------------------------------------------------------------------------- #


def test_shipped_resource_folders_equal_the_deleted_constants() -> None:
    registry = ac.shipped_conventions()
    assert registry.source == "shipped"
    assert registry.findings == ()
    assert registry.conventions.anchors["resource"].folders == ("products", "systems")
    assert registry.conventions.anchors["hub"].tags == frozenset({"hub"})


def test_shipped_skip_folders_equal_the_deleted_constant_exact_case() -> None:
    registry = ac.shipped_conventions()
    assert registry.conventions.skip_folders == _SKIP_DIR_NAMES


def test_shipped_state_and_date_fields_equal_the_deleted_constants() -> None:
    registry = ac.shipped_conventions()
    assert registry.conventions.state_fields == _STATE_FIELDS
    assert registry.conventions.date_fields == _DATE_FIELDS


def test_shipped_stopwords_equal_the_deleted_constant() -> None:
    registry = ac.shipped_conventions()
    assert registry.conventions.stopwords == STOPWORDS


def test_shipped_rare_term_threshold_equals_the_deleted_constant() -> None:
    registry = ac.shipped_conventions()
    assert registry.conventions.rare_term_max_anchors == RARE_TERM_MAX_ANCHORS


def test_raw_material_folders_constant_still_names_sources_and_evidence() -> None:
    """Pin kept for equivalence even though the walk now reads
    `vault.in_append_only_tree` rather than this copy directly."""
    assert _RAW_MATERIAL_FOLDERS == frozenset({"Sources", "Evidence"})


def test_scaffold_and_plugin_copies_are_byte_identical() -> None:
    repo = Path(__file__).resolve().parents[1]
    scaffold = repo / "src" / "exomem" / "_scaffold" / "_Schema" / ac.REGISTRY_FILENAME
    plugin = repo / "plugins" / "claude-code" / "skills" / "exomem" / ac.REGISTRY_FILENAME

    assert scaffold.is_file()
    assert plugin.is_file()
    assert scaffold.read_bytes() == plugin.read_bytes()


# --------------------------------------------------------------------------- #
# No override, no change
# --------------------------------------------------------------------------- #


def test_no_override_no_change(vault: Path) -> None:
    registry = ac.load_conventions(vault)
    shipped = ac.shipped_conventions()
    assert registry.source == "shipped"
    assert registry.conventions == shipped.conventions
    assert registry.conventions_hash == shipped.conventions_hash


# --------------------------------------------------------------------------- #
# Add / drop
# --------------------------------------------------------------------------- #


def test_a_vault_names_its_own_resource_folder(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"add_folders": ["Equipment"]}}})
    registry = ac.load_conventions(vault)
    assert "equipment" in registry.conventions.anchors["resource"].folders
    assert registry.source == "vault"


def test_dropping_a_shipped_resource_folder(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"drop_folders": ["Systems"]}}})
    registry = ac.load_conventions(vault)
    assert "systems" not in registry.conventions.anchors["resource"].folders
    assert "products" in registry.conventions.anchors["resource"].folders


def test_hub_add_tags(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"hub": {"add_tags": ["hub-de"]}}})
    registry = ac.load_conventions(vault)
    assert {"hub", "hub-de"} <= registry.conventions.anchors["hub"].tags


def test_add_skip_folders(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"add_skip_folders": ["Vorlagen"]}})
    registry = ac.load_conventions(vault)
    assert "Vorlagen" in registry.conventions.skip_folders
    assert registry.conventions.skip_folders >= _SKIP_DIR_NAMES


def test_state_prefer_and_drop(vault: Path) -> None:
    _override(
        vault,
        {"schema_version": 1, "state": {"prefer_state_fields": ["stock"], "drop_state_fields": ["value"]}},
    )
    registry = ac.load_conventions(vault)
    fields = registry.conventions.state_fields
    assert fields[0] == "stock"
    assert "value" not in fields
    assert "state" in fields


def test_stopwords_add(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "stopwords": {"add": ["der", "die", "das"]}})
    registry = ac.load_conventions(vault)
    assert {"der", "die", "das"} <= registry.conventions.stopwords
    assert registry.conventions.stopwords >= STOPWORDS


# --------------------------------------------------------------------------- #
# Add-only: stopwords and skip folders cannot be dropped
# --------------------------------------------------------------------------- #


def test_stopwords_drop_is_ignored_with_a_finding(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "stopwords": {"drop": ["a"]}})
    registry = ac.load_conventions(vault)
    assert "a" in registry.conventions.stopwords
    assert any(finding["code"] == "stopword_drop_refused" for finding in registry.findings)


def test_skip_folders_have_no_drop_key_and_a_shipped_one_cannot_be_removed(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"drop_skip_folders": ["Templates"]}})
    registry = ac.load_conventions(vault)
    assert "Templates" in registry.conventions.skip_folders
    assert any(finding["code"] == "unknown_field" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# Rejected folder-rule shapes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ("/absolute", "rule_absolute"),
        ("../escape", "rule_parent_segment"),
        ("Valid/../escape", "rule_parent_segment"),
        (".hidden", "rule_hidden_segment"),
        ("_private", "rule_hidden_segment"),
        ("Knowledge Base/Products", "rule_kb_prefixed"),
        ("Entities", "rule_entity_folder"),
        ("Entities/Sub", "rule_entity_folder"),
        ("Planning", "rule_structured_tree"),
        ("Records", "rule_structured_tree"),
        ("Records/Anything", "rule_structured_tree"),
        ("Sources", "rule_append_only"),
        ("Sources/Articles", "rule_append_only"),
        ("Evidence", "rule_append_only"),
    ],
)
def test_rejected_folder_rule_shapes(vault: Path, rule: str, code: str) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"add_folders": [rule]}}})
    registry = ac.load_conventions(vault)
    assert rule not in registry.conventions.anchors["resource"].folders
    assert any(finding["code"] == code for finding in registry.findings), registry.findings


def test_a_drive_letter_rule_is_rejected_as_absolute(vault: Path) -> None:
    # Built at runtime, never as a contiguous literal: the public-artifact
    # privacy gate's `absolute_local_path` rule flags a drive-letter path
    # written as a source literal, and this is test data, not a real path.
    rule = "".join(("C", ":", "/win"))
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"add_folders": [rule]}}})
    registry = ac.load_conventions(vault)
    assert rule not in registry.conventions.anchors["resource"].folders
    assert any(finding["code"] == "rule_absolute" for finding in registry.findings)


def test_one_bad_rule_does_not_void_the_file(vault: Path) -> None:
    _override(
        vault,
        {"schema_version": 1, "anchors": {"resource": {"add_folders": ["Planning", "Equipment"]}}},
    )
    registry = ac.load_conventions(vault)
    assert "equipment" in registry.conventions.anchors["resource"].folders
    assert any(finding["code"] == "rule_structured_tree" for finding in registry.findings)


def test_raw_material_cannot_be_made_a_hub_anchor(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"hub": {"add_folders": ["Sources/Articles"]}}})
    registry = ac.load_conventions(vault)
    assert not registry.conventions.anchors["hub"].folders
    assert any(finding["code"] == "rule_append_only" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# Case-insensitive segment matching
# --------------------------------------------------------------------------- #


def test_case_insensitive_segment_matching_pinned_on_lowercase_products(vault: Path) -> None:
    registry = ac.shipped_conventions()
    folders = registry.conventions.anchors["resource"].folders
    # The rule is stored normalised (casefolded); a lowercase `products/`
    # folder on disk must therefore match it too (working_set_index test
    # confirms the runtime side).
    assert "products" in folders


def test_override_folder_rule_is_stored_normalised(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"add_folders": ["EQUIPMENT"]}}})
    registry = ac.load_conventions(vault)
    assert "equipment" in registry.conventions.anchors["resource"].folders


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


def test_caps_with_findings(vault: Path) -> None:
    _override(
        vault,
        {"schema_version": 1, "anchors": {"resource": {"add_folders": [f"Folder{i}" for i in range(40)]}}},
    )
    registry = ac.load_conventions(vault)
    assert len(registry.conventions.anchors["resource"].folders) == ac.MAX_FOLDERS_PER_KIND
    cap_findings = [f for f in registry.findings if f["code"] == "cap_exceeded"]
    assert len(cap_findings) == 1
    assert cap_findings[0]["field"] == "anchors.resource.folders"


def test_entry_over_char_limit_is_dropped_with_a_finding(vault: Path) -> None:
    long_entry = "x" * (ac.MAX_ENTRY_CHARS + 1)
    _override(vault, {"schema_version": 1, "stopwords": {"add": [long_entry]}})
    registry = ac.load_conventions(vault)
    assert long_entry not in registry.conventions.stopwords
    assert any(finding["code"] == "entry_too_long" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# The 256 KiB refusal, before parsing
# --------------------------------------------------------------------------- #


def test_oversized_override_is_refused_before_parsing(vault: Path) -> None:
    path = ac.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema_version: 1\n" + ("#" * (ac.MAX_FILE_BYTES + 1)), encoding="utf-8")
    ac.clear_cache()
    registry = ac.load_conventions(vault)
    assert registry.source == "shipped"
    assert any(finding["code"] == "file_too_large" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# Invalid YAML fallback
# --------------------------------------------------------------------------- #


def test_invalid_yaml_falls_back_to_shipped(vault: Path) -> None:
    _override(vault, "anchors: [this is not: valid: yaml:::")
    registry = ac.load_conventions(vault)
    assert registry.source == "shipped"
    assert registry.conventions == ac.shipped_conventions().conventions
    assert any(finding["code"] == "invalid_yaml" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# Threshold range and unknown resolution keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_value", [0, 4, "two", 2.5, True])
def test_threshold_out_of_range_or_non_integer_falls_back_to_shipped(vault: Path, bad_value: object) -> None:
    _override(vault, {"schema_version": 1, "resolution": {"rare_term_max_anchors": bad_value}})
    registry = ac.load_conventions(vault)
    assert registry.conventions.rare_term_max_anchors == RARE_TERM_MAX_ANCHORS
    assert any(finding["code"] == "invalid_threshold" for finding in registry.findings)


def test_threshold_can_be_tightened(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "resolution": {"rare_term_max_anchors": 1}})
    registry = ac.load_conventions(vault)
    assert registry.conventions.rare_term_max_anchors == 1
    assert registry.findings == ()


def test_unknown_resolution_key_is_ignored_with_a_finding(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "resolution": {"bogus": 1}})
    registry = ac.load_conventions(vault)
    assert registry.conventions.rare_term_max_anchors == RARE_TERM_MAX_ANCHORS
    assert any(finding["code"] == "unknown_field" and finding["field"] == "resolution.bogus" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# The digest is over EFFECTIVE values, never file bytes
# --------------------------------------------------------------------------- #


def test_two_files_that_resolve_to_the_same_conventions_share_a_digest(vault: Path) -> None:
    _override(vault, {"schema_version": 1, "anchors": {"resource": {"add_folders": ["Equipment"]}}})
    first = ac.load_conventions(vault)

    _override(
        vault,
        "# a harmless comment that changes nothing effective\n"
        "schema_version: 1\n"
        "anchors:\n"
        "  resource:\n"
        "    add_folders: [Equipment]\n",
    )
    second = ac.load_conventions(vault)

    assert first.conventions_hash == second.conventions_hash
    assert first.conventions == second.conventions


def test_digest_changes_when_effective_conventions_change(vault: Path) -> None:
    shipped = ac.shipped_conventions()
    _override(vault, {"schema_version": 1, "stopwords": {"add": ["der"]}})
    changed = ac.load_conventions(vault)
    assert changed.conventions_hash != shipped.conventions_hash


def test_the_only_writer_is_the_governed_save(vault: Path) -> None:
    """Review-gated evolution (design.md decision 7): the loader gained
    exactly one writer, `save_conventions`, and it cannot be called without a
    reviewed proposal's expected_hash -- there is no unreviewed write."""
    writers = [name for name in dir(ac) if name.startswith(("save", "write"))]
    assert writers == ["save_conventions"]
    with pytest.raises(TypeError):
        ac.save_conventions(vault, {"schema_version": 1})


# --------------------------------------------------------------------------- #
# Anchor membership (pure function)
# --------------------------------------------------------------------------- #


def test_anchor_membership_matches_a_prefix_of_any_depth() -> None:
    registry = ac.shipped_conventions()
    conventions = registry.conventions
    assert ac.anchor_membership(
        conventions, kind="resource", directory_segments=("products",), tags=(), type_value=""
    )
    assert ac.anchor_membership(
        conventions, kind="resource", directory_segments=("products", "sub", "deep"), tags=(), type_value=""
    )
    assert not ac.anchor_membership(
        conventions, kind="resource", directory_segments=("notes",), tags=(), type_value=""
    )
    assert ac.anchor_membership(
        conventions, kind="hub", directory_segments=("anything",), tags=("hub",), type_value=""
    )
