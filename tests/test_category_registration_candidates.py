"""Deterministic reviewed `register_category` candidates from inference.

Covers OpenSpec `teach-portable-category-core` tasks 1.3 (inference half) and
3.4: recurring unregistered categories become one complete, saveable proposal
while core categories, registered extensions, defaulted rich kinds, and thin
usage stay purely observational and never mutate the active registry.
"""

from __future__ import annotations

from pathlib import Path

from exomem import memory_schema, semantic_language_registry

_DESCRIPTION = "User-defined semantic category observed across multiple pages."


def _seed(vault: Path, bodies: list[str]) -> list[Path]:
    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    pages: list[Path] = []
    for index, body in enumerate(bodies):
        path = vault / "Knowledge Base" / "Notes" / f"page-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: insight\n"
            "project: atlas\n"
            "---\n\n"
            f"# Page {index}\n\n{body}",
            encoding="utf-8",
        )
        pages.append(path)
    return pages


def test_recurring_unknown_category_becomes_one_saveable_candidate(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    pages = _seed(
        vault,
        [f"- [workflow] Stage {index} of the deploy flow\n" for index in range(5)],
    )
    before = [path.read_text(encoding="utf-8") for path in pages]

    first = memory_schema.infer_category_registry(vault, project="atlas")
    second = memory_schema.infer_category_registry(vault, project="atlas")

    assert first == second

    candidates = first["candidate_changes"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["type"] == "register_category"
    assert candidate["category_key"] == "workflow"
    assert candidate["unit_count"] == 5
    assert candidate["page_count"] == 5

    proposal = candidate["proposal"]
    assert proposal["categories"]["workflow"] == {"description": _DESCRIPTION}
    validation = semantic_language_registry.load_registry(proposal=proposal)
    assert [item for item in validation.findings if item["severity"] == "error"] == []
    assert "workflow" in validation.categories

    # The active registry is never mutated by inference.
    active = semantic_language_registry.load_registry(vault)
    assert "workflow" not in active.categories
    assert not semantic_language_registry.registry_path(vault).exists()
    assert [path.read_text(encoding="utf-8") for path in pages] == before


def test_candidate_examples_are_bounded_and_pages_deduplicate(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # Seven distinct pages, and page-0 carries two units of the same category.
    bodies = [
        "- [workflow] First unit ^a\n- [workflow] Second unit ^b\n",
        *[f"- [workflow] Stage {index}\n" for index in range(1, 7)],
    ]
    _seed(vault, bodies)

    result = memory_schema.infer_category_registry(vault, project="atlas")
    candidate = result["candidate_changes"][0]

    assert candidate["unit_count"] == 8
    assert candidate["page_count"] == 7
    assert len(candidate["examples"]) == 5
    assert candidate["examples"][0]["path"].endswith("page-0.md")
    assert candidate["examples"][0]["raw_category"] == "workflow"
    assert set(candidate["examples"][0]) == {
        "path",
        "line",
        "anchor",
        "raw_category",
        "excerpt",
        "excerpt_truncated",
    }


def test_fewer_than_five_pages_never_creates_a_candidate(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault, [f"- [workflow] Stage {index}\n" for index in range(4)])

    result = memory_schema.infer_category_registry(vault, project="atlas")

    assert result["candidate_changes"] == []
    observed = next(
        item for item in result["categories"] if item["category_key"] == "workflow"
    )
    assert observed["page_count"] == 4


def test_core_categories_and_aliases_stay_observational(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(
        vault,
        [f"- [decision] Chose option {index}\n- [decisions] Alias {index}\n" for index in range(6)],
    )

    result = memory_schema.infer_category_registry(vault, project="atlas")

    assert result["candidate_changes"] == []
    keys = {item["category_key"] for item in result["categories"]}
    assert "decision" in keys


def test_registered_extension_category_never_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed(vault, [f"- [workflow] Stage {index}\n" for index in range(6)])
    semantic_language_registry.save_registry(
        vault,
        {
            "schema_version": 1,
            "categories": {"workflow": {"description": "A reviewed workflow"}},
            "kinds": {},
        },
    )

    result = memory_schema.infer_category_registry(vault, project="atlas")

    assert result["candidate_changes"] == []
    observed = next(
        item for item in result["categories"] if item["category_key"] == "workflow"
    )
    assert observed["registry_status"] == "extension"


def test_rich_unit_defaulting_to_core_kind_never_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed(
        vault,
        [f"## Claim\n\nClaim body {index} without an explicit category.\n" for index in range(6)],
    )

    result = memory_schema.infer_category_registry(vault, project="atlas")

    assert result["candidate_changes"] == []
    observed = next(
        item for item in result["categories"] if item["category_key"] == "claim"
    )
    assert observed["page_count"] == 6
    assert observed["registry_status"] == "unregistered"


def test_candidate_proposal_preserves_existing_reviewed_definitions(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed(vault, [f"- [workflow] Stage {index}\n" for index in range(5)])
    semantic_language_registry.save_registry(
        vault,
        {
            "schema_version": 1,
            "categories": {"config": {"description": "Configuration facts"}},
            "kinds": {"protocol": {"description": "A repeatable protocol"}},
        },
    )

    candidate = memory_schema.infer_category_registry(
        vault, project="atlas"
    )["candidate_changes"][0]
    proposal = candidate["proposal"]

    assert set(proposal["categories"]) == {"config", "workflow"}
    assert proposal["kinds"] == {"protocol": {"description": "A repeatable protocol"}}
    validation = semantic_language_registry.load_registry(proposal=proposal)
    assert [item for item in validation.findings if item["severity"] == "error"] == []


def test_core_shadow_warning_does_not_suppress_unrelated_category_finding(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    # One page authoring an UNRELATED deprecated extension category.
    _seed(vault, ["- [legacy_note] Superseded label.\n"])

    proposal = {
        "schema_version": 1,
        "categories": {
            # A non-fatal core collision: this extension shadows a built-in core
            # key, which the registry reports as a `core_category_shadowed` WARNING
            # (never an error).
            "decision": {"description": "Deliberately shadows a core key."},
            # An unrelated deprecated extension, actually authored on the page.
            "legacy_note": {
                "description": "Retired note category.",
                "status": "deprecated",
                "replaced_by": "modern_note",
            },
            "modern_note": {"description": "The active replacement."},
        },
        "kinds": {},
    }

    result = memory_schema.validate_category_registry(
        vault, proposal=proposal, project="atlas"
    )

    codes = [finding["code"] for finding in result["findings"]]
    # The registry warning must be present AND must not have suppressed the
    # unrelated deprecated observation: both relevant findings survive.
    assert "core_category_shadowed" in codes
    assert "deprecated" in codes
    # The proposal carries no error findings, so the registry stays valid.
    assert result["valid"] is True
    assert [item for item in result["findings"] if item["severity"] == "error"] == []
    shadow = next(f for f in result["findings"] if f["code"] == "core_category_shadowed")
    assert shadow["severity"] == "warning"
    deprecated = next(f for f in result["findings"] if f["code"] == "deprecated")
    assert deprecated["severity"] == "warning"
    assert "legacy_note" in deprecated["detail"]
