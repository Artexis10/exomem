"""A saved relation registry survives a release change: core upgrade and rollback.

The workflow under test is loading a vault's saved relation overlay after the
shipped core pack changed (a newer pack adds a key, or a newer overlay carries a
field this release does not know) and writing pages against it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from exomem import commands, relation_registry
from exomem import note as note_module
from exomem.governance.principal import library_scope
from exomem.vocabulary import registry as vocabulary_registry

TODAY = dt.date(2026, 10, 26)
TARGET = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"


@pytest.fixture(autouse=True)
def owner_library_scope() -> Iterator[None]:
    with library_scope():
        yield


@pytest.fixture
def ship_relations_pack() -> Iterator[Callable[[Callable[[str], str]], None]]:
    """Replace the shipped core relations pack text, then restore it and every cache."""

    with pytest.MonkeyPatch.context() as patch:
        shipped = vocabulary_registry.pack_text

        def ship(edit: Callable[[str], str]) -> None:
            patch.setattr(
                vocabulary_registry,
                "pack_text",
                lambda name: edit(shipped(name)) if name == "relations.yaml" else shipped(name),
            )
            relation_registry.core_registry.cache_clear()
            relation_registry.clear_cache()

        yield ship
    relation_registry.core_registry.cache_clear()
    relation_registry.clear_cache()


def _add_core_keys(*keys: str) -> Callable[[str], str]:
    """A later release ships these core relations."""
    return lambda text: (
        text
        + "".join(
            f"  {key}: {{description: Newly shipped meaning, family: ownership, "
            "direction: directed, origins: [semantic_relation]}\n"
            for key in keys
        )
    )


def _save_overlay(vault: Path, extensions: dict, **root: object) -> str:
    """Write an overlay as an earlier or later release would have saved it."""
    path = relation_registry.extension_registry_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "extensions": extensions, **root}),
        encoding="utf-8",
    )
    return relation_registry.load_registry(vault).extension_hash


def _write_page(vault: Path, title: str, relation: str) -> str:
    result = note_module.note(
        vault,
        content=(
            f"# {title}\n\n## Observations\n\n- [constraint] Keep {title} bounded.\n\n"
            f"## Relations\n- {relation} [[{TARGET}]]\n"
        ),
        note_type="insight",
        title=title,
        today=TODAY,
    )
    assert (vault / result.path).is_file()
    return result.path


def test_a_saved_alias_keeps_its_meaning_across_a_core_upgrade(
    vault: Path, ship_relations_pack: Callable[[Callable[[str], str]], None]
) -> None:
    _save_overlay(
        vault,
        {
            "vault.has_pet": {
                "parent": "owns",
                "description": "Keeps an animal.",
                "aliases": ["has_pet"],
            }
        },
    )
    _write_page(vault, "Before the upgrade", "has_pet")

    ship_relations_pack(_add_core_keys("has_pet"))
    upgraded = relation_registry.load_registry(vault)

    assert [(item["code"], item["severity"]) for item in upgraded.findings] == [
        ("core_shadowed", "warning")
    ]
    assert upgraded.resolve("has_pet").canonical == "vault.has_pet"
    core_meaning = upgraded.resolve("core.has_pet")
    assert (core_meaning.canonical, core_meaning.status) == ("has_pet", "core")
    _write_page(vault, "After the upgrade", "has_pet")
    _write_page(vault, "After the upgrade, core meaning", "core.has_pet")


def test_a_new_alias_that_collides_with_a_core_key_is_still_refused(
    vault: Path, ship_relations_pack: Callable[[Callable[[str], str]], None]
) -> None:
    current = _save_overlay(
        vault,
        {
            "vault.has_pet": {
                "parent": "owns",
                "description": "Keeps an animal.",
                "aliases": ["has_pet"],
            },
            "vault.keeps": {
                "parent": "owns",
                "description": "Keeps a thing.",
                "aliases": ["keeps"],
            },
        },
    )
    ship_relations_pack(_add_core_keys("has_pet", "tends"))

    def save(delta: dict, why: str) -> None:
        commands.op_schema_memory(
            vault,
            subject="relations",
            operation="save-relations",
            proposal=delta,
            expected_hash=current,
            why=why,
        )

    # Grandfathering is by saved alias: an older extension may not gain the label,
    # and a new extension may not take it.
    older_gains_label = {"upsert": {"vault.keeps": {"aliases": ["keeps", "tends"]}}}
    newer_takes_label = {
        "upsert": {
            "vault.new_pet": {
                "parent": "owns",
                "description": "Another animal link.",
                "aliases": ["tends"],
            }
        }
    }
    for delta in (older_gains_label, newer_takes_label):
        with pytest.raises(ValueError, match="core_shadowed"):
            save(delta, "try to bind a core label")
    assert set(relation_registry.load_registry(vault).extensions) == {
        "vault.has_pet",
        "vault.keeps",
    }

    # A save that leaves the shadowed alias alone still commits.
    unrelated = {
        "upsert": {
            "vault.new_pet": {
                "parent": "owns",
                "description": "Another animal link.",
                "aliases": ["pet_link"],
            }
        }
    }
    save(unrelated, "add an unrelated meaning beside a shadowed alias")
    assert "vault.new_pet" in relation_registry.load_registry(vault).extensions


def test_a_field_this_release_does_not_know_never_blocks_page_writes(vault: Path) -> None:
    _save_overlay(
        vault,
        {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "A policy applies to a case.",
                "aliases": ["applies_to"],
                "cardinality": {"subject": "one", "object": "many"},
            }
        },
        written_by="a later release",
    )

    registry = relation_registry.load_registry(vault)
    assert {(item["code"], item["severity"]) for item in registry.findings} == {
        ("unknown_field", "warning")
    }
    _write_page(vault, "Written after a rollback", "applies_to")


def test_a_new_proposal_with_an_unknown_field_is_still_refused() -> None:
    proposal = {
        "schema_version": 1,
        "extensions": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "A policy applies to a case.",
                "cardinality": {"subject": "one"},
            }
        },
    }
    refused = relation_registry.validate_proposal(proposal)
    assert [(item["code"], item["severity"]) for item in refused] == [("unknown_field", "error")]


def test_a_saved_endpoints_declaration_this_release_cannot_read_never_blocks_page_writes(
    vault: Path,
) -> None:
    _save_overlay(
        vault,
        {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "A policy applies to a case.",
                "aliases": ["applies_to"],
                "endpoints": {"subject": "entity:*", "object": "entity:*"},
            }
        },
    )

    registry = relation_registry.load_registry(vault)
    assert {(item["code"], item["severity"]) for item in registry.findings} == {
        ("invalid_endpoints", "warning")
    }
    _write_page(vault, "Written with unreadable endpoints", "applies_to")


def test_a_grandfathered_family_mismatch_warning_does_not_block_page_writes(
    vault: Path,
) -> None:
    _save_overlay(
        vault,
        {
            "vault.holds_site": {
                "parent": "owns",
                "family": "association",
                "description": "Holds a site.",
            }
        },
    )
    assert [item["severity"] for item in relation_registry.load_registry(vault).findings] == [
        "warning"
    ]
    _write_page(vault, "With a grandfathered mismatch", "owns")


def test_the_core_spelling_reaches_core_only_when_no_saved_label_matches() -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "vault.kin_group": {
                    "parent": "relates_to",
                    "description": "Belongs to a kin group.",
                    "aliases": ["kin_group"],
                }
            },
        }
    )
    assert registry.resolve("core.owns").canonical == "owns"
    assert registry.resolve("core.kin_group").status == "unregistered"
    assert registry.resolve("kin_group").canonical == "vault.kin_group"


def test_a_new_extension_cannot_take_the_core_spelling() -> None:
    proposal = {
        "schema_version": 1,
        "extensions": {"core.owns": {"parent": "owns", "description": "Takes the core spelling."}},
    }
    assert [item["code"] for item in relation_registry.validate_proposal(proposal)] == ["collision"]


@pytest.mark.parametrize(
    ("edit", "found"),
    [
        (lambda text: text.replace(", generic: true", ""), "[]"),
        (
            lambda text: text.replace("links_to: {", "links_to: {generic: true, "),
            "['links_to', 'relates_to']",
        ),
    ],
    ids=["none", "two"],
)
def test_the_core_pack_must_mark_exactly_one_generic_relation(
    ship_relations_pack: Callable[[Callable[[str], str]], None],
    edit: Callable[[str], str],
    found: str,
) -> None:
    ship_relations_pack(edit)
    with pytest.raises(RuntimeError, match="exactly one generic relation") as raised:
        relation_registry.core_registry()
    assert found in str(raised.value)


def test_an_extension_cannot_mark_itself_generic() -> None:
    proposal = {
        "schema_version": 1,
        "extensions": {
            "vault.catch_all": {
                "parent": "relates_to",
                "description": "Anything at all.",
                "generic": True,
            }
        },
    }
    refused = relation_registry.validate_proposal(proposal)
    assert [(item["code"], item["path"]) for item in refused] == [
        ("unknown_field", "extensions.vault.catch_all.generic")
    ]
    assert relation_registry.generic_relation_key() == "relates_to"
