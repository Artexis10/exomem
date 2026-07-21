from __future__ import annotations

import hashlib
import json
from pathlib import Path

from exomem import semantic_authoring

COMPILED_DESTINATIONS = {
    "experiment": "Notes/Experiments",
    "failure": "Notes/Failures",
    "insight": "Notes/Insights",
    "pattern": "Notes/Patterns",
    "production-log": "Notes/Productions",
    "research-note": "Notes/Research",
}


def test_contract_pins_exact_language_applicability_and_findings() -> None:
    contract = semantic_authoring.build_semantic_authoring_contract().as_dict()

    assert contract["contract_id"] == "exomem.semantic-authoring"
    assert contract["version"] == 1
    assert contract["content_digest"] == (
        "sha256:de18ad66fe7778b91eefc61e0dea12642793e60a3be308eb838181107e3780b3"
    )
    assert (
        semantic_authoring.get_semantic_authoring_contract()
        is semantic_authoring.AUTHORING_CONTRACT
    )
    assert contract["compact"] == {
        "canonical_section": "## Observations",
        "syntax": "- [category] content #tags (context) ^anchor",
        "required_fields": ["category", "content"],
        "optional_suffix_order": ["tags", "context", "anchor"],
        "kind": "observation",
        "category_vocabulary": "open",
        "category_rule": (
            "Category is open vocabulary and does not infer or register a governed kind."
        ),
        "relation_rule": (
            "Compact units do not carry typed unit relations; use a canonical note-level "
            "relation or the rich form."
        ),
    }
    assert contract["rich"] == {
        "heading_syntax": "## <Governed Kind>",
        "kind_vocabulary": "governed",
        "metadata_syntax": [
            "- id: <stable-id>",
            "- category: <open category>",
            "- tags: <comma-separated tags>",
            "- context: <context>",
            "- relations: <relation-type>: [[Target]]",
        ],
        "metadata_rule": (
            "Metadata rows are optional and leading; category defaults to the governed kind."
        ),
        "body_rule": (
            "After optional leading metadata, add a blank line and a substantive Markdown body."
        ),
        "heading_boundary_rule": (
            "A heading at level N owns content until the next non-fenced heading at level "
            "N or shallower; deeper headings remain in its body."
        ),
        "relation_rule": "Typed unit relations require the rich form.",
    }

    applicability = contract["minimum_semantic_unit"]
    assert applicability == {
        "minimum_count": 1,
        "accepted_forms": ["compact", "rich"],
        "compact_preferred": True,
        "duplicate_compact_for_rich_required": False,
        "compiled_intent": (
            "canonical_compiled_destination(path) OR normalized_type in COMPILED_TYPES"
        ),
        "compiled_types": [
            "experiment",
            "failure",
            "insight",
            "pattern",
            "production-log",
            "research-note",
        ],
        "compiled_destinations": COMPILED_DESTINATIONS,
        "applies_when": [
            "the path and normalized compiled type structurally match",
            "the result is writable managed Markdown in the governed subtree",
            "the result is outside Sources, Evidence, and trash",
            "no activation exclusion applies",
            "the resolved lifecycle is active",
        ],
        "inactive_lifecycles": [
            "archived",
            "draft",
            "dropped",
            "planned",
            "superseded",
        ],
        "exemptions": [
            "arbitrary non-compiled Markdown",
            "dataset cards",
            "Evidence artifacts",
            "hubs",
            "indexes",
            "logs",
            "non-Markdown files",
            "schema and admin artifacts",
            "snapshots",
            "Sources",
            "templates",
            "trash",
        ],
        "structural_rule": (
            "Reject missing, invalid, or mismatched compiled frontmatter before evaluating "
            "the minimum-unit predicate."
        ),
        "lifecycle_rule": (
            "Check new active creates, replacements, and inactive-to-active transitions; "
            "inactive drafts may remain unit-free until activation."
        ),
        "independence_rule": (
            "Semantic-unit coverage and relation-review disposition are independent obligations."
        ),
    }

    assert contract["routes"] == {
        "new_compiled_note": "remember",
        "replacement": "replace_memory",
        "single_semantic_unit": "observe_memory",
        "small_edit_or_activation": "edit_memory",
        "tier_2": (
            "manage_memory_file create, overwrite, and append evaluate the complete resulting "
            "compiled Markdown; prefer remember or replace_memory when their typed route fits."
        ),
    }
    assert contract["findings"] == {
        "empty_rich_unit": {
            "severity": "error",
            "when": "a recognized rich heading has no substantive body",
            "remediation": (
                "Add substantive body content or remove the empty recognized heading."
            ),
        },
        "missing_semantic_unit": {
            "severity": "error",
            "when": "an applicable active compiled result has no valid non-empty unit",
            "compact_remediation": (
                "Add `## Observations` and `- [operating constraint] Keep retries bounded "
                "#reliability`."
            ),
            "rich_remediation": (
                "Alternatively add `## Decision`, a blank line, and a substantive body."
            ),
        },
    }


def test_contract_digest_is_deterministic_and_covers_normative_content() -> None:
    first = semantic_authoring.build_semantic_authoring_contract()
    second = semantic_authoring.build_semantic_authoring_contract()

    assert first.as_dict() == second.as_dict()
    assert first.content_digest.startswith("sha256:")
    payload = first.normative_dict()
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert first.content_digest == f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    changed = dict(payload)
    changed["version"] = 2
    changed_bytes = json.dumps(
        changed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert f"sha256:{hashlib.sha256(changed_bytes).hexdigest()}" != first.content_digest


def test_contract_construction_is_vault_independent(
    monkeypatch, tmp_path: Path
) -> None:
    def unexpected_read(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the canonical authoring contract must not read files")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    monkeypatch.setattr(Path, "read_bytes", unexpected_read)

    contract = semantic_authoring.build_semantic_authoring_contract()

    serialized = json.dumps(contract.as_dict(), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "project_key" not in serialized
    assert "vault" not in serialized.lower()


def test_concise_and_expanded_renderers_are_byte_stable_and_complete() -> None:
    contract = semantic_authoring.build_semantic_authoring_contract()

    concise = semantic_authoring.render_concise(contract)
    expanded = semantic_authoring.render_expanded(contract)

    assert concise == semantic_authoring.render_concise(contract)
    assert expanded == semantic_authoring.render_expanded(contract)
    assert concise.encode("utf-8") == semantic_authoring.render_concise(contract).encode(
        "utf-8"
    )
    assert concise.startswith(
        "<!-- exomem-semantic-authoring:v1 " + contract.content_digest + " -->\n"
    )
    for required in (
        "`## Observations`",
        "`- [category] content #tags (context) ^anchor`",
        "open vocabulary",
        "`observation`",
        "`## <Governed Kind>`",
        "next non-fenced heading at level N or shallower",
        "one valid, non-empty semantic unit",
        "`missing_semantic_unit`",
        "`empty_rich_unit`",
        "`remember`",
        "`replace_memory`",
        "`observe_memory`",
        "Tier 2",
        "independent obligations",
    ):
        assert required in concise
    assert expanded.startswith(concise + "\n")
    assert "### Exact applicability" in expanded
    assert "### Exempt content" in expanded
    assert "### Remediation examples" in expanded
    assert "[operating constraint]" in expanded
    assert "## Decision" in expanded
