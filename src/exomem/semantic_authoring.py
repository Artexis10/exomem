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

AUTHORING_CONTRACT_VERSION = 2
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
    semantic_roles: Mapping[str, Any]
    minimum_semantic_unit: Mapping[str, Any]
    routes: Mapping[str, Any]
    findings: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "compact",
            "rich",
            "semantic_roles",
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
            "semantic_roles": _thaw(self.semantic_roles),
            "minimum_semantic_unit": _thaw(self.minimum_semantic_unit),
            "routes": _thaw(self.routes),
            "findings": _thaw(self.findings),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible projection of the canonical object."""
        out = self.normative_dict()
        out["content_digest"] = self.content_digest
        return out


def contract_from_normative(
    normative: Mapping[str, Any],
) -> SemanticAuthoringContract:
    """Build a content-addressed contract from detached normative fields."""
    required = {
        "contract_id",
        "version",
        "compact",
        "rich",
        "semantic_roles",
        "minimum_semantic_unit",
        "routes",
        "findings",
    }
    unknown = set(normative) - required
    missing = required - set(normative)
    if missing or unknown:
        raise ValueError(
            "semantic authoring normative fields mismatch: "
            f"missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    detached = {key: _thaw(normative[key]) for key in sorted(required)}
    digest = f"sha256:{hashlib.sha256(_canonical_bytes(detached)).hexdigest()}"
    return SemanticAuthoringContract(content_digest=digest, **detached)


def build_semantic_authoring_contract() -> SemanticAuthoringContract:
    """Construct the canonical contract without consulting a vault or model."""
    compact = {
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
    rich = {
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
    semantic_roles = {
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
    minimum_semantic_unit = {
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
            "manage_memory_file create, overwrite, and append receive the same semantic "
            "precommit contract on the complete resulting compiled Markdown; prefer remember "
            "or replace_memory when their typed route fits."
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
        "semantic_roles": semantic_roles,
        "minimum_semantic_unit": minimum_semantic_unit,
        "routes": routes,
        "findings": findings,
    }
    return contract_from_normative(normative)


AUTHORING_CONTRACT = build_semantic_authoring_contract()


def get_semantic_authoring_contract() -> SemanticAuthoringContract:
    """Return the immutable process-wide canonical authoring contract."""
    return AUTHORING_CONTRACT


def bootstrap_projection(
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> dict[str, Any]:
    """Return the complete vault-independent object embedded in every profile."""
    return contract.as_dict()


def contract_identity(
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Return the stable identity marker used by deterministic projections."""
    return f"{contract.contract_id}:v{contract.version} {contract.content_digest}"


