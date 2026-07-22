from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from exomem import observe_memory, semantic_authoring, semantic_units

COMPILED_DESTINATIONS = {
    "experiment": "Notes/Experiments",
    "failure": "Notes/Failures",
    "insight": "Notes/Insights",
    "pattern": "Notes/Patterns",
    "production-log": "Notes/Productions",
    "research-note": "Notes/Research",
}
EXPECTED_NORMATIVE_IDENTITY = (
    2,
    "sha256:b5fd73be05d4d07cf37c941625f8fe5e09a1ae2bc6f9e2db43392583e9eb2d5f",
)


def test_contract_pins_exact_language_applicability_and_findings() -> None:
    contract = semantic_authoring.build_semantic_authoring_contract().as_dict()

    assert contract["contract_id"] == "exomem.semantic-authoring"
    assert contract["version"] == 2
    assert (contract["version"], contract["content_digest"]) == (
        EXPECTED_NORMATIVE_IDENTITY
    )
    assert (
        semantic_authoring.get_semantic_authoring_contract()
        is semantic_authoring.AUTHORING_CONTRACT
    )
    assert contract["compact"] == {
        "canonical_section": "## Observations",
        "syntax": "- [category] content #tags (context) ^anchor",
        "parser_compatibility": (
            "Parse valid compact observations anywhere outside fenced code blocks."
        ),
        "canonical_authoring": (
            "Exomem writers use `-` under the canonical `## Observations` section."
        ),
        "parser_bullet_markers": ["-", "*", "+"],
        "canonical_bullet_marker": "-",
        "required_fields": ["category", "content"],
        "optional_suffix_order": ["tags", "context", "anchor"],
        "suffix_parse_rule": (
            "Parse from the end by taking anchor, then context, then trailing tags; "
            "the authored display order remains tags, context, anchor."
        ),
        "kind": "observation",
        "category": {
            "role": "the unit's one primary open-vocabulary subject or domain label",
            "vocabulary": "open",
            "lexical_rule": (
                "After trimming, use 1-64 Unicode code points; begin with a Unicode "
                "letter; then use only Unicode letters or digits, spaces, `_`, or `-`."
            ),
            "canonicalization": (
                "Apply Unicode NFKC and casefold, then collapse runs of spaces, `_`, "
                "and `-` to one `_`."
            ),
            "registry_rule": (
                "Registry alias resolution is separate from authored canonicalization; "
                "an unseen valid category needs no registry write."
            ),
        },
        "content": {
            "role": "the unit's substantive observation",
            "rule": "Use non-empty content that remains on one Markdown line.",
            "escaping_rule": (
                "Escaped parentheses, embedded hashes, and non-trailing tag-like text "
                "remain content."
            ),
        },
        "tags": {
            "role": (
                "zero or more optional secondary retrieval labels; tags do not replace "
                "the primary category or governed kind"
            ),
            "syntax": "#slug",
            "lexical_rule": (
                "Use 1-64 Unicode letters or digits, `_`, `-`, or `/`; begin with a "
                "letter or digit; do not use empty path segments or a trailing `/`."
            ),
            "position_rule": (
                "Use one contiguous trailing run after content and before optional "
                "context and anchor."
            ),
        },
        "context": {
            "role": "one optional authored qualifier for the observation",
            "syntax": "(<context>)",
            "rule": (
                "Use one balanced, unescaped parenthesized suffix preceded by whitespace."
            ),
        },
        "anchor": {
            "role": "one optional stable authored unit identifier",
            "syntax": "^anchor",
            "lexical_rule": (
                "Use 1-64 ASCII letters, digits, or hyphens and begin and end "
                "alphanumeric."
            ),
            "position_rule": "Place it at the end of the line.",
        },
        "exclusions": [
            "observation-shaped rows inside fenced code blocks",
            "task labels `[ ]`, `[x]`, `[X]`, and `[-]`",
            "reserved or punctuation-bearing bracket labels outside category grammar",
        ],
        "relation_rule": (
            "Compact units do not carry typed unit relations; use a canonical note-level "
            "relation or the rich form."
        ),
    }
    assert contract["rich"] == {
        "heading_syntax": "## <Governed Kind>",
        "kind_vocabulary": "governed",
        "metadata_syntax": [
            "- category: <open category>",
            "- id: <stable-id>",
            "- tags: <comma-separated tags>",
            "- context: <context>",
            "- relations: <relation-type>: [[Target]]",
        ],
        "accepted_metadata_order": "flexible while rows remain leading",
        "canonical_metadata_order": [
            "category",
            "id",
            "tags",
            "context",
            "relations",
        ],
        "metadata_rule": (
            "Metadata rows are optional and leading; the canonical writer emits category, "
            "id, tags, context, then relations; category defaults to the governed kind "
            "when omitted."
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
    assert contract["semantic_roles"] == {
        "category": (
            "One primary open-vocabulary label describes what a unit is about; rich "
            "category defaults to its governed kind unless explicitly overridden."
        ),
        "tag": (
            "Zero or more optional secondary retrieval labels refine lookup and never "
            "replace category or determine kind."
        ),
        "kind": (
            "The governed semantic form: compact units always use `observation`; rich "
            "units use their recognized heading kind."
        ),
    }

    applicability = contract["minimum_semantic_unit"]
    assert applicability == {
        "rule": (
            "Every new, replaced, or activated active compiled note needs at least one "
            "valid, non-empty semantic unit."
        ),
        "form_rule": (
            "Either compact or rich form satisfies the minimum; compact is preferred, "
            "and a valid rich unit does not need a duplicate compact restatement."
        ),
        "final_unit_rule": (
            "A post-activation compliant page cannot lose its final valid semantic unit."
        ),
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
            "manage_memory_file create, overwrite, and append receive the same semantic "
            "precommit contract on the complete resulting compiled Markdown; prefer remember "
            "or replace_memory when their typed route fits."
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
    changed["version"] = first.version + 1
    changed_bytes = json.dumps(
        changed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert f"sha256:{hashlib.sha256(changed_bytes).hexdigest()}" != first.content_digest


def test_normative_version_digest_identity_is_an_explicit_projection_guard() -> None:
    contract = semantic_authoring.get_semantic_authoring_contract()

    assert (contract.version, contract.content_digest) == EXPECTED_NORMATIVE_IDENTITY
    identity = f"v{contract.version} {contract.content_digest}"
    assert identity in semantic_authoring.render_concise(contract).splitlines()[0]
    assert identity in semantic_authoring.render_expanded(contract).splitlines()[0]

    changed = contract.normative_dict()
    changed["compact"]["syntax"] = "- [changed] content"
    changed["version"] = contract.version + 1
    changed_digest = hashlib.sha256(
        json.dumps(
            changed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changed_identity = f"sha256:{changed_digest}"
    assert changed_identity != EXPECTED_NORMATIVE_IDENTITY[1]

    changed_contract = replace(
        contract,
        version=changed["version"],
        content_digest=changed_identity,
        compact=changed["compact"],
    )
    assert changed_contract.version == contract.version + 1
    assert semantic_authoring.render_concise(changed_contract) != (
        semantic_authoring.render_concise(contract)
    )
    assert semantic_authoring.render_expanded(changed_contract) != (
        semantic_authoring.render_expanded(contract)
    )


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
        "<!-- exomem-semantic-authoring:v2 " + contract.content_digest + " -->\n"
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
    for exact_rule in (
        contract.compact["parser_compatibility"],
        contract.compact["canonical_authoring"],
        contract.compact["suffix_parse_rule"],
        contract.compact["category"]["lexical_rule"],
        contract.compact["category"]["canonicalization"],
        contract.compact["category"]["registry_rule"],
        contract.compact["content"]["rule"],
        contract.compact["content"]["escaping_rule"],
        contract.compact["tags"]["role"],
        contract.compact["tags"]["lexical_rule"],
        contract.compact["tags"]["position_rule"],
        contract.compact["context"]["rule"],
        contract.compact["anchor"]["lexical_rule"],
        contract.compact["anchor"]["position_rule"],
        contract.rich["metadata_rule"],
        contract.minimum_semantic_unit["compiled_intent"],
        contract.minimum_semantic_unit["lifecycle_rule"],
        contract.minimum_semantic_unit["independence_rule"],
        contract.findings["missing_semantic_unit"]["compact_remediation"],
        contract.findings["missing_semantic_unit"]["rich_remediation"],
    ):
        assert exact_rule in concise
        assert exact_rule in expanded
    for role_rule in contract.semantic_roles.values():
        assert role_rule in concise
        assert role_rule in expanded
    for page_type in contract.minimum_semantic_unit["compiled_types"]:
        assert f"`{page_type}`" in concise
    for path in contract.minimum_semantic_unit["compiled_destinations"].values():
        assert f"`{path}`" in concise
    for lifecycle in contract.minimum_semantic_unit["inactive_lifecycles"]:
        assert f"`{lifecycle}`" in concise
    assert expanded.startswith(concise + "\n")
    assert "### Exact applicability" in expanded
    assert "### Exempt content" in expanded
    assert "### Remediation examples" in expanded
    assert "[operating constraint]" in expanded
    assert "## Decision" in expanded


def test_public_semantic_language_doc_projects_the_canonical_contract() -> None:
    source = (
        Path(__file__).parents[1] / "docs" / "semantic-language.md"
    ).read_text(encoding="utf-8")
    start = "<!-- BEGIN GENERATED SEMANTIC AUTHORING CONTRACT -->\n"
    end = "<!-- END GENERATED SEMANTIC AUTHORING CONTRACT -->"

    assert source.count(start) == 1
    assert source.count(end) == 1
    projection = source.split(start, 1)[1].split(end, 1)[0].rstrip()
    assert projection == semantic_authoring.render_concise().rstrip()


def test_contract_is_deeply_immutable_and_as_dict_is_detached() -> None:
    contract = semantic_authoring.get_semantic_authoring_contract()

    with pytest.raises(FrozenInstanceError):
        contract.version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        contract.compact["category"]["role"] = "changed"  # type: ignore[index]
    with pytest.raises(AttributeError):
        contract.minimum_semantic_unit["compiled_types"].append("entity")

    detached = contract.as_dict()
    detached["compact"]["category"]["role"] = "changed"
    detached["minimum_semantic_unit"]["compiled_types"].append("entity")

    fresh = contract.as_dict()
    assert fresh["compact"]["category"]["role"] != "changed"
    assert "entity" not in fresh["minimum_semantic_unit"]["compiled_types"]


def test_contract_and_renderers_are_stable_across_process_hash_seeds() -> None:
    script = """
import json
from exomem import semantic_authoring

contract = semantic_authoring.get_semantic_authoring_contract()
print(json.dumps({
    "contract": contract.as_dict(),
    "concise": semantic_authoring.render_concise(contract),
    "expanded": semantic_authoring.render_expanded(contract),
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
"""

    outputs = []
    for seed in ("1", "777"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONIOENCODING"] = "utf-8"
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=Path(__file__).parents[1],
                env=env,
                text=True,
                encoding="utf-8",
            )
        )

    assert outputs[0] == outputs[1]


def test_compact_contract_matches_canonical_writer_and_compatible_parser() -> None:
    rendered = observe_memory._render_unit(
        kind="observation",
        category="Operating Constraint",
        content=r"Keep value#inside retry \(windows\) bounded",
        tags=("reliability", "runtime/retry"),
        context="synthetic load test",
        relations=(),
        anchor="retry-window",
    )
    authored = observe_memory._add_compact("# Synthetic note\n", rendered)

    assert "## Observations\n\n" in authored
    canonical = semantic_units.parse_semantic_units(authored, path="synthetic.md")
    compatible = semantic_units.parse_semantic_units(rendered, path="synthetic.md")

    for document in (canonical, compatible):
        assert not document.errors
        assert len(document.units) == 1
        unit = document.units[0]
        assert unit.form == "compact"
        assert unit.kind == "observation"
        assert unit.category_raw == "Operating Constraint"
        assert unit.category_key == "operating_constraint"
        assert unit.content == r"Keep value#inside retry \(windows\) bounded"
        assert unit.tags == ("reliability", "runtime/retry")
        assert unit.context == "synthetic load test"
        assert unit.anchor == "retry-window"


def test_rich_contract_matches_canonical_writer_metadata_order_and_parser() -> None:
    rendered = observe_memory._render_unit(
        kind="decision",
        category="Operating Constraint",
        content="Keep retry windows bounded.",
        tags=("reliability", "runtime"),
        context="synthetic load test",
        relations=(("supports", "[[Synthetic Target]]"),),
        anchor="retry-decision",
    )
    lines = rendered.splitlines()
    contract = semantic_authoring.get_semantic_authoring_contract()

    assert lines[1:6] == [
        "- category: Operating Constraint",
        "- id: retry-decision",
        "- tags: reliability, runtime",
        "- context: synthetic load test",
        "- relations: supports: [[Synthetic Target]]",
    ]
    assert contract.rich["canonical_metadata_order"] == (
        "category",
        "id",
        "tags",
        "context",
        "relations",
    )

    document = semantic_units.parse_semantic_units(rendered, path="synthetic.md")
    assert not document.errors
    assert len(document.units) == 1
    unit = document.units[0]
    assert unit.form == "rich"
    assert unit.kind == "decision"
    assert unit.category_raw == "Operating Constraint"
    assert unit.category_key == "operating_constraint"
    assert unit.anchor == "retry-decision"
    assert unit.metadata["tags"] == "reliability, runtime"
    assert unit.metadata["context"] == "synthetic load test"
    assert [(relation.kind, relation.target) for relation in unit.relations] == [
        ("supports", "[[Synthetic Target]]")
    ]
