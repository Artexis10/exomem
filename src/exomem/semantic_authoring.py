"""Vault-independent semantic authoring contract and deterministic projections.

The contract in this module is package policy, not vault state.  Keep its
construction pure: bootstrap, tool surfaces, and packaged skills all project
the same versioned object without reading Markdown or invoking a model.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

AUTHORING_CONTRACT_VERSION = 1
AUTHORING_CONTRACT_ID = "exomem.semantic-authoring"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SemanticAuthoringContract:
    """Deeply immutable normative contract with a content-addressed identity."""

    contract_id: str
    version: int
    content_digest: str
    compact: Mapping[str, Any]
    rich: Mapping[str, Any]
    minimum_semantic_unit: Mapping[str, Any]
    routes: Mapping[str, Any]
    findings: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "compact",
            "rich",
            "minimum_semantic_unit",
            "routes",
            "findings",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def normative_dict(self) -> dict[str, Any]:
        """Return the deterministic digest payload, excluding transport metadata."""
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "compact": _thaw(self.compact),
            "rich": _thaw(self.rich),
            "minimum_semantic_unit": _thaw(self.minimum_semantic_unit),
            "routes": _thaw(self.routes),
            "findings": _thaw(self.findings),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible projection of the canonical object."""
        out = self.normative_dict()
        out["content_digest"] = self.content_digest
        return out


def build_semantic_authoring_contract() -> SemanticAuthoringContract:
    """Construct the canonical contract without consulting a vault or model."""
    compact = {
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
    rich = {
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
    minimum_semantic_unit = {
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
        "compiled_destinations": {
            "experiment": "Notes/Experiments",
            "failure": "Notes/Failures",
            "insight": "Notes/Insights",
            "pattern": "Notes/Patterns",
            "production-log": "Notes/Productions",
            "research-note": "Notes/Research",
        },
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
    routes = {
        "new_compiled_note": "remember",
        "replacement": "replace_memory",
        "single_semantic_unit": "observe_memory",
        "small_edit_or_activation": "edit_memory",
        "tier_2": (
            "manage_memory_file create, overwrite, and append evaluate the complete resulting "
            "compiled Markdown; prefer remember or replace_memory when their typed route fits."
        ),
    }
    findings = {
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
    normative = {
        "contract_id": AUTHORING_CONTRACT_ID,
        "version": AUTHORING_CONTRACT_VERSION,
        "compact": compact,
        "rich": rich,
        "minimum_semantic_unit": minimum_semantic_unit,
        "routes": routes,
        "findings": findings,
    }
    digest = f"sha256:{hashlib.sha256(_canonical_bytes(normative)).hexdigest()}"
    return SemanticAuthoringContract(
        content_digest=digest,
        **normative,
    )


AUTHORING_CONTRACT = build_semantic_authoring_contract()


def get_semantic_authoring_contract() -> SemanticAuthoringContract:
    """Return the immutable process-wide canonical authoring contract."""
    return AUTHORING_CONTRACT


def render_concise(
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Render the complete minimum contract for schemas and compact skills."""
    compact = contract.compact
    rich = contract.rich
    minimum = contract.minimum_semantic_unit
    routes = contract.routes
    findings = contract.findings
    applies = "; ".join(minimum["applies_when"])
    exemptions = ", ".join(minimum["exemptions"])
    metadata = ", ".join(f"`{row}`" for row in rich["metadata_syntax"])
    return (
        f"<!-- exomem-semantic-authoring:v{contract.version} "
        f"{contract.content_digest} -->\n"
        "## Semantic authoring contract\n\n"
        "Every new, replaced, or activated active compiled note needs at least one "
        "valid, non-empty semantic unit. Either compact or rich form satisfies the "
        "minimum; compact is preferred, and a valid rich unit does not need a duplicate "
        "compact restatement.\n\n"
        f"- Compact: under `{compact['canonical_section']}`, write "
        f"`{compact['syntax']}`. Category is open vocabulary; compact kind is always "
        f"`{compact['kind']}`. Optional suffixes stay in tags, context, anchor order. "
        f"{compact['relation_rule']}\n"
        f"- Rich: write `{rich['heading_syntax']}` with optional leading metadata "
        f"{metadata}, then a blank line and a substantive body. Kind is governed; "
        "category defaults to kind. Typed unit relations require rich form.\n"
        f"- Rich boundary: {rich['heading_boundary_rule']} Empty recognized blocks produce "
        "`empty_rich_unit` and do not count.\n"
        f"- Applies when {applies}. {minimum['structural_rule']}\n"
        f"- Exempt content: {exemptions}.\n"
        f"- Routes: use `{routes['new_compiled_note']}` for a new compiled note, "
        f"`{routes['replacement']}` for a replacement, `{routes['single_semantic_unit']}` "
        f"for one unit, and `{routes['small_edit_or_activation']}` for a small edit or "
        f"activation. Tier 2 {routes['tier_2']}\n"
        f"- Findings: `missing_semantic_unit` means "
        f"{findings['missing_semantic_unit']['when']}; `empty_rich_unit` means "
        f"{findings['empty_rich_unit']['when']}.\n"
        f"- {minimum['independence_rule']}\n"
    )


def render_expanded(
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Render the concise contract plus deterministic reference detail and examples."""
    minimum = contract.minimum_semantic_unit
    findings = contract.findings
    destinations = "\n".join(
        f"- `{page_type}` → `{path}`"
        for page_type, path in minimum["compiled_destinations"].items()
    )
    applies = "\n".join(f"- {item}" for item in minimum["applies_when"])
    exemptions = "\n".join(f"- {item}" for item in minimum["exemptions"])
    return (
        render_concise(contract)
        + "\n### Exact applicability\n\n"
        + f"`compiled_intent(after_state) = {minimum['compiled_intent']}`. "
        + "Canonical destinations are:\n\n"
        + destinations
        + "\n\nThe minimum-unit predicate is true only when:\n\n"
        + applies
        + "\n\n"
        + minimum["lifecycle_rule"]
        + "\n\n### Exempt content\n\n"
        + exemptions
        + "\n\n### Remediation examples\n\n"
        + "Compact (preferred):\n\n"
        + "```markdown\n## Observations\n\n"
        + "- [operating constraint] Keep retries bounded #reliability\n```\n\n"
        + findings["missing_semantic_unit"]["compact_remediation"]
        + "\n\nRich alternative:\n\n"
        + "```markdown\n## Decision\n\nKeep retry windows bounded.\n```\n\n"
        + findings["missing_semantic_unit"]["rich_remediation"]
        + "\n"
    )
