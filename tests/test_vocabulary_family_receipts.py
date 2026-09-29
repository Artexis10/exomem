"""Family adapters share one bounded receipt record under their own authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, link, mutation_terminal, vocabulary_resolution
from exomem.entity_types import load_entity_types


def _record(**overrides: str) -> dict[str, str]:
    record = {
        "family": "entity_type",
        "requested": "org",
        "canonical": "organization",
        "destination": "Organizations",
        "match_kind": "alias",
        "snapshot": "a" * 64,
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize("family", ["domain", "entity_type"])
def test_public_receipt_admits_each_integrated_family(family: str) -> None:
    assert vocabulary_resolution.valid_public_resolution(_record(family=family))


@pytest.mark.parametrize("family", ["evidence_scope", "category", "relation", "", "DOMAIN"])
def test_public_receipt_refuses_a_family_without_an_adapter(family: str) -> None:
    assert not vocabulary_resolution.valid_public_resolution(_record(family=family))


def test_entity_creation_receipt_names_the_resolved_type_and_folder(vault: Path) -> None:
    created = commands.op_link(
        vault,
        entity_type="Organizations",
        name="Harbour Cooperative",
        summary="A synthetic cooperative that runs a harbour.",
    )

    resolution = created["vocabulary_resolution"]
    assert resolution == {
        "family": "entity_type",
        "requested": "Organizations",
        "canonical": "organization",
        "destination": "Organizations",
        "match_kind": "alias",
        "snapshot": load_entity_types(vault).fingerprint,
    }
    assert created["path"].startswith("Knowledge Base/Entities/Organizations/")

    terminal = mutation_terminal.committed_terminal(
        created, request_id="r-1", receipt_id="rc-1", idempotency_key=None
    )
    assert mutation_terminal.project_terminal(terminal)["vocabulary_resolution"] == resolution
    assert mutation_terminal.project_terminal(terminal, "full")["vocabulary_resolution"] == resolution


@pytest.mark.parametrize(
    ("requested", "match_kind"),
    [("person", "exact"), ("Person", "normalized"), ("individual", "alias")],
)
def test_entity_type_match_kind_follows_the_registry_resolution(
    vault: Path, requested: str, match_kind: str
) -> None:
    validated = link.link(
        vault,
        entity_type=requested,
        name=f"Synthetic Person {match_kind}",
        summary="A synthetic person for a receipt check.",
        validate_only=True,
    ).as_dict()

    assert validated["vocabulary_resolution"]["canonical"] == "person"
    assert validated["vocabulary_resolution"]["match_kind"] == match_kind
    assert validated["vocabulary_resolution"]["destination"] == "People"


def test_unknown_entity_type_keeps_the_registry_refusal(vault: Path) -> None:
    with pytest.raises(ValueError, match="ENTITY_TYPE_UNKNOWN"):
        commands.op_link(
            vault,
            entity_type="spaceship",
            name="Synthetic Vessel",
            summary="Not a registered type.",
        )
