"""The relation registry holds relation behaviours and the frontmatter binding.

The workflow under test is loading the registry: the shipped pack declares what
code does with each core relation and which frontmatter field states which
relation, and an extension inherits those through its parent and sets neither.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from exomem import relation_registry
from exomem.vocabulary import registry as vocabulary_registry

OVERLAY = """\
schema_version: 1
extensions:
  x.backs: {parent: supports, description: Backs a claim}
  x.old_backs: {parent: supports, description: Backed a claim, status: deprecated, replaced_by: x.backs}
  x.belongs_to: {parent: member_of, description: Belongs to a group}
  x.leans_on: {parent: relates_to, family: support, description: Leans on a claim}
  x.joins: {parent: relates_to, family: membership, description: Joins a group}
"""


def _registry(tmp_path: Path, overlay: str = OVERLAY) -> relation_registry.RelationRegistry:
    path = relation_registry.extension_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(overlay, encoding="utf-8")
    return relation_registry.load_registry(tmp_path)


@pytest.fixture
def ship_relations_pack() -> Iterator[Callable[[Callable[[str], str]], None]]:
    """Replace the shipped relations pack text, then restore it and every cache."""
    with pytest.MonkeyPatch.context() as patch:
        shipped = vocabulary_registry.pack_text

        def ship(edit: Callable[[str], str]) -> None:
            patch.setattr(
                vocabulary_registry,
                "pack_text",
                lambda name: edit(shipped(name)) if name == "relations.yaml" else shipped(name),
            )
            relation_registry.core_registry.cache_clear()

        yield ship
    relation_registry.core_registry.cache_clear()


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #


def test_the_order_pair_and_assesses_are_typed_edges_with_one_behaviour_each() -> None:
    """The parity module reads each other behaviour through its reader; these three have one each."""
    core = relation_registry.core_registry()

    assert core.behaviours_of("assesses") == frozenset({"names_assessed_claim"})
    assert core.behaviours_of("precedes") == frozenset({"earlier_than_target"})
    assert core.behaviours_of("follows") == frozenset({"later_than_target"})
    assert core.core["precedes"].inverse == "follows"
    assert core.core["follows"].inverse == "precedes"
    assert {core.core[key].family for key in ("precedes", "follows")} == {"order"}
    assert core.core["assesses"].family == "assessment"


def test_an_extension_has_its_parents_behaviours_and_never_those_of_its_saved_family(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    assert registry.behaviours_of("supports") == {"lifts", "bears_on", "corrects"}
    assert registry.behaviours_of("x.backs") == registry.behaviours_of("supports")
    # `x.joins` saved family `membership` and `x.leans_on` saved family `support` buy nothing.
    assert registry.behaviours_of("x.joins") == registry.behaviours_of("relates_to") == frozenset()
    assert registry.behaviours_of("x.leans_on") == frozenset()
    assert registry.behaviours_of("not.registered") == frozenset()


def test_keys_with_lists_core_keys_and_every_extension_that_inherits_the_behaviour(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    lifts = registry.keys_with("lifts")
    assert {"supports", "x.backs"} <= lifts
    assert "x.old_backs" in lifts  # deprecated, and its edges still exist
    assert not {"x.leans_on", "x.joins", "relates_to"} & lifts
    members = registry.keys_with("membership")
    assert members == {"member_of", "x.belongs_to"}  # not `x.joins`, whose saved family says so


def test_keys_with_refuses_a_behaviour_the_schema_does_not_declare(tmp_path: Path) -> None:
    """A mistyped name must not read as "no relation has it"."""
    with pytest.raises(ValueError, match="not a relation behaviour"):
        _registry(tmp_path).keys_with("settle")


def test_has_reads_a_behaviour_and_refuses_one_the_schema_does_not_declare(
    tmp_path: Path,
) -> None:
    """A mistyped name must not read false for every relation."""
    registry = _registry(tmp_path)

    assert registry.has("answers", "settles") is True
    assert registry.has("links_to", "settles") is False
    assert registry.has("x.unregistered", "settles") is False
    with pytest.raises(ValueError, match="not a relation behaviour"):
        registry.has("answers", "settle")


def test_a_reader_that_holds_a_label_or_a_stored_set_refuses_a_misspelt_behaviour(
    tmp_path: Path,
) -> None:
    """Catches the reads that took a label or a census row's stored behaviours and so read false
    for a mistyped name, where `has` raises."""
    registry = _registry(tmp_path)

    assert registry.has_label("answers", "settles") is True
    assert registry.has_label("links_to", "settles") is False
    stored = registry.behaviours_of("answers")
    assert relation_registry.has_behaviour(stored, "settles") is True
    with pytest.raises(ValueError, match="not a relation behaviour"):
        registry.has_label("answers", "settle")
    with pytest.raises(ValueError, match="not a relation behaviour"):
        relation_registry.has_behaviour(stored, "settle")


def test_the_pack_binds_seven_frontmatter_fields_and_each_bound_relation_accepts_the_origin() -> (
    None
):
    core = relation_registry.core_registry()

    assert {field: tuple(binding) for field, binding in core.frontmatter_fields().items()} == {
        "sources": ("derived_from", "forward"),
        "evidence": ("evidenced_by", "forward"),
        "evidences": ("evidenced_by", "forward"),
        "evidence_paths": ("evidenced_by", "forward"),
        "supersedes": ("supersedes", "forward"),
        "superseded_by": ("supersedes", "reverse"),
        "related": ("links_to", "forward"),
    }
    assert core.frontmatter_relation("superseded_by") == ("supersedes", "reverse")
    assert core.frontmatter_relation("title") is None
    # The loader derives `frontmatter` from the binding; the pack no longer writes it.
    for relation in ("derived_from", "evidenced_by", "supersedes", "links_to"):
        assert core.resolve(relation, origin="frontmatter").status == "core"
    assert core.core["links_to"].origins == {"semantic_relation", "wikilink", "frontmatter"}
    assert core.resolve("mentions", origin="frontmatter").status == "scope_violation"


def test_the_registry_schema_read_shows_a_core_entrys_behaviours_and_bindings(
    tmp_path: Path,
) -> None:
    entries = vocabulary_registry.load(relation_registry.SPEC, tmp_path).entries

    assert entries["supersedes"].attributes["behaviours"] == ["corrects", "replaces"]
    assert entries["supersedes"].attributes["frontmatter_fields"] == {
        "supersedes": "forward",
        "superseded_by": "reverse",
    }
    assert "behaviours" not in relation_registry.SPEC.attributes  # no delta can set it


# --------------------------------------------------------------------------- #
# An extension sets neither field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [("behaviours", ["settles"]), ("frontmatter_fields", {"claims": "forward"})],
)
def test_a_proposed_extension_that_sets_a_core_only_field_is_refused_by_name(
    field: str, value: object
) -> None:
    proposal = {
        "schema_version": 1,
        "extensions": {
            "legal.settles": {"parent": "refines", "description": "Settles a claim", field: value}
        },
    }

    findings = relation_registry.validate_proposal(proposal)

    assert [(item["code"], item["relation"], item["path"]) for item in findings] == [
        ("core_only_field", "legal.settles", f"extensions.legal.settles.{field}")
    ]
    assert "remove the field from the extension" in findings[0]["detail"]


def test_a_delta_that_sets_behaviours_on_an_extension_names_the_extension_and_the_field() -> None:
    current = {
        "schema_version": 1,
        "extensions": {"legal.settles": {"parent": "refines", "description": "Settles a claim"}},
    }
    delta = {"upsert": {"legal.settles": {"behaviours": ["settles"]}}}

    with pytest.raises(ValueError, match=r"legal\.settles.*\['behaviours'\].*remove the field"):
        relation_registry.merge_extension_delta(current, delta)


def test_the_generic_vocabulary_route_cannot_set_behaviours_on_an_extension(
    tmp_path: Path,
) -> None:
    snapshot = vocabulary_registry.load(relation_registry.SPEC, tmp_path)
    delta = {
        "upsert": {
            "legal.settles": {
                "parent": "refines",
                "description": "Settles a claim",
                "attributes": {"behaviours": ["settles"]},
            }
        }
    }

    with pytest.raises(vocabulary_registry.RegistryError, match=r"legal\.settles.*behaviours"):
        vocabulary_registry.apply_delta(relation_registry.SPEC, snapshot, delta)


def test_a_saved_overlay_that_carries_the_field_loads_with_a_warning_and_inherits_only(
    tmp_path: Path,
) -> None:
    """A later release may have written the field, so the saved vault must still load."""
    registry = _registry(
        tmp_path,
        yaml.safe_dump(
            {
                "schema_version": 1,
                "extensions": {
                    "x.backs": {
                        "parent": "supports",
                        "description": "Backs a claim",
                        "behaviours": ["settles"],
                    }
                },
            }
        ),
    )

    assert [(item["code"], item["severity"]) for item in registry.findings] == [
        ("core_only_field", "warning")
    ]
    assert registry.behaviours_of("x.backs") == registry.behaviours_of("supports")
    assert "settles" not in registry.behaviours_of("x.backs")


# --------------------------------------------------------------------------- #
# A malformed pack fails the build
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda text: text.replace("behaviours: [lifts]}", "behaviours: [lifts, gathers]}", 1),
            r"'duplicates' behaviours: \['gathers'\] are not declared",
        ),
        (
            lambda text: text.replace(
                "family: duplication, direction: symmetric, origins: [semantic_relation]",
                "family: duplication, direction: symmetric, origins: [semantic_relation, frontmatter]",
                1,
            ),
            r"'duplicates' origins: remove 'frontmatter'",
        ),
        (
            lambda text: text.replace(
                "family: question, direction: directed, origins: [semantic_relation], behaviours: [lifts]}",
                "family: question, direction: directed, origins: [semantic_relation], "
                "frontmatter_fields: {sources: forward}}",
                1,
            ),
            r"'raises_question' frontmatter_fields: 'sources' is already bound to 'derived_from'",
        ),
        (
            lambda text: text.replace("sources: forward}", "sources: sideways}", 1),
            r"'derived_from' frontmatter_fields: must map a field name to one of",
        ),
    ],
    ids=[
        "undeclared behaviour",
        "written frontmatter origin",
        "field bound twice",
        "bad direction",
    ],
)
def test_a_malformed_pack_fails_the_build_naming_the_relation_and_the_field(
    ship_relations_pack: Callable[[Callable[[str], str]], None],
    edit: Callable[[str], str],
    message: str,
) -> None:
    ship_relations_pack(edit)

    with pytest.raises(RuntimeError, match=message):
        relation_registry.core_registry()
