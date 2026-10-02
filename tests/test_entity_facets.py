"""Vault-declared entity facets through the public create-entity writer.

`memory-loop` "Independent referents remain distinct from roles": one
organization may carry several governed roles without a duplicate identity per
role, and a facet the registry does not declare is refused rather than written.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import commands, entity_candidates
from exomem import link as link_module

TODAY = dt.date(2026, 9, 28)


def _write_registry(vault: Path) -> None:
    path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "site": {
                        "folder": "Sites",
                        "label": "Site",
                        "aliases": [],
                        "capture_guidance": "A stable physical site identity.",
                    }
                },
                "facets": {
                    "organization": {"roles": {"cardinality": "multi", "value": "text"}},
                    "site": {
                        "operator": {"cardinality": "single", "value": "wikilink"},
                        "established": {"cardinality": "single", "value": "date"},
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])


def _entity_pages(vault: Path) -> list[str]:
    root = vault / "Knowledge Base" / "Entities"
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.md")
        if path.name != "index.md"
    )


def test_one_identity_many_roles_no_duplicate(vault: Path) -> None:
    _write_registry(vault)
    before = _entity_pages(vault)

    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="organization",
        name="Kestrel Farm Partners",
        summary="A synthetic business that operates a site, produces and supplies.",
        facets={"roles": ["operator", "producer", "supplier", "producer"]},
    )

    page = vault / created["path"]
    frontmatter = _frontmatter(page)
    assert frontmatter["entity_type"] == "organization"
    assert frontmatter["roles"] == ["operator", "producer", "supplier"]
    assert _entity_pages(vault) == sorted(
        [*before, "Organizations/Kestrel Farm Partners.md"]
    )

    # A second role is never a second identity: the same organization under
    # the same type is refused, whatever facet the retry carries.
    with pytest.raises(ValueError, match="ENTITY_EXISTS"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="organization",
            name="Kestrel Farm Partners",
            summary="The same business, described by another role.",
            facets={"roles": ["supplier"]},
        )
    resolved = entity_candidates.resolve_entity_candidate(vault, name="Kestrel Farm Partners")
    assert resolved["status"] == "match"

    # A declared wikilink and date facet on a vault-defined type.
    site = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="site",
        name="Kestrel Upper Field",
        summary="The physical field the business operates.",
        facets={
            "operator": "[[Kestrel Farm Partners]]",
            "established": "2019-04-01",
        },
    )
    site_frontmatter = _frontmatter(vault / site["path"])
    # A bare name resolves through the writer's resolver to the canonical link.
    assert site_frontmatter["operator"] == (
        "[[Knowledge Base/Entities/Organizations/Kestrel Farm Partners]]"
    )
    assert site_frontmatter["established"] == "2019-04-01"

    snapshot = _entity_pages(vault)
    refusals = (
        ({"entity_type": "organization", "facets": {"operator": "x"}}, "ENTITY_FACET_UNDECLARED"),
        # Core writer fields are not facets and cannot be set through one.
        ({"entity_type": "person", "facets": {"affiliation": "x"}}, "ENTITY_FACET_UNDECLARED"),
        ({"entity_type": "organization", "facets": {"roles": "operator"}}, "INVALID_ENTITY_FACET"),
        ({"entity_type": "site", "facets": {"established": "April 2019"}}, "INVALID_ENTITY_FACET"),
        ({"entity_type": "site", "facets": {"operator": ["[[A]]", "[[B]]"]}}, "INVALID_ENTITY_FACET"),
    )
    for index, (args, code) in enumerate(refusals):
        with pytest.raises(ValueError, match=code):
            commands.op_connect_memory(
                vault,
                operation="create-entity",
                name=f"Refused Facet {index}",
                summary="Never written.",
                **args,
            )
    assert _entity_pages(vault) == snapshot

    # The core optional fields are unchanged beside declared facets.
    person = link_module.link(
        vault,
        entity_type="person",
        name="Wren Talbot",
        summary="A synthetic person with a core affiliation field.",
        affiliation="Kestrel Farm Partners",
        today=TODAY,
    )
    assert _frontmatter(vault / person.path)["affiliation"] == "Kestrel Farm Partners"


def test_bootstrap_names_declared_facets_only_where_declared(vault: Path) -> None:
    _write_registry(vault)

    compact = commands.op_bootstrap(vault, profile="compact", section="all")
    types = {item["id"]: item for item in compact["entity_registry"]["types"]}

    assert types["organization"]["facets"] == {"roles": "multi text"}
    assert types["site"]["facets"] == {
        "operator": "single wikilink",
        "established": "single date",
    }
    assert "facets" not in types["person"]