def render_tool_guidance(
    tool: str,
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Render concise tool-specific guidance from the canonical contract fields."""
    compact = contract.compact
    rich = contract.rich
    roles = contract.semantic_roles
    minimum = contract.minimum_semantic_unit
    routes = contract.routes
    findings = contract.findings
    identity = contract_identity(contract)
    compact_form = (
        f"Under `{compact['canonical_section']}`, write "
        f"`{compact['syntax']}` with an {compact['category']['vocabulary']}-vocabulary "
        f"category: {compact['category']['role']}. Category: {roles['category']} "
        f"Tag: {roles['tag']} Kind: {roles['kind']}"
    )
    rich_form = (
        f"Rich form uses `{rich['heading_syntax']}`. {rich['body_rule']} "
        f"{rich['heading_boundary_rule']}"
    )
    refusal = (
        f"`missing_semantic_unit` means {findings['missing_semantic_unit']['when']}. "
        f"Compact remediation: {findings['missing_semantic_unit']['compact_remediation']} "
        f"Rich remediation: {findings['missing_semantic_unit']['rich_remediation']} "
        f"`empty_rich_unit` means {findings['empty_rich_unit']['when']}. "
        f"{findings['empty_rich_unit']['remediation']}"
    )
    common = " ".join(
        (
            minimum["rule"],
            minimum["form_rule"],
            minimum["lifecycle_rule"],
            minimum["final_unit_rule"],
            compact_form,
            rich_form,
            refusal,
        )
    )

    if tool in {"remember", "replace_memory", "observe_memory", "edit_memory"}:
        guidance = common
    elif tool == "manage_memory_file":
        guidance = (
            f"{routes['tier_2']} {common}"
        )
    else:
        raise ValueError(f"no semantic-authoring projection for tool {tool!r}")
    return f"Semantic authoring [{identity}]: {guidance}"


def render_parameter_guidance(
    tool: str,
    parameter: str,
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Return canonical guidance for a tool parameter, or an empty string."""
    if (tool, parameter) == ("observe_memory", "content"):
        content = contract.compact["content"]
        return (
            f"Provide only {content['role']}; {content['rule']} Pass the category and "
            "optional suffix values through the sibling `category`, `tags`, and `context` "
            "fields. Do not include the Markdown row wrapper in `content`."
        )
    if (tool, parameter) in {
        ("remember", "content"),
        ("replace_memory", "content"),
        ("edit_memory", "operation"),
        ("manage_memory_file", "operation"),
        ("manage_memory_file", "content"),
    }:
        return render_tool_guidance(tool, contract)
    return ""


def project_tool_description(
    tool: str,
    description: str,
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Insert canonical guidance where doc parsers preserve tool prose."""
    guidance = render_tool_guidance(tool, contract)
    args_marker = "\nArgs:"
    if args_marker in description:
        preamble, remainder = description.split(args_marker, 1)
        return f"{preamble.rstrip()}\n\n{guidance}\n{args_marker}{remainder}"
    return f"{description.rstrip()}\n\n{guidance}\n"


def render_concise(
    contract: SemanticAuthoringContract = AUTHORING_CONTRACT,
) -> str:
    """Render the complete minimum contract for schemas and compact skills."""
    compact = contract.compact
    rich = contract.rich
    roles = contract.semantic_roles
    minimum = contract.minimum_semantic_unit
    routes = contract.routes
    findings = contract.findings
    applies = "; ".join(minimum["applies_when"])
    exemptions = ", ".join(minimum["exemptions"])
    compact_exclusions = "; ".join(compact["exclusions"])
    compiled_types = ", ".join(
        f"`{page_type}`" for page_type in minimum["compiled_types"]
    )
    compiled_destinations = ", ".join(
        f"`{page_type}` → `{path}`"
        for page_type, path in minimum["compiled_destinations"].items()
    )
    inactive = ", ".join(
        f"`{lifecycle}`" for lifecycle in minimum["inactive_lifecycles"]
    )
    metadata = ", ".join(f"`{row}`" for row in rich["metadata_syntax"])
    return (
        f"<!-- exomem-semantic-authoring:v{contract.version} "
        f"{contract.content_digest} -->\n"
        "## Semantic authoring contract\n\n"
        f"{minimum['rule']} {minimum['form_rule']}\n\n"
        "Semantic roles:\n\n"
        f"- Category: {roles['category']}\n"
        f"- Tag: {roles['tag']}\n"
        f"- Kind: {roles['kind']}\n\n"
        f"Compact grammar: `{compact['syntax']}`. {compact['parser_compatibility']} "
        f"{compact['canonical_authoring']} Parser bullet markers are "
        f"{', '.join(f'`{marker}`' for marker in compact['parser_bullet_markers'])}; "
        f"the canonical marker is `{compact['canonical_bullet_marker']}`. "
        f"{compact['suffix_parse_rule']} Category uses open vocabulary.\n\n"
        f"- Compact category: {compact['category']['role']}. "
        f"{compact['category']['lexical_rule']} "
        f"{compact['category']['canonicalization']} "
        f"{compact['category']['registry_rule']}\n"
        f"- Compact content: {compact['content']['role']}. "
        f"{compact['content']['rule']} {compact['content']['escaping_rule']}\n"
        f"- Compact tags: {compact['tags']['role']}. Write "
        f"`{compact['tags']['syntax']}`. {compact['tags']['lexical_rule']} "
        f"{compact['tags']['position_rule']}\n"
        f"- Compact context: {compact['context']['role']}. Write "
        f"`{compact['context']['syntax']}`. {compact['context']['rule']}\n"
        f"- Compact anchor: {compact['anchor']['role']}. Write "
        f"`{compact['anchor']['syntax']}`. {compact['anchor']['lexical_rule']} "
        f"{compact['anchor']['position_rule']}\n"
        f"- Compact exclusions: {compact_exclusions}. {compact['relation_rule']}\n"
        f"- Rich: write `{rich['heading_syntax']}` with optional leading metadata "
        f"{metadata}. {rich['metadata_rule']} Accepted metadata order is "
        f"{rich['accepted_metadata_order']}. {rich['body_rule']} "
        f"{rich['relation_rule']}\n"
        f"- Rich boundary: {rich['heading_boundary_rule']} `empty_rich_unit` means "
        f"{findings['empty_rich_unit']['when']}; "
        f"{findings['empty_rich_unit']['remediation']}\n"
        f"- Exact applicability: `compiled_intent(after_state) = "
        f"{minimum['compiled_intent']}`. `COMPILED_TYPES` contains exactly "
        f"{compiled_types}, with canonical destinations {compiled_destinations}. "
        f"{minimum['structural_rule']} The minimum predicate applies when "
        f"{applies}. Inactive lifecycle values are {inactive}. "
        f"{minimum['lifecycle_rule']}\n"
        f"- Existing active pages: {minimum['final_unit_rule']}\n"
        f"- Exempt content: {exemptions}.\n"
        f"- Routes: use `{routes['new_compiled_note']}` for a new compiled note, "
        f"`{routes['replacement']}` for a replacement, `{routes['single_semantic_unit']}` "
        f"for one unit, and `{routes['small_edit_or_activation']}` for a small edit or "
        f"activation. Tier 2 {routes['tier_2']}\n"
        f"- Findings: `missing_semantic_unit` means "
        f"{findings['missing_semantic_unit']['when']}; `empty_rich_unit` means "
        f"{findings['empty_rich_unit']['when']}. "
        f"{findings['empty_rich_unit']['remediation']}\n"
        f"- Compact remediation: "
        f"{findings['missing_semantic_unit']['compact_remediation']}\n"
        f"- Rich remediation: {findings['missing_semantic_unit']['rich_remediation']}\n"
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
