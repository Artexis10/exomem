"""Single declarative command registry — the genuine source of truth for every
surface (MCP tools, the REST facade, the OpenAPI document, and the CLI).

Each operation is one `Command`: its canonical name, the leaf callable
`leaf(vault_root, **kwargs)` (the former per-surface wrapper body, lifted to
module level so it can be shared), declarative `Param` specs (drive REST
coercion + CLI argparse + OpenAPI), the set of surfaces it is exposed on, and the
full description Claude reads (the leaf's own docstring).

MCP tools are generated via `bind_vault`, which presents each leaf's signature
(minus the injected `vault_root` / `source_schema`) and its docstring to FastMCP
exactly as a hand-written wrapper would — so the generated tool's input-schema and
description are byte-identical to the pre-registry tool (pinned by
`tests/test_mcp_schema_fidelity.py`). Any tool whose schema cannot be reproduced
cleanly (the env-bound `mint_*` tools) — or that needs a per-vault description
(`note`'s live project-key hint) — stays hand-registered in `server.py` and is
named in `HAND_REGISTERED_EXCEPTIONS`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, NotRequired

from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image as FastMCPImage
from mcp.types import TextContent
from pydantic import Field, StrictInt
from typing_extensions import TypedDict

from . import add as add_module
from . import adopt as adopt_module
from . import adoption_proposals as adoption_proposals_module
from . import adoption_run as adoption_run_module
from . import append_to_file as append_to_file_module
from . import attention as attention_module
from . import audit as audit_module
from . import audit_fix as audit_fix_module
from . import capabilities as capabilities_module
from . import compile_proposal as compile_proposal_module
from . import context_pack as context_pack_module
from . import corpus_aware as corpus_aware_module
from . import create_directory as create_directory_module
from . import create_file as create_file_module
from . import delete_directory as delete_directory_module
from . import delete_file as delete_file_module
from . import edit as edit_module
from . import edit_operations as edit_operations_module
from . import entity_candidates as entity_candidates_module
from . import entity_types as entity_types_module
from . import epistemic_graph as epistemic_graph_module
from . import evolution as evolution_module
from . import find as find_module
from . import get_frontmatter as get_frontmatter_module
from . import get_page as get_page_module
from . import knowledge_packs as knowledge_packs_module
from . import link as link_module
from . import link_summary as link_summary_module
from . import list_directory as list_directory_module
from . import list_inbound_links as list_inbound_links_module
from . import list_trash as list_trash_module
from . import memory_context as memory_context_module
from . import memory_refs as memory_refs_module
from . import memory_schema as memory_schema_module
from . import move_file as move_file_module
from . import multi_edit as multi_edit_module
from . import note as note_module
from . import observe_memory as observe_memory_module
from . import overview as overview_module
from . import provenance as provenance_module
from . import query_data as query_data_module
from . import query_log, retrieval_models, semantic_census, upload_tokens, vault
from . import readiness as readiness_module
from . import reconcile as reconcile_module
from . import recover_from_trash as recover_from_trash_module
from . import relation_queue as relation_queue_module
from . import relation_registry as relation_registry_module
from . import replace as replace_module
from . import retrieval_explain as retrieval_explain_module
from . import review_context as review_context_module
from . import review_state as review_state_module
from . import semantic_authoring as semantic_authoring_module
from . import semantic_language_registry as semantic_language_registry_module
from . import semantic_unit_read as semantic_unit_read_module
from . import set_frontmatter_field as set_frontmatter_field_module
from . import set_take as set_take_module
from . import traversal_profiles as traversal_profiles_module
from . import workflow_skills as workflow_skills_module
from .command_surface import (
    DESTRUCTIVE_OPS,  # noqa: F401 - re-exported for server.py
    GUARDED_WRITE_FIELDS,  # noqa: F401 - re-exported for server.py
    Command,
    Param,  # noqa: F401 - re-exported for server.py
    bind_vault,  # noqa: F401 - re-exported for server.py
    mcp_tool_annotations,  # noqa: F401 - re-exported for server.py
)
from .command_surface import (
    derive_params as _derive_params,
)
from .command_surface import (
    parse_args_help as _parse_args_help,  # noqa: F401 - re-exported for server.py
)
from .command_surface import (
    type_tag as _type_tag,  # noqa: F401 - re-exported for server.py
)
from .entity_types import EntityTypeId
from .kbdir import kb_dirname
from .vault import (
    VaultPathError,
    resolve_under_vault,
)

_link_summary = link_summary_module.link_summary
_CONNECT_MEMORY_DEFAULT_OPERATION = "suggest-links"
_ADOPT_VAULT_DEFAULT_MODE = "scan-only"
_AuditSampleLimit = Annotated[
    StrictInt,
    Field(
        ge=0,
        le=audit_module.MAX_LEGACY_SAMPLE_LIMIT,
        description="Audit legacy-backlog sample count; integer from 0 to 50.",
    ),
]
_RerankCandidateLimit = Annotated[
    StrictInt,
    Field(
        ge=1,
        le=find_module.MAX_RERANK_CANDIDATES,
        description=(
            "Maximum fused candidates passed to the reranker; strict integer "
            "from the effective result limit through 300."
        ),
    ),
]

# Keep commands.py as the public command-surface facade for server, CLI, docs,
# and tests while the implementation lives in command_surface.py.
_COMMAND_SURFACE_EXPORTS = (
    DESTRUCTIVE_OPS,
    GUARDED_WRITE_FIELDS,
    Param,
    bind_vault,
    mcp_tool_annotations,
)


def _preserve_module():
    from . import preserve as preserve_module

    return preserve_module


def _video_frames_module():
    from . import video_frames as video_frames_module

    return video_frames_module



FindHit = retrieval_models.PageHit
RetrievalHit = retrieval_models.RetrievalHit
FindEnvelope = retrieval_models.FindEnvelope


class SearchResult(TypedDict):
    id: str
    title: str
    url: str
    metadata: dict[str, str]


class SearchResponse(TypedDict):
    results: list[SearchResult]


class FetchResponse(TypedDict):
    id: str
    title: str
    text: str
    url: str
    metadata: NotRequired[dict[str, str]]


class GetResponse(TypedDict):
    path: str
    frontmatter: dict[str, Any]
    body: NotRequired[str]
    content_hash: NotRequired[str]
    mtime: NotRequired[float]
    content: NotRequired[str]
    has_frontmatter: NotRequired[bool]
    body_truncated: NotRequired[bool]
    body_chars: NotRequired[int]
    history: NotRequired[list[dict[str, Any]]]
    links: NotRequired[dict[str, Any]]


# ----- op-leaves: the former per-surface wrapper bodies (vault_root injected) -----
# Extracted verbatim from server.py's build_server; their docstrings ARE the tool
# descriptions Claude reads (byte-pinned by tests/test_mcp_schema_fidelity.py).


def op_bootstrap(
    vault_root: Path,
    profile: str = "compact",
    workflow: str | None = None,
) -> dict:
    """Return Exomem's versioned operating contract for generic MCP clients.

    Call this once at the start of a session when the client does not have the
    Exomem Claude Skill loaded. It teaches the agent how to use the tools: when
    to search, when to save, how to interpret scoped misses, which `find` knobs
    are cheap vs diagnostic, how compiled notes differ from raw sources/evidence,
    and how Exomem differs from built-in AI memory. The payload is deterministic
    instruction plus local compute policy and product-surface metadata; it does
    not inspect or summarize vault content.

    Args:
        profile: "compact" (default), "full", or "diagnostics". Compact is
            enough for normal clients. Full adds examples. Diagnostics adds
            performance interpretation guidance.
        workflow: Optional caller-selected workflow label. Returned as context
            only; it does not change server behavior.

    Returns:
        A structured, versioned contract with workflow, search, save, upload,
        and performance guidance for MCP clients.
    """
    if profile not in ("compact", "full", "diagnostics"):
        raise ValueError(
            "bootstrap: profile must be 'compact', 'full', or 'diagnostics', "
            f"got {profile!r}"
        )

    try:
        package_version = version("exomem")
    except PackageNotFoundError:
        package_version = "0+unknown"

    from . import mode as mode_module
    from . import tool_surface as tool_surface_module

    compute_policy = mode_module.resolved()
    active_descriptor = _active_bootstrap_descriptor()
    active_product_names = frozenset(active_descriptor.product_commands)
    requested_workflow = workflow.strip() if workflow and workflow.strip() else "general"
    selected_packs = knowledge_packs_module.selected_pack_state(vault_root)
    # Project the semantic authoring contract ONCE at the selected profile and
    # reuse it everywhere in the payload. A compact bootstrap must stay compact
    # through the whole payload, so the nested authoring_contract projection can
    # never fall back to the full profile and leak the rich example.
    semantic_authoring_projection = semantic_authoring_module.bootstrap_projection(
        profile=profile
    )
    payload: dict = {
        "contract_version": "2026-07-19.1",
        "profile": profile,
        "server": {
            "name": "exomem",
            "version": package_version,
            "kb_dir": kb_dirname(),
            "pure_substrate": True,
            "content_included": False,
            "published_mcp_tool_surface_sha256": tool_surface_module.sha256(),
            "published_mcp_tool_surface_scope": "packaged-full-mcp-discovery",
            "canonical_mcp_tool_surface": {
                "scope": "packaged-full-mcp-discovery",
                "sha256": tool_surface_module.sha256(),
            },
            "compute_policy": compute_policy,
        },
        "active_capabilities": active_descriptor.as_metadata(),
        "semantic_authoring": semantic_authoring_projection,
        "memory_model": {
            "built_in_ai_memory": (
                "Use as short-term or behavioural memory for user preferences, working "
                "rules, routing instructions, and current working context."
            ),
            "exomem": (
                "Use as long-term governed memory for durable governed knowledge: "
                "sources, proof/evidence, history, decisions, records, review, and "
                "compiled conclusions."
            ),
        },
        "knowledge_packs": {
            "available": knowledge_packs_module.list_builtin_packs(),
            "selected": selected_packs,
            "selection_rule": (
                "Packs are product guidance only. They help route simple user intent "
                "into typed tools; they do not create folders, migrate files, or bypass governance."
            ),
        },
        "entity_registry": {
            "types": [
                {
                    "id": definition.id,
                    "label": definition.label,
                    "folder": definition.folder,
                    "aliases": list(definition.aliases),
                    "capture_guidance": definition.capture_guidance,
                }
                for definition in entity_types_module.ENTITY_TYPE_REGISTRY
            ],
            "capture_rule": (
                "After durable work, run one bounded exact-match-first entity pass. "
                "Create only stable, recurring identities; update an existing entity only "
                "with new durable facts or relations; skip incidental mentions."
            ),
            "candidate_route": "connect_memory(operation='resolve-entity')",
        },
        "workflow": {
            "requested": requested_workflow,
            "loop": [
                "bootstrap",
                "adopt_vault or browse_memory when first seeing an existing vault",
                "ask_memory for cheap product recall",
                "read_memory or ask_memory(deep=true) when more context is needed",
                (
                    "show the note title by default in normal user-facing prose and do not "
                    "expose the raw canonical ref by default; add the current vault-relative "
                    "path for clarity or disambiguation, or use the path or file name as the "
                    "visible fallback when the title is unusable; keep the canonical "
                    "exomem://memory/<uuid> ref for tool arguments, durable machine state, "
                    "and machine-readable automation; show it only when the user explicitly "
                    "asks for it or the identifier itself is being inspected or debugged; "
                    "do not embed the canonical ref as a Markdown link target; use a plain "
                    "title-first citation"
                ),
                "reason in the agent",
                "use connect_memory(operation='suggest-links' or 'suggest-relations') before important compiled writes",
                "remember or replace_memory for page-level conclusions; observe_memory for one semantic unit; edit_memory for other small page corrections",
                "read warnings/suggestions/write_feedback and follow up on unresolved links or duplicate warnings",
            ],
            "save_rule": (
                "Save durable decisions, solved problems, diagnosed failures, "
                "and reusable patterns as compiled notes; keep raw artifacts in "
                "Sources and case-bound proof in Evidence."
            ),
            "miss_rule": (
                "An empty find result means not found in that query/scope. Try synonyms, "
                "related terms, compact recall, or scope='vault' before concluding absence."
            ),
        },
        "workflow_skills": workflow_skills_module.bootstrap_entries(),
        "authoring_contract": {
            "canonical_loop": [
                "ask_memory for relevant prior notes and sources",
                "read_memory enough context; use ask_memory(deep=true) for synthesis",
                "draft the smallest durable compiled conclusion",
                "run connect_memory(operation='suggest-links') and, when directional meaning matters, 'suggest-relations' on the draft",
                "write accepted note-level edges under `## Relations` as `- relation_type [[Target]]`",
                "write with remember, observe_memory, edit_memory, replace_memory, capture_source, preserve_evidence, or connect_memory as appropriate",
                "inspect warnings, suggestions, and write_feedback from the write result",
                "apply any accepted links through edit_memory",
                "report the written path",
            ],
            "route_by_intent": {
                "raw_material": "capture_source",
                "raw_evidence_or_artifact": "preserve_evidence or transfer_artifact",
                "new_durable_conclusion": "remember",
                "small_correction": "edit_memory",
                "semantic_unit_mutation": "observe_memory",
                "substantial_rewrite": "replace_memory",
                "stable_named_entity": "connect_memory(operation='create-entity')",
            },
            "preflight": {
                "connect_memory": "standard read-only suggest-links/suggest-relations check before a compiled note write",
                "near_duplicate_warnings": "if they fire, consider edit or replace instead of a parallel page",
            },
            "post_write": {
                "remember_suggestions": "non-binding related pages returned by remember(suggestions=true)",
                "write_feedback": "structural feedback returned by remember(): semantic blocks, typed note/block relations, generic/source links, relation debt, unresolved wikilinks, and next actions",
                "accepted_links": "persist only through edit_memory/remember/replace_memory; never auto-write suggestions",
            },
            "note_type_recipes": {
                "research-note": "Project-scoped finding with Question, Findings, and typed Relations.",
                "insight": "Cross-cutting claim with Claim, Why it holds, and typed Relations.",
                "failure": "Failure mode with mechanism, detection, mitigation, and typed Relations.",
                "pattern": "Reusable solution with Problem, Solution, When to use, When not to use, and typed Relations.",
                "experiment": "Primary protocol with Hypothesis, Protocol, Results, and Conclusion.",
                "production-log": "Creative artifact record with Frame, Artifact, Outcomes, Reflection, and typed Relations.",
            },
            "semantic_units": {
                "contract": semantic_authoring_projection,
                "compact_syntax": semantic_authoring_module.AUTHORING_CONTRACT.compact[
                    "syntax"
                ],
                "compact_kind": semantic_authoring_module.AUTHORING_CONTRACT.compact[
                    "kind"
                ],
                "category_rule": semantic_authoring_module.AUTHORING_CONTRACT.semantic_roles[
                    "category"
                ],
                "rich_form": semantic_authoring_module.AUTHORING_CONTRACT.rich[
                    "heading_syntax"
                ],
                "rich_relation_rule": semantic_authoring_module.AUTHORING_CONTRACT.rich[
                    "relation_rule"
                ],
                "mutation_rule": semantic_authoring_module.AUTHORING_CONTRACT.routes[
                    "single_semantic_unit"
                ],
                "drift_guards": (
                    "update/remove require the current parent content hash and unit fingerprint"
                ),
            },
            "reviewed_creation": {
                "validate_only": (
                    "call the intended creation writer with validate_only=true and retain "
                    "its draft_id, draft_hash, candidate, and semantic feedback"
                ),
                "commit": (
                    "after review, call the same writer with the unchanged draft_id and "
                    "draft_hash; changed candidates must be validated again"
                ),
                "reviewed_none": (
                    "when a governed qualifying relation has no accepted edge, use the "
                    "returned relation review hash and an explicit reason; never fabricate "
                    "a none decision or infer review from missing relations"
                ),
                "adoption_handoff": (
                    "adopt_vault(mode='compile-selected') returns a proposal only; review "
                    "it, then call remember() so normal semantic precommit still applies"
                ),
            },
        },
        "tool_defaults": {
            "normal_lookup": {
                "tool": "ask_memory",
                "args": {"detail": "compact", "rerank": False},
                "when": "normal cheap product recall",
            },
            "metadata_lookup": {
                "tool": "ask_memory",
                "args": {"detail": "compact", "rerank": False},
                "when": "caller needs the richer find filters or compact stubs",
            },
            "reasoning_lookup": {
                "tool": "ask_memory",
                "args": {"deep": True},
            },
            "adopt_existing_vault": {
                "tool": "adopt_vault",
                "args": {"mode": "scan-only"},
                "when": "first-run import/adoption of an existing vault",
            },
            "diagnostics_lookup": {
                "tool": "ask_memory",
                "args": {
                    "detail": "compact",
                    "include_timings": True,
                    "rerank": True,
                },
            },
            "read_full_page": {"tool": "read_memory", "when": "after choosing a hit"},
            "write_compiled_note": {
                "tool": "remember",
                "when": "new durable conclusion",
            },
            "minor_edit": {"tool": "edit_memory", "when": "small correction to an existing page"},
            "mutate_semantic_unit": {
                "tool": "observe_memory",
                "when": "add, update, remove, or validate one compact/rich semantic unit",
            },
            "supersede": {
                "tool": "replace_memory",
                "when": "substantial rewrite of compiled material",
            },
            "binary_upload": {
                "tool": "transfer_artifact",
                "endpoint": "/upload",
                "fields": ["file", "scope", "category", "description", "text"],
            },
        },
        "performance_profiles": {
            "normal": {
                "ask_memory_args": {"detail": "compact", "rerank": False},
                "interpretation": "cheap product recall; follow with read_memory if needed",
            },
            "reasoning": {
                "ask_memory_args": {"deep": True},
                "interpretation": "bounded context assembly for synthesis",
            },
            "diagnostics": {
                "ask_memory_args": {"include_timings": True, "rerank": True},
                "interpretation": (
                    "timings measure retrieval stages; unset rerank is mode-aware "
                    "and CPU steady-state modes keep auto-rerank off; compute_policy "
                    "explains quiet/normal/performance mode separately from rerank/pack knobs"
                ),
            },
        },
        "search_guidance": {
            "prefer_compiled_default": True,
            "compiled_types": ["research-note", "insight", "failure", "pattern", "entity"],
            "raw_types": ["source", "evidence"],
            "semantic_recall": {
                "result_levels": ["page", "unit", "mixed"],
                "structured_filters": (
                    "use filters for typed page.* or unit.* predicates; categories and "
                    "kinds are shortcuts compiled into the same bounded filter plan"
                ),
                "filter_only": (
                    "an empty query with filters is a filter-only lookup ordered by the "
                    "documented filtered-most-recent tuple, not a fabricated text match"
                ),
                "explanation": (
                    "set explain=true only when ranking interpretation is useful; it adds "
                    "a bounded retrieval profile and per-hit evidence without changing recall"
                ),
                "score_interpretation": {
                    "bm25": "backend relevance value; interpret using the returned direction and range",
                    "cosine": "vector similarity measurement, not probability",
                    "rrf": "rank-fusion contribution computed only for participating fused lanes",
                    "reranker": "separate raw and adjusted reranker values when reranking runs",
                    "final_rank": "the final deterministic order after boosts, reranking, and tie-breaks",
                    "rule": "none of these metrics is confidence; compare only within its labelled profile",
                },
            },
            "retry_examples": [
                "try synonyms and singular/plural forms",
                "try adjacent domain terms",
                "try scope='vault' if Knowledge Base recall is sparse",
                "use adopt_vault(mode='scan-only') before proposing migration/copy actions",
                "try ask_memory(deep=true) for synthesis instead of many read_memory calls",
            ],
        },
        "simple_actions": simple_action_catalog(
            selected_packs, available_tools=active_product_names
        ),
        "common_actions": list(simple_action_names()),
        "front_door_actions": product_front_door_catalog(
            selected_packs, available_tools=active_product_names
        ),
        "product_commands": product_tool_catalog(
            active_product_names, callable_tools=active_descriptor.callable_commands
        ),
        "tool_catalog": product_tool_catalog(
            active_product_names, callable_tools=active_descriptor.callable_commands
        ),
        "common_tools": [
            "adopt_vault",
            "browse_memory",
            "ask_memory",
            "read_memory",
            "remember",
            "edit_memory",
            "observe_memory",
            "replace_memory",
            "connect_memory",
            "transfer_artifact",
            "read_media",
        ],
    }

    if profile in ("full", "diagnostics"):
        payload["examples"] = [
            {
                "goal": "safe existing-vault adoption",
                "call": "adopt_vault(mode='scan-only')",
            },
            {
                "goal": "cheap proactive recall",
                "call": "ask_memory(query='...', detail='compact', rerank=false)",
            },
            {
                "goal": "reason across top matches",
                "call": "ask_memory(query='...', deep=true)",
            },
            {
                "goal": "capture a durable conclusion",
                "call": "remember(note_type='research-note'|'insight'|..., title='...', content='...')",
            },
        ]
    if profile == "diagnostics":
        payload["diagnostics"] = {
            "timings": (
                "Use include_timings=true when discussing latency. Rerank and pack "
                "can dominate wall time; unset rerank is mode-aware, while explicit "
                "rerank=true may still spend CPU seconds for precision."
            ),
            "compute_modes": {
                "quiet": "CPU/low-power, no preload, releases models when idle",
                "normal": "safe default, CPU steady-state with lexical recall ready first",
                "performance": "GPU-preferred steady-state when available",
            },
            "upload_response": (
                "/upload returns stored_path, hash, media_id, size, and sidecar_path "
                "so the agent can report stored artifacts exactly."
            ),
        }
    return _filter_bootstrap_payload(payload, active_descriptor)


def op_find(
    vault_root: Path,
    query: str = "",
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    result_level: str = "auto",
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    graph: bool = True,
    rerank: bool | None = None,
    rerank_max_candidates: _RerankCandidateLimit | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    pack: bool = False,
    graph_enrich: bool = False,
    detail: str = "full",
    include_timings: bool = False,
    explain: bool = False,
) -> list[RetrievalHit] | FindEnvelope:
    """Search / find / look up / query / retrieve / recall pages in the Knowledge Base (KB vault): notes, sources, insights, failures, patterns, experiments, entities. Hybrid semantic + keyword search, read-only. Filters are AND'd; tag/project lists are OR'd within.

    Args:
        query: Free-text search string. In "hybrid"/"vector" mode it's
            embedded with bge-base for semantic recall. In "keyword" mode
            it's tokenized on whitespace and every token must appear in
            title or body (any order) — `contract employment` matches a
            page about "employment contract". Empty string always falls
            back to "most-recent filtered" behaviour regardless of mode.
        types: Filter to these page types (source, research-note, insight, failure, pattern, experiment, production-log, entity).
        projects: Filter to pages whose `project` or `projects:` includes any of these keys.
        tags: Filter to pages whose `tags:` includes any of these (case-insensitive).
        speakers: Filter to diarized media whose `speakers:` frontmatter includes any of
            these named speakers (case-insensitive) — e.g. "what did Alice say about X".
            AND'd with the query/other filters; OR'd within the list.
        file_types: Scope results to these artifact kinds — note, pdf, image,
            audio, video, csv, json, tsv. A binary surfaces under its media
            kind (pdf/image/...); a data file under its dataset card's format
            (csv/json). Omit to return ALL kinds (the default — search never
            hides a type unless you ask).
        exclude_file_types: Drop these kinds from results (same vocabulary).
        categories: Semantic-unit category shortcuts, such as config or rule.
        kinds: Semantic-unit kind shortcuts, such as decision or claim.
        filters: Structured page/unit metadata filters.
        result_level: auto, page, unit, or mixed. Auto preserves page recall
            unless semantic-unit filters request independently ranked units.
        limit: Max hits to return. Default 15, hard cap 100.
        scope: "kb" (default) searches Knowledge Base/ first and
            AUTO-WIDENS to the whole vault when the KB doesn't fill
            `limit` — so content in sibling folders (Tracking/,
            Reference/, Finance/, ... and curated, read-only trees kept
            outside Knowledge Base/) is never silently invisible. Widened
            hits carry `outside_kb: true`. "vault" always walks the
            whole vault. "kb-only" is the strict opt-out: KB only,
            never widens. Outside-KB recall is BM25/keyword (the
            vector sidecar is KB-scoped), with a relaxed gate so terse
            files (e.g. a numbers-heavy tracker) surface on a partial
            token match. `_Schema/`, `_trash/`, `_attachments/`, and
            `.obsidian/` are excluded under every scope. NOTE: an
            empty result means "not found in what I searched," NOT
            "doesn't exist" — say so, and try "vault" before
            concluding absence.
        mode: Ranker. "hybrid" (default) fuses BM25 + local vector
            embeddings via reciprocal rank fusion — best recall on
            natural-language queries. "keyword" preserves the original
            case-insensitive substring matching, sorted by `updated:`.
            "vector" is vector-only (testing aid). BM25 corpus is
            Snowball-stemmed so "regulation" reaches pages with
            "regulator"; keyword mode stays strict-substring. If the
            embedding sidecar hasn't been built yet, hybrid degrades
            to BM25-only; run `audit_fix(rebuild_embeddings=true)` to
            populate it.
        graph: When true (default) and mode is hybrid/vector, outbound
            wikilinks of top BM25/vector candidates contribute a third
            ranking — surfaces 1-hop neighbours of strong matches.
        rerank: Cross-encoder re-sort of the top fused candidates
            (bge-reranker-base) for higher-precision ordering. Default
            (unset) is mode-aware AUTO: in CPU steady-state modes it stays
            off for predictable latency; when the text/reranker device is
            accelerated, the server reranks only when ranking lanes
            disagree or the query is long. Pass true to force reranking
            for a high-value query, including on CPU; pass false to skip
            it entirely.
        rerank_max_candidates: Bound the fused prefix sent to the reranker.
            Must be an integer from the effective result limit through 300.
            Omit to preserve the existing `3 * limit` prefix. This bounds
            candidate count, not wall-clock latency: the synchronous model
            call has no safe cancellation boundary.
        prefer_compiled: When true (default), applies a small boost to
            compiled types (insight, pattern, failure, research-note,
            entity) and a small penalty to raw `source` after fusion
            AND rerank. Reflects the KB's epistemic hierarchy. Set
            false to retrieve raw source discussion verbatim (e.g.
            "what did I capture from Dr. X").
        prefer_active: When true (default), soft-demotes `status:
            superseded` pages so a replaced conclusion can't outrank the
            page that superseded it. The tombstone stays findable and its
            hit still carries `status` + `superseded_by` (the forward
            pointer) so you can see it's superseded. Set false to rank a
            superseded page on its content alone (e.g. "what did I used to
            think about X").
        prefer_used: When true (OFF by default — default ranking is
            usage-blind), applies a bounded, positive-only usage boost:
            pages you actually read (`get`) and cite in written notes rank
            slightly higher, from ACT-R activation over the server's own
            access logs. Capped below the compiled boost so usage breaks
            ties but never overrides the epistemic hierarchy; never a
            penalty; never ADDS results — it only reorders pages the
            content lanes already matched. Boosted hits expose
            `signals.activation` + `signals.usage_boost` so you can see
            exactly why. Reading and citing pages IS the feedback loop —
            no separate feedback call exists or is needed. Use for "what
            have I been working with lately" recall; leave off for
            neutral knowledge lookup.
        pack: When true (off by default), ALSO assemble a reasoning-ready
            context pack from the top hits and change the return to
            {"hits": [...], "pack": {...}} (with pack off, the return is the
            usual hit list, unchanged). The pack is PURE MEASUREMENT over the
            notes you already wrote — no server-side reasoning: each top note's
            structurally-extracted key claims (lede + headline-section lines +
            heading outline), bounded cited compact/rich semantic units with
            parent provenance/lifecycle and authored relations, the 1-hop
            wikilink neighbourhood of those notes ranked by co-citation, and
            the contradictions among them (recorded supersession edges +
            proximity "tension" pairs in the embedding band, surfaced for you
            to judge — proximity, not polarity). Page, unit, and mixed results
            all group by parent before packing. Lets you reason over the matches
            in one shot instead of fanning out `get` calls. Bounded with explicit
            `truncation`; the tension part needs the embedding sidecar and reports
            `embeddings_available`.
        graph_enrich: When true with `pack=true`, add typed graph neighborhood
            data from the derived epistemic graph sidecar to the pack. Default
            false; missing/disabled/stale graph state soft-fails inside
            `pack.graph.available` without changing hits, hit ordering, or the
            default pack contract.
        detail: Result verbosity. "full" (default) keeps the current hit
            shape including `excerpt` and `signals`. "compact" returns the
            SAME ranked hits as token-cheap routing stubs — path, title,
            type, scope, updated, plus lifecycle/media/outside_kb markers
            when present — omitting `excerpt` and `signals`. Use compact
            for cheap proactive recall, then `get` a chosen page (or rerun
            with detail="full") when you need the why.
        include_timings: When true (off by default), the return becomes an
            envelope {"hits": [...], "timings": {...}} (with pack=true the
            envelope also carries "pack"). `timings` reports total_ms,
            hot-cache status, and per-stage milliseconds for the retrieval
            lanes (skipped/failed optional lanes are marked, never fatal).
            Diagnostics only — timings never include note content. Omitted
            → the response shape is unchanged.
        explain: Add a bounded retrieval profile and per-hit ranking evidence.
            False by default; omitted/false preserves the existing response.

    Returns:
        With pack off (default): a list of {path, type, scope, title, updated,
        excerpt[, outside_kb]
        [, status][, superseded_by][, signals]}. `outside_kb: true` is
        present only on hits the "kb" auto-widen pulled from beyond
        Knowledge Base/ (the `path` also shows the sibling folder).
        `status` + `superseded_by` appear only when a hit is NOT plain
        `active` — i.e. a superseded tombstone (or draft) — so you can tell
        it from a live conclusion and follow `superseded_by` to the replacement.
        In hybrid mode `excerpt` shows the best-matching chunk; in
        keyword mode it's a snippet anchored to the literal query
        match. `signals` (hybrid/vector only) carries per-ranker
        position: {bm25_rank?, vector_rank?, vector_score?, graph_hop?,
        graph_in_degree?, rerank_score?}. `graph_in_degree` is the
        number of top-N seeds whose body wikilinks to this hit —
        independent of graph_hop, which only fires for graph-only
        results.
        With pack on: {"hits": [...the same list...], "pack": {packed_paths,
        claims, semantic_units, semantic_blocks, neighborhood, contradictions:
        {superseded, tension}, embeddings_available, truncation}}. `semantic_units`
        groups bounded citable units under one parent context; `semantic_blocks`
        is a bounded compatibility projection from those same rich units.
        With detail="compact": each hit is the routing stub described under
        `detail` (no excerpt/signals) — same paths, same order.
        With include_timings on: {"hits": [...], ["pack": {...},]
        "timings": {total_ms, cache, stages}}.
        Right after a server start, while models are still warming in the
        background, semantic lanes are skipped rather than blocked on — the
        result is then the envelope {"hits": [...], "warming": {"components":
        [...], "since_s": N}} and the hits are lexical-only ranking. If
        recall quality matters for the query, retry once "warming" stops
        appearing (typically well under a minute).
    """
    if detail not in ("full", "compact"):
        raise ValueError(
            f"find: detail must be 'full' or 'compact', got {detail!r}"
        )
    auto_rerank = rerank is None and find_module.auto_rerank_allowed_by_policy()
    compute_profile: dict[str, str | bool] = {}
    if explain:
        from . import mode as mode_module

        policy = mode_module.resolved()
        compute_profile = {
            key: policy[key]
            for key in (
                "mode",
                "preload_models",
                "retain_cpu_caches",
                "defer_expensive_indexes",
                "release_when_idle",
            )
        }
    retrieval_trace = (
        retrieval_explain_module.RetrievalTrace(
            requested_mode=mode,
            requested_result_level=result_level,
            rerank_requested=rerank,
            auto_rerank=auto_rerank,
            rerank_candidate_limit_requested=rerank_max_candidates,
            compute_profile=compute_profile,
        )
        if explain
        else None
    )
    timings = find_module.FindTimings() if include_timings else None
    if timings is not None:
        from . import mode as mode_module

        timings.profile.update(
            {
                "mode": mode,
                "scope": scope,
                "detail": detail,
                "pack": pack,
                "graph_enrich": graph_enrich,
                "graph": graph,
                "rerank_requested": rerank,
                "rerank_max_candidates": rerank_max_candidates,
                "auto_rerank": auto_rerank,
                "prefer_compiled": prefer_compiled,
                "prefer_active": prefer_active,
                "prefer_used": prefer_used,
                "result_level": result_level,
                "compute_policy": mode_module.resolved(),
            }
        )
    degraded: list[str] = []
    failed: list[str] = []
    hits = find_module.find(
        vault_root,
        query=query,
        types=types,
        projects=projects,
        tags=tags,
        speakers=speakers,
        file_types=file_types,
        exclude_file_types=exclude_file_types,
        categories=categories,
        kinds=kinds,
        filters=filters,
        result_level=result_level,
        limit=limit,
        scope=scope,
        mode=mode,
        graph=graph,
        rerank=rerank,
        rerank_max_candidates=rerank_max_candidates,
        # rerank=None uses the mode/device-gated auto policy. Explicit
        # true/false from the caller always wins over auto.
        auto_rerank=auto_rerank,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        prefer_used=prefer_used,
        timings=timings,
        degraded_out=degraded,
        failed_out=failed,
        retrieval_trace=retrieval_trace,
    )
    pack_obj: dict | None = None
    if pack:
        with find_module._span(timings, "pack"):
            pack_obj = context_pack_module.assemble_pack(
                vault_root, hits, graph_enrich=graph_enrich
            )
    with find_module._span(timings, "serialize"):
        if detail == "compact":
            hit_dicts = [h.as_compact_dict() for h in hits]
        else:
            hit_dicts = [h.as_dict() for h in hits]
        ref_index = memory_refs_module.ReferenceIndex(vault_root)
        refs = ref_index.refs_for_paths(
            [str(hit.get("path") or "") for hit in hit_dicts]
        )
        for hit in hit_dicts:
            ref = refs.get(str(hit.get("path") or ""))
            if ref:
                hit["ref"] = ref
        if retrieval_trace is not None:
            retrieval_explain_module.attach_hit_explanations(retrieval_trace, hit_dicts)
    timings_dict = timings.as_dict() if timings is not None else None
    # Durable structured log → feeds the offline retrieval feedback loop.
    # Best-effort; never affects the returned result.
    query_log.log_find_call(
        query=query, mode=mode, scope=scope,
        types=types, projects=projects, tags=tags,
        limit=limit, rerank=rerank, prefer_compiled=prefer_compiled,
        prefer_used=prefer_used,
        graph=graph, hits=hits,
        timing_summary=_timing_log_summary(timings_dict),
    )
    # Warming marker: the server just started and the background warm-up is
    # still loading models, so one or more semantic lanes were skipped —
    # these hits are lexical-only ranking. Present only during that window
    # (~30s per process start; minutes on a first-ever model download).
    warming: dict | None = None
    if degraded:
        info = readiness_module.warming_info() or {}
        warming = {
            "components": sorted(set(degraded)),
            "since_s": info.get("since_s", 0.0),
        }
    # Degraded marker: a semantic lane FAILED post-warm (not merely deferred) so
    # the hits are a silently weaker ranking — vector→BM25, or every-lane-empty→
    # keyword. Distinct from `warming`: warming is the transient, expected boot
    # window; `degraded` means a lane broke (e.g. a corrupt embedding sidecar or
    # a crashing model) and the fallback should be investigated, not waited out.
    degraded_marker: list[str] | None = sorted(set(failed)) if failed else None
    if (
        timings_dict is None
        and warming is None
        and degraded_marker is None
        and retrieval_trace is None
    ):
        if not pack:
            return hit_dicts
        return {"hits": hit_dicts, "pack": pack_obj}
    out: dict = {"hits": hit_dicts}
    if pack:
        out["pack"] = pack_obj
    if timings_dict is not None:
        out["timings"] = timings_dict
    if warming is not None:
        out["warming"] = warming
    if degraded_marker is not None:
        out["degraded"] = degraded_marker
    if retrieval_trace is not None:
        out["retrieval_profile"] = retrieval_trace.profile()
    return out






def _citation_url(_path: str) -> str:
    """Citation URL placeholder for portable clients.

    The local vault path is the stable citation ID; exposing file:// URLs here
    would make remote clients less portable.
    """
    return ""


def _resolve_memory_identifier(vault_root: Path, value: str) -> str:
    try:
        return memory_refs_module.resolve_identifier(vault_root, value)
    except memory_refs_module.ReferenceError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc


def _attach_memory_ref(vault_root: Path, out: dict, path: str) -> dict:
    ref = memory_refs_module.ReferenceIndex(vault_root).ref_for_path(path)
    if ref:
        out["ref"] = ref
    return out


def _string_metadata(**items: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in items.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple, dict)):
            out[key] = json.dumps(value, ensure_ascii=False)
        else:
            out[key] = str(value)
    return out


def _search_result_from_hit(hit: dict) -> SearchResult:
    path = str(hit.get("path") or "")
    title = str(hit.get("title") or path)
    metadata = _string_metadata(
        path=path,
        type=hit.get("type"),
        scope=hit.get("scope"),
        updated=hit.get("updated"),
        status=hit.get("status"),
        superseded_by=hit.get("superseded_by"),
        outside_kb=hit.get("outside_kb"),
        media_type=hit.get("media_type"),
        media_file=hit.get("media_file"),
        ref=hit.get("ref"),
    )
    return {"id": path, "title": title, "url": _citation_url(path), "metadata": metadata}


def _frontmatter_metadata(path: str, frontmatter: dict[str, Any]) -> dict[str, str]:
    allowed = (
        "type",
        "status",
        "created",
        "updated",
        "project",
        "projects",
        "tags",
        "severity",
        "pattern_type",
        "domain",
    )
    items = {key: frontmatter.get(key) for key in allowed if key in frontmatter}
    items["path"] = path
    return _string_metadata(**items)


def _title_from_page(path: str, frontmatter: dict[str, Any], body: str = "") -> str:
    return vault.resolve_display_title(frontmatter, body, path)


def _bounded_text(text: str, max_chars: int) -> tuple[str, bool]:
    max_chars = max(500, min(int(max_chars), 6000))
    if len(text) <= max_chars:
        return text, False
    marker = "\n\n[truncated]"
    keep = max(0, max_chars - len(marker))
    return text[:keep].rstrip() + marker, True


def op_search(
    vault_root: Path,
    query: str = "",
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    scope: str = "kb",
) -> SearchResponse:
    """Search the Knowledge Base with a portable metadata-only response. Read-only.

    This is the conservative companion to `find`: it returns result IDs, titles,
    URLs, and string metadata only. It never returns note excerpts, graph/ranking
    signals, context packs, timings, raw content, or page bodies. Use `fetch`
    with one returned `id` to read bounded document text, or use `find`/`get`
    directly when the caller intentionally needs richer retrieval output.

    Args:
        query: Free-text search string.
        types: Optional page-type filter; values are OR'd within the filter.
        projects: Optional project filter; values are OR'd within the filter.
        tags: Optional tag filter; values are OR'd within the filter.
        limit: Maximum number of results. Capped to 50.
        scope: "kb" for Knowledge Base first, or "vault" to search the broader vault.

    Returns:
        {"results": [{"id", "title", "url", "metadata"}, ...]}. `id` is the
        canonical vault-relative path to pass to `fetch` or `get`; `metadata`
        contains string-only routing fields such as path, type, scope, updated,
        status, and media markers.
    """
    limit = max(1, min(int(limit), 50))
    raw = op_find(
        vault_root,
        query=query,
        types=types,
        projects=projects,
        tags=tags,
        limit=limit,
        scope=scope,
        mode="hybrid",
        graph=True,
        rerank=False,
        prefer_compiled=True,
        prefer_active=True,
        prefer_used=False,
        pack=False,
        detail="compact",
        include_timings=False,
    )
    hits = raw.get("hits", []) if isinstance(raw, dict) else raw
    return {"results": [_search_result_from_hit(hit) for hit in hits]}


def op_fetch(
    vault_root: Path,
    id: str,
    max_chars: int = 3000,
) -> FetchResponse:
    """Fetch one Knowledge Base document by `search` result ID with bounded text. Read-only.

    This is a bounded read step between metadata-only `search` and full `get`.
    It returns the markdown body without raw frontmatter and caps body text at
    6000 characters. Use `get` when the caller intentionally needs the full
    frontmatter/body/edit hash envelope.

    Args:
        id: A result `id` returned by `search` (the canonical vault-relative path).
        max_chars: Maximum body characters to return. Values below 500 are raised
            to 500; values above 6000 are capped server-side.

    Returns:
        {"id", "title", "text", "url", "metadata"}. `text` is the markdown body;
        it ends with `[truncated]` when the body exceeded the effective cap.
    """
    id = _resolve_memory_identifier(vault_root, id)
    try:
        page = get_page_module.get_page(vault_root, path=id)
    except get_page_module.GetError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    text_out, truncated = _bounded_text(page.body, max_chars)
    metadata = _frontmatter_metadata(page.path, page.frontmatter)
    metadata.update(_string_metadata(truncated=truncated))
    query_log.log_get_call(
        read_path=page.path,
        frontmatter_only=False,
        include_history=False,
    )
    out = {
        "id": page.path,
        "title": _title_from_page(page.path, page.frontmatter, page.body),
        "text": text_out,
        "url": _citation_url(page.path),
        "metadata": metadata,
    }
    return _attach_memory_ref(vault_root, out, page.path)
def _timing_log_summary(timings_dict: dict | None) -> dict | None:
    """Query-log-safe slice of a timings envelope: totals + per-stage ms only
    (never content; stage entries drop skip/error detail to stay compact)."""
    if timings_dict is None:
        return None
    return {
        "total_ms": timings_dict.get("total_ms"),
        "cache_hit": bool(timings_dict.get("cache", {}).get("hit")),
        "stage_ms": {
            name: entry["ms"]
            for name, entry in timings_dict.get("stages", {}).items()
            if isinstance(entry, dict) and "ms" in entry
        },
    }


def op_suggest_links(
    vault_root: Path,
    path: str | None = None,
    draft_title: str | None = None,
    draft_body: str | None = None,
    limit: int = 8,
    scope: str = "kb",
) -> list[dict]:
    """Suggest existing KB pages a note should link to. Read-only.

    Closes the corpus-blind-write gap: surfaces the related prior work a
    draft (or an existing page) should connect to, so the graph gets denser
    with every write instead of just bigger. For link suggestions only — it
    reuses the same hybrid ranker as `find`, prefers well-connected hubs, and excludes
    the page itself plus anything it already links. Suggestions are
    non-binding: YOU decide which to wire in (e.g. via a follow-up `edit`).

    Two call shapes:
    - `path`: suggest links for an EXISTING page (densify it retroactively).
      Same path conventions as `get`/`find`.
    - `draft_title` + `draft_body`: suggest links for a note you're about to
      create, BEFORE calling `note` — so you can cite/connect on first write.

    Args:
        path: Existing page to suggest links for. Mutually exclusive with
            the draft_* args.
        draft_title: Title of a not-yet-written note.
        draft_body: Body (markdown) of a not-yet-written note. Wikilinks
            already present in it are treated as "already linked" and excluded.
        limit: Max suggestions (default 8).
        scope: "kb" (default) or "vault" — same meaning as `find`.

    Returns:
        List of {path, title, type, why, excerpt}, best-first. `why`
        explains the match (e.g. "semantic #2, 4 shared link(s) (hub)").
        Empty list if nothing relevant or the draft/page is empty.

    Errors:
        INVALID_SUGGEST (neither path nor draft supplied); plus get-style
        path errors (NOT_FOUND, INVALID_PATH) when `path` doesn't resolve.
    """
    if path:
        path = _resolve_memory_identifier(vault_root, path)
        try:
            gp = get_page_module.get_page(vault_root, path=path)
        except get_page_module.GetError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        page = find_module._CACHE.get(vault_root / gp.path, vault_root)
        if page is None:
            raise ValueError(f"UNREADABLE: could not parse {gp.path}")
        existing_links = set(
            find_module._outbound_wikilink_paths(page, vault_root)
        )
        suggestions = corpus_aware_module.suggest_related(
            vault_root, title=page.title, body=page.body,
            self_path=page.rel_path, existing_links=existing_links,
            limit=limit, scope=scope,
        )
    elif draft_title or draft_body:
        body = draft_body or ""
        existing_links = set(link_summary_module.outbound_link_targets(body))
        suggestions = corpus_aware_module.suggest_related(
            vault_root, title=draft_title or "", body=body,
            self_path=None, existing_links=existing_links,
            limit=limit, scope=scope,
        )
    else:
        raise ValueError(
            "INVALID_SUGGEST: provide either `path` (existing page) or "
            "`draft_title`/`draft_body` (a note you're about to write)"
        )
    return [s.as_dict() for s in suggestions]


def op_graph_context(
    vault_root: Path,
    path: str | None = None,
    query: str | None = None,
    unit_ref: str | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
) -> dict:
    """Return a bounded typed-graph neighborhood for a page or query. Read-only.

    Reads the derived `.graph.sqlite` sidecar created from Markdown,
    frontmatter, wikilinks, source/evidence links, supersession fields, and
    semantic blocks. Markdown remains the source of truth; this operation does
    not build the sidecar, modify notes, accept suggested relations, or change
    `find` ranking. If the graph sidecar is missing, disabled, or incompatible,
    the response reports `available: false` instead of failing.

    Args:
        path: Existing page path to use as the graph seed. Optional when `query`
            is supplied.
        query: Text seed for matching graph node titles/content. Optional when
            `path` is supplied.
        unit_ref: Exact current semantic-unit reference to use as the graph seed.
        categories: Optional registry-resolved semantic-unit category allowlist.
        kinds: Optional governed semantic-unit kind allowlist.
        depth: Traversal depth from seed nodes. Default 1.
        relation_types: Optional allowlist of relation types, e.g.
            `derived_from`, `evidenced_by`, `supports`, `contradicts`,
            `supersedes`, `links_to`.
        node_types: Optional allowlist of node kinds, e.g. `file`, `decision`,
            `finding`, `risk`, `action`, `claim`, `evidence`.
        max_nodes: Cap returned nodes. Default 40.
        max_edges: Cap returned edges. Default 80.
        traversal_profile: Deterministic traversal lens. One of `epistemic`,
            `provenance`, `causal`, `decision`, `all`, or a governed custom profile.

    Returns:
        {available, reason, seeds, nodes, edges, truncation}. Nodes and edges
        carry source path/anchor/hash provenance and relation metadata.
    """
    if path:
        path = _resolve_memory_identifier(vault_root, path)
    return epistemic_graph_module.graph_context(
        vault_root,
        path=path,
        query=query,
        unit_ref=unit_ref,
        categories=categories,
        kinds=kinds,
        depth=depth,
        relation_types=relation_types,
        node_types=node_types,
        max_nodes=max_nodes,
        max_edges=max_edges,
        traversal_profile=traversal_profile,
    )


def op_suggest_relations(
    vault_root: Path,
    path: str | None = None,
    draft_title: str | None = None,
    draft_body: str | None = None,
    include_model_suggestions: bool = False,
    limit: int = 10,
) -> dict:
    """Suggest candidate typed graph relations. Read-only and proposal-only.

    Uses deterministic signals from wikilinks, frontmatter sources, shared
    sources/entities, supersession, and optional embedding proximity when
    available. Model-backed suggestions are default-off and soft-fail as
    warnings; accepted relations still require an explicit `note` or `edit`
    write. This operation never mutates Markdown or the graph sidecar.

    Args:
        path: Existing page to inspect. Mutually exclusive with draft-only use.
        draft_title: Optional title for a not-yet-written draft.
        draft_body: Draft body; wikilinks in it become proposal candidates.
        include_model_suggestions: Request optional model-backed suggestion
            paths. Default false; unavailable paths soft-fail with warnings.
        limit: Max candidates to return. Default 10.

    Returns:
        {candidates, warnings, model_suggestions_available, mutated}. Each
        candidate includes from/to, relation_type, method, and evidence.
        `mutated` is always false.
    """
    if path:
        path = _resolve_memory_identifier(vault_root, path)
    return epistemic_graph_module.suggest_relations(
        vault_root,
        path=path,
        draft_title=draft_title,
        draft_body=draft_body,
        include_model_suggestions=include_model_suggestions,
        limit=limit,
    )

def op_add(
    vault_root: Path,
    source_schema: object,  # SourceSchema; injected + stripped, so kept import-free here
    content: str,
    source_type: str,
    title: str,
    slug: str | None = None,
    url: str | None = None,
    tags: list[str] | None = None,
    why_captured: str | None = None,
) -> dict:
    """Capture raw content as an immutable source page in the Knowledge Base.

    Writes a frontmatter-compliant page to Sources/<Type>/YYYY-MM-DD-<slug>.md
    and updates Sources/index.md, the top-level index.md (Recent activity
    + Counts), and log.md. Per SKILL.md rule 7.

    Args:
        content: Full text body to capture (markdown / plain text). For
            files or binaries, use the /upload endpoint instead.
        source_type: One of article, session, book, paper, video, other.
        title: Unicode display title stored in frontmatter and the H1.
        slug: Optional lowercase ASCII kebab-case filename component.
        url: Required when source_type is article, paper, or video.
        tags: Lowercase dash-separated; the server normalizes case/spacing.
        why_captured: One short paragraph on why this is worth keeping.
            Rendered as a leading blockquote in the source body, between
            the `# Source: ...` header and the `## Capture` section.

    Returns:
        {path, warnings}. On schema violation, raises a structured error
        with code=INVALID_SOURCE, the missing fields, and the reason.
    """
    try:
        result = add_module.add(
            vault_root,
            source_schema,
            content=content,
            source_type=source_type,
            title=title,
            slug=slug,
            url=url,
            tags=tags,
            why_captured=why_captured,
        )
    except add_module.AddError as e:
        # FastMCP serializes raised exceptions; we want a structured shape.
        raise ValueError(
            f"{e.code}: {e.reason} (missing: {e.missing})"
        ) from e
    query_log.log_write_call(tool="add", written_path=result.path, cited_sources=[])
    return result.as_dict()


def op_audit(
    vault_root: Path,
    categories: list[str] | None = None,
    detail: Literal["actionable", "full"] = "actionable",
    legacy_sample_limit: _AuditSampleLimit = audit_module.DEFAULT_LEGACY_SAMPLE_LIMIT,
) -> dict:
    """Audit / lint / health-check the Knowledge Base: find orphans, broken wikilinks, supersession gaps, stale unprocessed sources, and stale-review candidates. Read-only.

    Returns a structured report Claude can read to propose follow-up
    edits via `note`/`add`. Does NOT modify anything.

    Categories (default: all):
    - `broken_wikilink`: `[[X]]` whose target file doesn't exist.
      Skips wikilinks inside fenced code blocks and inline code spans.
      Bare names resolve against filename stems AND frontmatter `title:`
      (so date-prefixed sources with a title match are not flagged).
    - `orphan_entity`: `Entities/...` file with no inbound wikilinks
    - `unprocessed_source`: source with empty `ingested_into:` (no notes
      have compiled from it yet)
    - `index_drift`: top-level `index.md` Counts disagree with on-disk counts
    - `tag_inconsistency`: case/separator variants of the same tag
      (`warning_letter_incident` vs `warning-letter-incident` vs
      `Warning-Letter-Incident`). Mechanical drift only; semantic
      near-duplicates like `workflow` vs `workflows` aren't flagged.
    - `frontmatter_compliance`: per-page-type required-field gaps,
      a `tenant:` set without the expected project, patterns using singular
      `project:` instead of plural `projects:`.
    - `unregistered_project_key`: a `project`/`projects` value not in the
      registry (typo or genuinely new scope).
    - `embedding_drift`: vector sidecar rows out of sync with disk (a file
      changed/added/removed since it was last embedded).
    - `relevance_pairs_pending`: real-usage (query -> cited_path) labels not
      yet in the golden retrieval set.
    - `stale_review`: active compiled conclusion that is old AND rarely
      surfaced in `find` AND low inbound-link degree — a measurement-only
      review candidate (still true? keep / supersede / archive). Never
      decays or down-ranks; `find` ordering is unchanged.
    - `corpus_contradictions`: corpus-wide pairs of active read-write
      compiled conclusions whose embeddings sit just below the near-dup
      threshold (close enough to restate/refine/contradict). A proximity
      measurement surfaced for review (reconcile or supersede); never
      auto-acted. The queue is ordered by review priority (cosine + ACT-R
      dormancy), same-family `Notes/Research/<X>/` architecture noise is
      demoted, and the surfaced set is capped at EXOMEM_CONTRADICTION_TOP_N
      (default 40; 0 = uncapped) with an explicit "N more not shown" line.
      No-ops when embeddings are disabled.

    Args:
        categories: Optional filter; only run these checks. Each must be
            one of the categories above. Omit to run all.
        detail: `actionable` (default) groups grandfathered relation debt and
            prioritizes current work; `full` returns every raw finding.
        legacy_sample_limit: Number of deterministic legacy-backlog samples in
            actionable output. Integer from 0 to 50; default 5.

    Returns:
        Action-first findings, summary, grouped legacy backlog, and explicit
        presentation/truncation facts. Full detail preserves raw findings.
    """
    audit_module.validate_presentation_controls(detail, legacy_sample_limit)
    report = audit_module.audit(
        vault_root,
        categories=categories,
        semantic_detail=detail,
    )
    return report.as_public_dict(
        detail=detail,
        legacy_sample_limit=legacy_sample_limit,
    )


def op_attention(
    vault_root: Path,
    categories: list[str] | None = None,
    limit: int = 25,
    state: str = "open",
) -> dict:
    """Your review queue: the one ranked list of what in the Knowledge Base needs your attention today. Read-only.

    Composes the four measurement-only epistemic queues into a single list,
    ranked by Reciprocal Rank Fusion over each queue's own ordering — a note
    flagged by more than one queue rises to the top:
    - `stale_review`: active conclusions that are old AND rarely surfaced in
      `find` AND low inbound-degree (possibly stale — still true?).
    - `corpus_contradictions`: pairs of active conclusions whose embeddings sit
      close enough to restate, refine, or contradict (do they conflict?).
    - `unprocessed_source`: sources captured but never compiled (nothing
      distilled from them yet).
    - `relation_debt`: active compiled pages with no outbound Markdown
      connections (semantic neighbours are not yet durable graph edges).

    Each item carries its reason(s), the related note(s) for a contradiction
    pair, and severity. Surfaced for REVIEW only: the ranking is a deterministic
    measurement, not a judgment that anything is wrong — you decide keep /
    `replace` (supersede) / `reconcile` / `propose_compilation` / archive.
    Nothing is auto-acted; `find` ordering is unchanged.

    Front door for daily review. Use `audit` instead for the full lint/health
    report (broken wikilinks, frontmatter compliance, index drift, embedding
    drift, etc.).

    Args:
        categories: Optional subset of {corpus_contradictions, stale_review,
            unprocessed_source, relation_debt}. Omit to include all four.
        limit: Max items to surface (default 25; 0 or negative = uncapped,
            surface all). Lower-priority items beyond the cap are summarized in
            a "N more not shown" note, never dropped silently.
        state: open (default), all, snoozed, or dismissed.

    Returns:
        {items: [{path, score, severity, categories, reasons: [{category, rank,
         detail, related_paths?, meta}], proposed_fix}], summary: {category:
         count}, shown, total, truncated, upstream_truncated, note}.
    """
    report = attention_module.attention(
        vault_root,
        categories=categories,
        limit=limit,
        state=state,
    )
    return report.as_dict()


def op_evolution(
    vault_root: Path,
    query: str = "",
    limit: int = 10,
    scope: str = "kb",
    projects: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """How a conclusion CHANGED over time — the supersession history of a topic, as timelines. Read-only.

    For a topic `query`, finds the matching notes, follows each one's supersession
    chain (the `supersedes`/`superseded_by` links `replace` records), and returns one
    ordered timeline per chain — oldest version → newest. Each version carries its own
    structurally-extracted claims, its date, and the RECORDED reason it was superseded
    (the `why:` logged at that edit). The server only orders and surfaces what you
    wrote; it does NOT generate a "here's how your thinking changed" summary — you read
    the consecutive versions and see the shift.

    Use this for "how did my view on X evolve / what did I used to think / why did this
    change". Use `find` for plain lookup. Notes never superseded are omitted (no
    evolution to show); a topic with no supersession returns empty `timelines` — an
    honest "nothing changed here", not an error. Read-only: mutates nothing; `find`
    ordering is unchanged.

    Args:
        query: The topic to trace (free text, like `find`).
        limit: Max chains (timelines) to return, by find relevance (default 10;
            0 or negative = uncapped). Chains beyond the cap are reported in
            `truncation`, never dropped silently.
        scope: Search scope passed to `find` — "kb" (default) / "vault" / "kb-only".
        projects: Optional project-key filter passed to `find`.
        tags: Optional tag filter passed to `find`.

    Returns:
        {query, timelines: [{chain_id, topic_anchor, span: {from, to, n_versions},
         versions: [{path, title, status, date, claims: {title, type, lede, sections,
         outline}, transition: {reason, date} | null}]}], truncation: [...]}.
        `transition` is null on the active head; `versions` run oldest → newest by
        supersession order; `span`/
_versions` describe the whole chain.
    """
    return evolution_module.evolution(
        vault_root,
        query=query,
        limit=limit,
        scope=scope,
        projects=projects,
        tags=tags,
    )


def op_audit_fix(
    vault_root: Path,dry_run: bool = False, rebuild_embeddings: bool = False) -> dict:
    """Run audit + auto-apply safe fixes; propose-only for risky categories.

    Closes the lint-finds-but-doesn't-fix loop. Safe categories get
    rewritten in-place via atomic batch writes; risky categories
    surface in `proposed` for human/LLM review.

    Safe categories (auto-applied):
    - Canonical wikilink form across all compiled material (body +
      frontmatter). Skips Sources/ and Evidence/ (append-only).
    - Frontmatter required-field backfill with safe defaults:
      - production-log missing created/updated → use started/shipped/today
      - research-note/insight/failure/pattern missing status → "active"
      - research-note/insight/failure/pattern missing updated →
        use created, else today
      - experiment missing duration → computed from started+concluded
      - source missing captured → use created (if present)
    - Pattern with singular `project:` → plural `projects: [<value>]`
      (auto-merged into existing projects: list if present).
    - Sub-folder index refresh + top-index count refresh.

    Risky categories (propose-only, surfaced in `proposed` list):
    - broken_wikilink residuals after canonicalization (forward refs,
      missing files, audit limitations).
    - orphan_entity (deletion is too big to auto-apply).
    - unprocessed_source (compilation is a thinking task).
    - tag_inconsistency (renames can break user mental models).
    - frontmatter_compliance: tenant: misuse (might be intentional).
    - source missing source_type (folder→type inference is brittle).

    Idempotent: running twice on a clean vault produces no changes.

    Args:
        dry_run: If true, compute what would change without writing.
            Default false.
        rebuild_embeddings: If true, wipe and rebuild the vector sidecar
            at `<vault>/Knowledge Base/.embeddings.sqlite` after the fix
            sweep. Use on first run, after a machine swap, or when the
            sidecar has drifted from disk. Ignored when `dry_run=true`.

    Returns:
        {fixed: [{category, path, detail, action}, ...],
         proposed: [<audit findings>],
         files_rewritten: int,
         summary: {fixed: N, proposed: N, fixed_<category>: N,
                   embeddings_chunks?: N},
         dry_run: bool}
    """
    report = audit_fix_module.audit_fix(
        vault_root,
        dry_run=dry_run,
        rebuild_embeddings=rebuild_embeddings,
    )
    return report.as_dict()


def op_reconcile(
    vault_root: Path,dry_run: bool = False) -> dict:
    """Heal vault drift from out-of-band edits in one pass.

    The writers keep the embedding sidecar, index.md count rows, and log.md
    current on every write. But editing the vault directly — in Obsidian,
    on mobile, or via a manual filesystem edit — bypasses those hooks, so
    the sidecar and the counts drift silently. `reconcile` is the
    first-class "I edited around the system, fix it" command:

    1. Index counts — recompute Sources/Notes/Entities count rows from
       on-disk reality and rewrite any that drifted (curated descriptions
       and Recent-activity are preserved; only count tokens move).
    2. Embeddings — incrementally re-embed only the *stale* files (those
       `embedding_drift` flags: on-disk mtime newer than the sidecar row),
       via the same path the writers use. Cheaper than
       `audit_fix(rebuild_embeddings=true)`'s full wipe-and-rebuild.
    3. Drift report — re-run index_drift + embedding_drift, return what
       remains.

    Narrower than `audit_fix`: it does NOT canonicalize wikilinks or
    backfill frontmatter (those are content rewrites you opt into).
    Idempotent; `dry_run=true` reports without writing.

    Args:
        dry_run: If true, compute what would change without writing.
            Default false.

    Returns:
        {indexes_updated: [<index path>, ...],
         embeddings_refreshed: int,
         embeddings_status: "current" | "refreshed" | "disabled",
         remaining_drift: [<audit findings>],
         dry_run: bool}
    """
    report = reconcile_module.reconcile(vault_root, dry_run=dry_run)
    return report.as_dict()


def op_provenance_report(
    vault_root: Path,
    tag: str | None = None,
    key: str | None = None,
    value: str | None = None,
    path: str | None = None,
) -> dict:
    """Trace provenance: scan note bodies for `<!-- key:value -->` tags — where an opinion/take/flag came from. Read-only.

    On-demand scan over markdown bodies — no index, no sidecar. Use it to
    answer "show all conv:-derived takes" or "what's flagged add-to-imdb"
    without grepping. The opinion/taste rows carry provenance as HTML
    comments (e.g. `<!-- platform:imdb -->`, `<!-- conv:2026-06-01 -->`);
    this reads them in place. Tags inside fenced code are ignored; multiple
    comments and multiple key:value pairs on one line are all parsed.

    Args:
        tag: Shorthand filter — "key" or "key:value" (e.g. "platform:imdb").
        key: Filter to rows carrying this provenance key.
        value: With key, require this exact value.
        path: Restrict the scan to one vault-relative file (else the whole
            Knowledge Base is walked).

    Returns:
        {findings: [{path, line_number, row_text, tags}], summary:
         {key: count}}. line_number is body-relative (frontmatter excluded).
    """
    findings = provenance_module.scan_provenance(
        vault_root, tag=tag, key=key, value=value, path=path
    )
    summary: dict[str, int] = {}
    for f in findings:
        for k in f.tags:
            summary[k] = summary.get(k, 0) + 1
    return {"findings": [f.as_dict() for f in findings], "summary": summary}


def op_propose_compilation(
    vault_root: Path,
    sources: list[str],
    suggested_title: str | None = None,
) -> dict:
    """Draft / scaffold a compiled note from unprocessed source(s) — what to compile next, drain the source backlog. Read-only.

    The backlog-drain companion to `audit`'s `unprocessed_source` findings:
    point it at one or more raw sources and it hands back a ready-to-fill
    note skeleton — inferred note_type, a Question/Findings/Relations (or
    Claim/…) outline, the `sources[]` to cite, and adjacent compiled pages to
    link (computed via the same hybrid retrieval as `suggest_links`). It
    does NOT write anything: you fill the prose and call `note()` with the
    returned `suggested_sources` + `suggested_connections`.

    Group sources yourself before calling — pass a set that genuinely belongs
    in one note (the audit list is aged oldest-first to help you triage).

    Args:
        sources: Vault-relative paths/wikilinks to the source(s) to compile.
            Same path conventions as `note.sources` (brackets and the
            leading `Knowledge Base/` are tolerated).
        suggested_title: Optional title override; otherwise one is derived
            from the source titles.

    Returns:
        {suggested_note_type, suggested_title, suggested_sources,
         suggested_connections, outline_markdown, warnings}.

    Errors:
        INVALID_PROPOSE (no sources); SOURCES_NOT_FOUND (none resolved).
    """
    try:
        return compile_proposal_module.propose_compilation(
            vault_root, sources=sources, suggested_title=suggested_title
        )
    except compile_proposal_module.ProposeError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e


def op_get(
    vault_root: Path,
    path: str,
    frontmatter_only: bool = False,
    include_history: bool = False,
    links: bool = False,
    include_raw: bool = False,
    max_body_chars: int | None = None,
) -> GetResponse:
    """Read / open / fetch / load the full contents of a KB or vault page by path. Returns frontmatter + body (+ raw content on request).

    Reads anywhere under the vault root — not just `Knowledge Base/`.
    This lets you cite from curated, read-only sibling folders (e.g.
    `Reference/`) kept outside Knowledge Base/ when compiling. Those are
    read-only by convention (marked in `_access.yaml`); `get` honors that
    by only reading.

    Use this when `find` gives you a path and you need the whole page
    (to cite, build on, or rewrite). `find` only returns excerpts.

    Args:
        path: Vault-relative path. Accepted shapes:
            - `Knowledge Base/Notes/Insights/foo.md`
            - `Reference/Strategy.md`
            - `Notes/Insights/foo` (auto-prepends `Knowledge Base/` if
              literal path doesn't resolve; auto-adds `.md`).
        frontmatter_only: If true, return ONLY the frontmatter (no body) —
            cheap for scanning many files by field (folds in the former
            `get_frontmatter` tool). Returns {path, frontmatter,
            has_frontmatter} instead of the full page below.
        include_history: If true, attach a `history` list — the page's
            change log from the append-only `log.md`, newest-first
            (`[{date, op, summary}]`, where `summary` is the `why:`
            rationale recorded at write time). Use this to answer "why was
            this note changed / what was the old version / show its history"
            and to verify an edit's rationale. `[]` when the page has no
            recorded edits.
        links: If true, attach a `links` summary —
            `{inbound: [...], outbound: [...]}`. `inbound` lists files whose
            wikilinks resolve to this page (each
            `{path, line_number, context, raw_target}`); `outbound` lists
            the distinct wikilink targets in this page's body. Use it to
            see a note's graph neighbourhood in one call. Default off (no
            behaviour change).
        include_raw: If true, ALSO return `content` — the raw file text
            including the frontmatter delimiters. Off by default because it
            duplicates `frontmatter` + `body` (double the tokens on every
            read) and nothing in the normal workflow needs it: edits
            round-trip `body`, and the drift guard uses `content_hash`,
            which the server always computes over the raw bytes for you.
        max_body_chars: Optional cap for the returned `body`. Use this when a
            client wants bounded content instead of an arbitrary full-page read.
            Values above 12000 are capped server-side; negative values are rejected.

    Returns:
        {path, frontmatter, body, content_hash, mtime}.
        `body` is the markdown after the frontmatter — what you feed back
        into `edit(new_body=...)`. `content_hash` is a sha256 of the raw
        file text; echo it to `edit`/`multi_edit` via `expected_hash` to
        refuse a write if the file changed on disk since this read
        (two-writer drift guard); `mtime` is advisory.
        Adds `content` (raw file text) when `include_raw=true`.
        Adds `body_truncated` and `body_chars` when `max_body_chars` is supplied.
        Adds `history` when `include_history=true`.

    Errors:
        INVALID_PATH (path escapes vault root or empty);
        NOT_FOUND (no such file); UNREADABLE (parse failure).
    """
    path = _resolve_memory_identifier(vault_root, path)
    if frontmatter_only:
        try:
            fm_result = get_frontmatter_module.get_frontmatter(
                vault_root, path=path
            )
        except get_frontmatter_module.GetFrontmatterError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        out = fm_result.as_dict()
    else:
        try:
            result = get_page_module.get_page(vault_root, path=path)
        except get_page_module.GetError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        out = result.as_dict(include_raw=include_raw)
    if max_body_chars is not None and not frontmatter_only:
        if max_body_chars < 0:
            raise ValueError("get: max_body_chars must be non-negative")
        max_body_chars = min(max_body_chars, 12000)
        body = str(out.get("body", ""))
        if len(body) > max_body_chars:
            marker = "\n\n[truncated]"
            keep = max(0, max_body_chars - len(marker))
            out["body"] = body[:keep].rstrip() + marker
            out["body_truncated"] = True
        else:
            out["body_truncated"] = False
        out["body_chars"] = len(str(out.get("body", "")))
    query_log.log_get_call(
        read_path=out["path"],
        frontmatter_only=frontmatter_only,
        include_history=include_history,
    )
    if include_history:
        out["history"] = vault.read_log_entries(vault_root, out["path"])
    if links:
        out["links"] = _link_summary(
            vault_root, out.get("path", ""), out.get("body", "")
        )
    return _attach_memory_ref(vault_root, out, str(out["path"]))


def op_edit(
    vault_root: Path,
    path: str,
    why: str,
    new_body: str | None = None,
    tags: list[str] | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    heading: str | None = None,
    section_position: str = "append",
    edits: list[multi_edit_module.EditItem] | None = None,
    row_key: str | None = None,
    take: str | None = None,
    overwrite: bool = False,
    field: str | None = None,
    value: str | int | float | bool | list | dict | None = None,
    allow_curated: bool = False,
    expected_hash: str | None = None,
    validate_only: bool = False,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Lightweight in-place edit of a page (body, tags, a surgical snippet,
    a batch, an opinion row, or one frontmatter field).

    For tweaks — typo fixes, filling a row, appending one line, tag
    corrections — without going through full supersession via `replace`.
    Use `replace` for substantial rewrites; use `edit` when creating a new
    file + superseded-link chain would be silly for what you're changing.

    One mode per call. Three param-selected modes fold in former tools:
    - `edits=[...]` -> batch surgical edits in one atomic commit (was the
      `multi_edit` tool). Each item {old_string, new_string, replace_all?}
      applies sequentially.
    - `row_key=...` + `take=...` -> fill a `[take: ]` opinion row by its
      leading text without re-sending the body (was `set_take`).
    - `field=...` + `value=...` -> patch ONE frontmatter field; pass
      `allow_curated=true` for curated trees (was `set_frontmatter_field`).
    Otherwise the default (composable) body/tags/surgical modes:
    - `new_body` — replace the WHOLE body. Heavyweight; you re-send
      everything after the frontmatter.
    - `tags` — replace the `tags:` frontmatter field.
    - `old_string`/`new_string` — **surgical** string-replace inside the
      body. Token-cheap: send only the changed snippet, not the whole
      page. Ideal for filling a `[take: ]` row or appending one opinion
      (replace a section heading with itself + the new line). `updated:`
      is always bumped to today.

    Surgical-mode rules (mirrors a precise find-and-replace):
    - `old_string` must match the file EXACTLY, including whitespace.
    - By default it must occur exactly once — an ambiguous match is an
      error (AMBIGUOUS_MATCH) so you never edit the wrong row. Pass
      `replace_all=True` to replace every occurrence.
    - Cannot be combined with `new_body` (both rewrite the body); may be
      paired with `tags`.
    - Only the inserted snippet gets wikilink-normalized; the rest of the
      body is left byte-for-byte untouched.

    What stays in all modes:
    - All other frontmatter fields (type, project, status, sources,
      superseded_by, etc.). If you need to change those, use `replace`.

    No type allowlist: any frontmatter-bearing page outside Sources/
    Evidence is editable, regardless of `type:`. Works on novel page
    types (`identity`, future types) without code changes.

    Refuses:
    - Sources/ and Evidence/ paths (rule 2: append-only). Add a
      corrective source or compile a downstream note instead.
    - Pages without a frontmatter block (won't synthesize one).
    - Pages already marked `status: superseded` (don't edit history;
      supersede the active page instead).

    Args:
        path: Vault-relative path to the compiled page (same shape as
            `get` accepts).
        why: One-line rationale for the edit. Required — lands in the
            log entry so the change is auditable.
        new_body: New markdown body (everything after frontmatter).
            Omit to keep the existing body.
        tags: New tags list (replaces existing). Lowercase dash-
            separated; the server normalizes. Omit to keep existing tags.
        old_string: Exact snippet to find in the body (surgical mode).
        new_string: Replacement snippet (required with old_string; must
            differ from it).
        replace_all: Replace every occurrence instead of requiring a
            unique match. Default False.
        heading: Section-targeted mode — the `## Heading` (the `#` markers
            are optional) under which to place `new_string`. The section
            spans from that heading to the next heading of equal-or-higher
            level (or EOF). Mutually exclusive with new_body/old_string.
            Raises HEADING_NOT_FOUND if absent.
        section_position: With `heading`, where to put `new_string`:
            "append" (default), "prepend", or "replace" the section body.
        edits: Batch-surgical mode — list of {old_string, new_string,
            replace_all?} applied sequentially in one atomic commit.
        row_key: Take-row mode — natural leading text of the row to fill
            (e.g. "Whiplash (2014)"). Requires `take`.
        take: Text to write between `[take:` and `]` (take-row mode).
        overwrite: In take-row mode, also replace an already-filled take.
        field: Frontmatter-patch mode — the single frontmatter key to set
            (cannot be `updated`, which is auto-bumped).
        value: New value for `field` (scalar/list/dict).
        allow_curated: Allow a frontmatter patch under a curated tree.
        expected_hash: Optional drift guard. Pass the `content_hash` you
            got from `get`; the edit refuses (STALE_EDIT) if the file
            changed on disk since, so you never clobber another writer.
        validate_only: Preview a surgical match without writing. Needs
            `old_string`. Reports how many rows would be hit instead of
            committing — use it before a `replace_all` to avoid an
            ambiguous match silently touching more rows than intended.
        transition_token: Exact semantic transition token returned by a
            validate-only preflight.
        relation_disposition: Reviewed relation outcome for the commit.
        relation_review_hash: Transition hash covered by reviewed-none.
        relation_review_reason: Audit reason for reviewed-none.

    Returns:
        Shape varies by mode (take-row -> {path, row, warnings};
        frontmatter-patch -> {path, field, old_value, new_value, warnings};
        batch -> {path, edits_applied, warnings}). Default mode normally
        {path, warnings}. When validate_only=True:
        {path, validate_only, mode, match_count, matches} — `matches` is
        the line(s) around each occurrence; nothing is written.

    Errors:
        INVALID_EDIT (nothing to edit, old_string+new_body both given,
        new_string missing/equal, path in Sources/Evidence); NOT_FOUND;
        STRING_NOT_FOUND (surgical snippet absent); AMBIGUOUS_MATCH
        (snippet not unique and replace_all=False); ALREADY_SUPERSEDED;
        STALE_EDIT (expected_hash mismatch — file changed since read);
        UNREADABLE.
    """
    active = [n for n, on in (
        ("edits", edits is not None),
        ("row_key", row_key is not None),
        ("field", field is not None),
    ) if on]
    if len(active) > 1:
        raise ValueError(
            f"INVALID_EDIT: one edit mode at a time; got {', '.join(active)}"
        )
    path = _resolve_memory_identifier(vault_root, path)
    try:
        if edits is not None:
            result = multi_edit_module.multi_edit(
                vault_root, path=path, why=why, edits=edits,
                expected_hash=expected_hash, validate_only=validate_only,
                semantic_transition_token=transition_token,
                relation_disposition=relation_disposition,
                relation_review_hash=relation_review_hash,
                relation_review_reason=relation_review_reason,
            )
        elif row_key is not None:
            if validate_only:
                raise ValueError(
                    "INVALID_EDIT: validate_only is not supported for row_key mode"
                )
            if take is None:
                raise ValueError("INVALID_EDIT: row_key mode requires `take`")
            result = set_take_module.set_take(
                vault_root, path=path, row_key=row_key, take=take,
                why=why, overwrite=overwrite,
            )
        elif field is not None:
            result = set_frontmatter_field_module.set_frontmatter_field(
                vault_root, path=path, field=field, value=value,
                why=why, allow_curated=allow_curated, validate_only=validate_only,
                semantic_transition_token=transition_token,
                relation_disposition=relation_disposition,
                relation_review_hash=relation_review_hash,
                relation_review_reason=relation_review_reason,
            )
        else:
            result = edit_module.edit(
                vault_root, path=path, why=why, new_body=new_body,
                tags=tags, old_string=old_string, new_string=new_string,
                replace_all=replace_all, heading=heading,
                section_position=section_position,
                expected_hash=expected_hash, validate_only=validate_only,
                semantic_transition_token=transition_token,
                relation_disposition=relation_disposition,
                relation_review_hash=relation_review_hash,
                relation_review_reason=relation_review_reason,
            )
    except (
        edit_module.EditError,
        set_take_module.SetTakeError,
        set_frontmatter_field_module.SetFrontmatterError,
    ) as e:
        msg = f"{e.code}: {e.reason}"
        if getattr(e, "missing", None):
            msg += f" (missing: {e.missing})"
        if getattr(e, "candidates", None):
            msg += f" (candidates: {e.candidates})"
        raise ValueError(msg) from e
    return result.as_dict()


def op_replace(
    vault_root: Path,
    old_path: str,
    content: str,
    note_type: str,
    title: str,
    slug: str | None = None,
    reason: str | None = None,
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    project_category: str | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Supersede an existing compiled page with a new one.

    Writes the new page at a fresh slug (via the same machinery as
    `note`), then patches the OLD page to set `status: superseded` and
    `superseded_by: "[[<new>]]"`. The NEW page gets `supersedes:
    "[[<old>]]"` in its frontmatter. The old page stays readable;
    readers follow the chain — inbound wikilinks are NOT retargeted
    (per SKILL.md rule 6).

    Use this for substantial rewrites of an existing page — not minor
    tweaks (the desk-side flow handles those better since you see a
    live diff). Cannot supersede sources or evidence (append-only).
    No type allowlist beyond the append-only guard: novel page types
    (`identity`, future types) can be superseded without code changes.

    Args:
        old_path: Vault-relative path of the page being superseded.
            Same path conventions as `get` and `find`.
        reason: Optional one-line explanation of why this replacement is
            happening; lands in the log entry body.
        (all other args): Same as the `note` tool — define the new page's
            content, type, project/projects, sources, etc.
        validate_only: Validate the replacement draft without writing either page.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        relation_disposition: Reviewed relation outcome for commit.
        relation_review_hash: Draft hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.

    Returns:
        {old_path, new_path, warnings}.

    Errors:
        INVALID_REPLACE (old is in Sources/ or Evidence/, or not a
        supersedable type); OLD_NOT_FOUND; ALREADY_SUPERSEDED
        (old page is already marked superseded).
    """
    old_path = _resolve_memory_identifier(vault_root, old_path)
    replace_kwargs = {
        "old_path": old_path,
        "reason": reason,
        "content": content,
        "note_type": note_type,
        "title": title,
        "slug": slug,
        "project": project,
        "projects": projects,
        "sources": sources,
        "tags": tags,
        "status": status,
        "severity": severity,
        "pattern_type": pattern_type,
        "domain": domain,
        "started": started,
        "duration": duration,
        "hypothesis": hypothesis,
        "n": n,
        "concluded": concluded,
        "medium": medium,
        "recorded": recorded,
        "published": published,
        "host": host,
        "editor": editor,
        "project_category": project_category,
        "draft_id": draft_id,
        "draft_token": draft_token,
        "relation_disposition": relation_disposition,
        "relation_review_reason": relation_review_reason,
    }
    try:
        predecessor_hash = _replacement_predecessor_hash(vault_root, old_path)
        if validate_only:
            result = replace_module.replace(
                vault_root,
                **replace_kwargs,
                validate_only=True,
                draft_hash=None,
                relation_review_hash=None,
            )
            if _replacement_predecessor_hash(vault_root, old_path) != predecessor_hash:
                raise ValueError(
                    "REPLACEMENT_PREVIEW_UNSTABLE: predecessor changed during advisory preview"
                )
            value = result.as_dict()
            base_hash = str(value["draft_hash"])
            value.update(
                validate_only=True,
                advisory=True,
                committed=False,
                status="preview",
                predecessor={"path": old_path, "content_hash": predecessor_hash},
                draft_hash=_replacement_review_hash(
                    base_hash,
                    predecessor_path=old_path,
                    predecessor_hash=predecessor_hash,
                ),
            )
            return value

        effective_draft_hash = draft_hash
        effective_relation_review_hash = relation_review_hash
        if draft_hash is not None:
            fresh = replace_module.replace(
                vault_root,
                **replace_kwargs,
                validate_only=True,
                draft_hash=None,
                relation_review_hash=None,
            )
            if _replacement_predecessor_hash(vault_root, old_path) != predecessor_hash:
                raise ValueError(
                    "DRAFT_HASH_MISMATCH: predecessor changed during fresh replacement validation"
                )
            base_hash = fresh.draft_hash
            expected_hash = _replacement_review_hash(
                str(base_hash),
                predecessor_path=old_path,
                predecessor_hash=predecessor_hash,
            )
            if draft_hash != expected_hash:
                raise ValueError(
                    "DRAFT_HASH_MISMATCH: replacement predecessor or draft requires fresh validation"
                )
            effective_draft_hash = base_hash
            if relation_review_hash is not None:
                if relation_review_hash != draft_hash:
                    raise ValueError(
                        "DRAFT_HASH_MISMATCH: relation review does not match replacement preview"
                    )
                effective_relation_review_hash = base_hash

        result = replace_module.replace(
            vault_root,
            **replace_kwargs,
            validate_only=False,
            draft_hash=effective_draft_hash,
            relation_review_hash=effective_relation_review_hash,
        )
    except replace_module.ReplaceError as e:
        raise ValueError(
            f"{e.code}: {e.reason} (missing: {e.missing})"
        ) from e
    except note_module.NoteError as e:
        # New-page validation failed before the supersession could land.
        raise ValueError(
            f"{e.code}: {e.reason} (missing: {e.missing})"
        ) from e
    if written_path := getattr(result, "new_path", None):
        query_log.log_write_call(
            tool="replace", written_path=written_path, cited_sources=sources
        )
    return result.as_dict()


def _replacement_predecessor_hash(vault_root: Path, old_path: str) -> str:
    try:
        return hashlib.sha256((Path(vault_root) / old_path).read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"OLD_NOT_FOUND: replacement predecessor is unavailable: {old_path}") from error


def _replacement_review_hash(
    draft_hash: str,
    *,
    predecessor_path: str,
    predecessor_hash: str,
) -> str:
    payload = json.dumps(
        {
            "schema": "exomem.replacement-preview.v1",
            "draft_hash": draft_hash,
            "predecessor_path": predecessor_path,
            "predecessor_content_hash": predecessor_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def op_link(
    vault_root: Path,
    entity_type: EntityTypeId,
    name: str,
    summary: str,
    slug: str | None = None,
    why_in_kb: str | None = None,
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    affiliation: str | None = None,
    relationship: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
) -> dict:
    """Create a typed entity under Entities/<Folder>/<Name>.md.

    Entities are typed nodes of the KB graph. The stable entity registry returned
    by `bootstrap` is authoritative for IDs, labels, folders, aliases, and capture
    guidance. Name entities after the thing they are (Title Case, not slugified):
    `Ada Lovelace`, `Agentic RAG`, `pgvector`.

    Conditional frontmatter is accepted only when the selected registry kind
    supports it: `affiliation`/`relationship`, `domain`, software metadata, or
    decision metadata. Unknown fields and unregistered kinds are refused.

    v1 is create-only. If the entity file already exists, returns
    ENTITY_EXISTS — use `replace` to supersede instead. Sub-folder index
    (e.g. Entities/Concepts/index.md categorization) is NOT auto-updated;
    reconcile via desk audit.

    Args:
        entity_type: Stable ID returned by bootstrap.entity_registry.
        name: Unicode display name stored in frontmatter and the H1.
        slug: Optional lowercase ASCII kebab-case filename component.
        summary: One-paragraph description for the `## Summary` section.
        why_in_kb: Optional `## Why in the KB` paragraph — explains what
            this entity is relevant to in your work.
        tags: Lowercase dash-separated; normalized by the server.
        connections: List of vault-relative wikilink targets to put under
            `## Relations` as conservative `relates_to` edges. Same path
            conventions as `note.sources`.
        (per-type fields): see the bullet list above.

    Returns:
        {path, warnings}.

    Errors:
        INVALID_LINK (bad entity_type, decision_status, missing required);
        ENTITY_EXISTS (update/link the returned active entity instead);
        ENTITY_AMBIGUOUS (reconcile the returned bounded candidates first).
    """
    try:
        result = link_module.link(
            vault_root,
            entity_type=entity_type,
            name=name,
            slug=slug,
            summary=summary,
            why_in_kb=why_in_kb,
            tags=tags,
            connections=connections,
            affiliation=affiliation,
            relationship=relationship,
            domain=domain,
            language=language,
            repo=repo,
            license=license,
            used_in=used_in,
            decided=decided,
            project=project,
            decision_status=decision_status,
        )
    except link_module.LinkError as e:
        suffix = f" (missing: {e.missing})"
        if e.candidates:
            suffix += f" (candidates: {e.candidates})"
        raise ValueError(f"{e.code}: {e.reason}{suffix}") from e
    return result.as_dict()


def op_preserve(
    vault_root: Path,
    scope: str,
    category: str,
    filename: str,
    content: str,
    description: str | None = None,
) -> dict:
    """Capture a TEXT artifact to Evidence/<scope>/<category>/.

    For raw factual artifacts that are text — transcripts, pasted letters,
    email bodies — preserved as-received with no analytical processing. Per
    SKILL.md rule 2, Evidence is append-only; analytical takes go in compiled
    notes that link to the evidence file.

    BINARY artifacts (PDFs, images, .docx — any non-text file) are delivered
    out-of-band, not through this tool: call `mint_upload_token` and POST the
    bytes to `/upload`, or drop the file into Evidence/ desk-side via Obsidian
    Sync. The bytes never pass through the model.

    Args:
        scope: Incident or domain key (e.g. "project-alpha", "incident-2026-04").
            Creates the subfolder if it doesn't exist.
        category: Sub-category within scope (e.g. "letters", "labs",
            "court-docs"). Creates the subfolder if it doesn't exist.
        filename: The artifact's filename including extension
            (e.g. `2026-04-15-statement.txt`).
        content: UTF-8 text to preserve as-received.
        description: Optional. If supplied, a sidecar `<filename>.md`
            is written alongside the artifact with frontmatter and the
            description under `## Description`.

    Returns:
        {path, sidecar_path, warnings}.

    Errors:
        INVALID_PRESERVE (missing required); ARTIFACT_EXISTS (file already
        exists — Evidence is append-only, pick a new filename).
    """
    preserve_module = _preserve_module()
    try:
        result = preserve_module.preserve(
            vault_root,
            scope=scope,
            category=category,
            filename=filename,
            content=content,
            description=description,
        )
    except preserve_module.PreserveError as e:
        raise ValueError(
            f"{e.code}: {e.reason} (missing: {e.missing})"
        ) from e
    return result.as_dict()


def op_note(
    vault_root: Path,
    content: str,
    note_type: str,
    title: str,
    slug: str | None = None,
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    suggestions: bool = True,
    project_category: str | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Create a compiled note in the Knowledge Base.

    Use this for distilled thinking — not raw capture. For raw capture
    (an article you read, a session transcript), use `add` instead.

    Six note types:
    - `research-note`: project-scoped findings. `project` REQUIRED.
      → `Notes/Research/<Project>/<slug>.md`
    - `insight`: cross-cutting claim. Optional `projects` (plural).
      → `Notes/Insights/<slug>.md`
    - `failure`: documented failure mode. Optional `projects`, optional
      `severity` ∈ {minor, moderate, serious, critical}.
      → `Notes/Failures/<slug>.md`
    - `pattern`: reusable cross-cutting pattern. Optional `projects`,
      optional `pattern_type` ∈ {architectural, workflow, prompting,
      governance, pedagogical}.
      → `Notes/Patterns/<slug>.md`
    - `experiment`: hypothesis + protocol. `domain`, `started` (YYYY-MM-DD),
      and `duration` (e.g. "30 days", "ongoing") REQUIRED. Optional
      `hypothesis`,
` (default 1), `concluded`.
      → `Notes/Experiments/<domain>/YYYY-MM-<slug>.md`
    - `production-log`: creative artifact log. `medium` REQUIRED (e.g.
      "Posts", "Articles"). Optional `recorded`, `published`, `host`,
      `editor`, `projects`. Status enum is richer: {planned, recorded,
      edited, published, reflected, dropped, archived}; defaults to
      `planned`.
      → `Notes/Productions/<medium>/YYYY-MM-<slug>.md`

    For each `sources:` wikilink, appends this note's wikilink to that
    source's `ingested_into:` frontmatter (maintaining the source→note graph).

    The result includes `write_feedback`: deterministic structural feedback with
    semantic block counts, source/link counts, unresolved wikilink warnings, and
    suggested next actions. Treat it as write-shape feedback, not semantic truth.

    Args:
        content: Markdown body written after frontmatter. The writer adds or
            normalizes the leading `# <title>` H1, followed by the section
            conventions per type:
            research-note: `## Question`/`## Findings`/`## Relations`.
            insight: `## Claim`/`## Why it holds`/`## Relations`.
            failure: `## What happened`/`## Mechanism`/`## Detection`/`## Mitigation`/`## Relations`.
            pattern: `## Problem`/`## Solution`/`## When to use`/`## When NOT to use`/`## Relations`.
            experiment: `## Hypothesis`/`## Protocol`/`## Baseline`/`## Intervention`/`## Results`/`## Conclusion`/`## Relations`.
            production-log: `## Frame`/`## Artifact`/`## Production session`/`## Outcomes`/`## Reflection`/`## Relations`.
            Conventions only — no shape is enforced.
        note_type: One of research-note, insight, failure, pattern,
            experiment, production-log.
        title: Unicode display title stored in frontmatter and the H1.
        slug: Optional lowercase ASCII kebab-case filename component.
            Experiments and production-logs auto-prefix with YYYY-MM.
        project: REQUIRED for research-note. __PROJECT_KEYS_HINT__
        projects: List of project keys (plural). Optional for insight,
            failure, pattern, production-log. __PROJECT_KEYS_HINT__
        sources: Vault-relative wikilinks to existing pages this note draws
            from, e.g. `["Knowledge Base/Sources/Articles/2026-05-18-foo"]`
            or `["[[Knowledge Base/Sources/Articles/2026-05-18-foo]]"]`.
            Brackets and the leading `Knowledge Base/` are tolerated.
        tags: Lowercase dash-separated; the server normalizes case/spacing.
        status: Defaults to `active` for most types, `planned` for
            production-log. Valid set varies by type.
        severity: failure only. {minor, moderate, serious, critical}.
        pattern_type: pattern only. {architectural, workflow, prompting,
            governance, pedagogical}.
        domain: experiment only. Becomes the subfolder name (lowercased).
        started: experiment only. YYYY-MM-DD when the experiment began.
        duration: experiment only. Freeform, e.g. "30 days", "ongoing".
        hypothesis: experiment only. One-line claim being tested.
        n: experiment only. Sample size. Defaults to 1 (n-of-1).
        concluded: experiment only. YYYY-MM-DD when it ended (absent while ongoing).
        medium: production-log only. Subfolder, e.g. "Posts", "Articles".
        recorded: production-log only. YYYY-MM-DD of recording session.
        published: production-log only. YYYY-MM-DD of publication.
        host: production-log only. Creator/talent name.
        editor: production-log only. Producer/editor name.

        suggestions: When true (default), the result carries a `suggestions`
            block: existing pages this note should probably link to, ranked
            by the retrieval stack. Set false for a faster write when you
            already know the note's links; the near-duplicate/overlap
            warnings stay ON either way (dedupe is a guardrail, not a
            suggestion). For important drafts, call
            `connect_memory(operation="suggest-links")`, use
            `operation="suggest-relations"` when direction matters, and write
            accepted note-level edges under `## Relations`.
        validate_only: Validate and return an immutable creation draft without writing.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        relation_disposition: Reviewed relation outcome for commit, usually
            reviewed_none when no honest relation exists.
        relation_review_hash: Draft hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.

    Returns:
        {path, warnings, suggestions?, write_feedback}. `write_feedback` is
        deterministic structural feedback: semantic block counts, source/link
        counts, unresolved wikilink warnings, and suggested next actions. On
        validation failure, raises a structured error with code=INVALID_NOTE,
        the missing fields, and the reason.
    """
    try:
        result = note_module.note(
            vault_root,
            content=content,
            note_type=note_type,
            title=title,
            slug=slug,
            project=project,
            projects=projects,
            sources=sources,
            tags=tags,
            status=status,
            severity=severity,
            pattern_type=pattern_type,
            domain=domain,
            started=started,
            duration=duration,
            hypothesis=hypothesis,
            n=n,
            concluded=concluded,
            medium=medium,
            recorded=recorded,
            published=published,
            host=host,
            editor=editor,
            suggestions=suggestions,
            project_category=project_category,
            validate_only=validate_only,
            draft_id=draft_id,
            draft_hash=draft_hash,
            draft_token=draft_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    except note_module.NoteError as e:
        raise ValueError(
            f"{e.code}: {e.reason} (missing: {e.missing})"
        ) from e
    if written_path := getattr(result, "path", None):
        query_log.log_write_call(
            tool="note", written_path=written_path, cited_sources=sources
        )
    return result.as_dict()


def op_query_data(
    vault_root: Path,
    path: str,
    record_path: str | None = None,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 100,
    offset: int = 0,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
) -> dict:
    """Tier 2: structured query over a CSV/JSON data file under the vault.

    The retrieval half of the data-search pattern — `find` surfaces a
    dataset's markdown "card"; this reads the raw file the card points at
    and returns exact rows / aggregates (no whole-file dump). KB datasets
    are small, so it reads on demand — no index, no new infra.

    Formats: CSV / TSV, and JSON (a top-level array, or a nested array via
    `record_path` / common-key auto-detect). Column names may be dotted to
    reach nested JSON fields (e.g. "performer.name", "id.extension")
    anywhere a column is named (filters / columns / sort / aggregate).

    Args:
        path: vault-relative path to the `.csv` / `.tsv` / `.json` file.
        record_path: (JSON) dotted path to the array inside a nested
            object, e.g. "sections.work_incapacity". Omit for a top-level
            array or the common keys result/results/data/rows/items/entries.
        filters: list of `{column, op, value}`. `op` ∈ eq, ne, gt, gte, lt,
            lte, contains, icontains, startswith, in, nin, exists, missing.
            Numeric compares coerce tolerantly (comma decimals; lab
            operators like "<0.4"/">75" are stripped for the comparison).
        columns: project to these columns (dotted ok). Omit for all.
        sort_by / descending: sort by a column (numeric-aware).
        limit / offset: pagination (limit default 100, hard cap 1000).
        aggregate: instead of rows — "count"; "func:column" where func ∈
            min, max, sum, avg, latest, distinct; or "profile" to get a
            deterministic content profile (per-column kind, distinct values,
            numeric ranges, date span) under `aggregate.profile` PLUS a
            ready-to-write markdown dataset card under `aggregate.dataset_card`.
            Use "profile" to make a CSV/JSON findable — write the card into
            the KB (fill in its "What this holds" line) so the dataset is
            discoverable by content without ever embedding its raw rows.
        date_from / date_to / date_column: convenience date-range filter on
            `date_column` (defaults to a "date" column if present); ISO
            date strings, compared lexicographically.

    Returns:
        {path, format, total_rows, total_matched, returned, columns, rows,
         aggregate, truncated, warnings}.

    Errors: INVALID_PATH / NOT_FOUND (path); UNSUPPORTED_FORMAT; TOO_LARGE;
        BAD_JSON; BAD_RECORD_PATH; BAD_FILTER; BAD_OP; BAD_AGGREGATE.
    """
    try:
        result = query_data_module.query_data(
            vault_root,
            path=path,
            record_path=record_path,
            filters=filters,
            columns=columns,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
            aggregate=aggregate,
            date_from=date_from,
            date_to=date_to,
            date_column=date_column,
        )
    except query_data_module.QueryDataError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_create_file(
    vault_root: Path,
    path: str,
    content: str = "",
    frontmatter: dict | None = None,
    overwrite: bool = False,
    allow_curated: bool = False,
    kind: str = "file",
    parents: bool = True,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Tier 2: write a file — or, with `kind="dir"`, create a folder — at an
    arbitrary vault path.

    With `kind="dir"`, this creates a folder (mkdir -p when `parents=true`)
    and ignores `content`/`frontmatter`/`overwrite` (folds in the former
    `create_directory` tool); returns {path, created, warnings}.

    Escape hatch for files that don't fit Tier 1 type routing — new folder
    structures (`Identity/`, `Templates/`), skill files, scratch. For
    typed notes use `note`/`add`/`link`/`preserve`.

    If `frontmatter` is a dict, this op prepends a YAML block built from
    it (and auto-fills `created`/`updated` to today if not provided);
    `content` is the body in that case. If `frontmatter` is omitted,
    `content` is written verbatim — the caller is responsible for any
    frontmatter already in it.

    Refuses:
    - Sources/, Evidence/ (append-only — use `add` or `preserve`).
    - Subtrees marked `readonly`/`excluded` in `_access.yaml` (curated,
      read-only material) — a hard refusal with no override.
    - Existing files unless `overwrite=true`.

    Args:
        path: Vault-relative, e.g. `Knowledge Base/Identity/Career.md`.
            Forward or back slashes accepted. Path-escape guarded.
        content: File body (or full file if `frontmatter` is None). Text
            only; for binaries use the /upload endpoint.
        frontmatter: Optional dict prepended as YAML frontmatter.
        overwrite: If true, replace existing file. Default false.
        allow_curated: Required to write under a curated tree. Default false.
        kind: "file" (default) or "dir". With "dir", creates a folder
            instead of a file (former `create_directory`).
        parents: In "dir" mode, create intermediate folders (mkdir -p).
            Default true.
        validate_only: Validate a Markdown file creation/overwrite without writing.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        relation_disposition: Reviewed relation outcome for semantic file creation.
        relation_review_hash: Draft hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.

    Returns: {path, warnings} for files; {path, created, warnings} for dirs.
    Errors: INVALID_PATH; APPEND_ONLY; CURATED_PROTECTED; FILE_EXISTS;
            NOT_A_FILE; (dir mode) NOT_A_DIR; MISSING_PARENT; MKDIR_FAILED.
    """
    if kind == "dir":
        if validate_only or any(
            value is not None
            for value in (
                draft_id,
                draft_hash,
                draft_token,
                relation_disposition,
                relation_review_hash,
                relation_review_reason,
            )
        ):
            raise ValueError(
                "INVALID_CREATE: creation review fields apply only to kind='file'"
            )
        try:
            result = create_directory_module.create_directory(
                vault_root,
                path=path,
                parents=parents,
                allow_curated=allow_curated,
            )
        except create_directory_module.CreateDirectoryError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()
    try:
        result = create_file_module.create_file(
            vault_root,
            path=path,
            content=content,
            frontmatter=frontmatter,
            overwrite=overwrite,
            allow_curated=allow_curated,
            validate_only=validate_only,
            draft_id=draft_id,
            draft_hash=draft_hash,
            draft_token=draft_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    except create_file_module.CreateFileError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_overview(
    vault_root: Path,
    path: str = "",
    max_depth: int = 3,
    include_hidden: bool = False,
    samples: int = 5,
) -> dict:
    """Bounded, read-only structure report of the vault (or a subtree).

    Answers "what does this vault look like?" in ONE call — use this instead
    of reading files one by one for structural questions. Reports totals,
    whether a `Knowledge Base/` tree is present, a depth/breadth-capped folder
    tree (per-folder file counts, frontmatter coverage %, wikilink/md-link
    counts, dominant filename patterns, sample names), junk candidates
    (zero-byte files, sync-conflict duplicates like `note 2.md`), largest and
    oldest-unmodified files, and exactly what was skipped. Lists are capped;
    counts are always exact. Works on vaults with no initialized
    `Knowledge Base/` (`kb.present` false).

    Args:
        path: Vault-relative subtree to report on. Empty string (default)
            reports the whole vault. Auto-handles forward/back slashes.
        max_depth: Tree depth cap; deeper folders roll up into their
            ancestors (counts stay exact). Default 3.
        include_hidden: If true, include dot-directories/dotfiles and
            `_trash`/`_attachments`. Default false.
        samples: Filename samples listed per folder. Default 5.

    Returns: {scope_note, root, totals, kb, tree, junk, largest,
             oldest_unmodified, skipped, warnings}.

    Errors: INVALID_PATH; NOT_FOUND; NOT_A_DIR.
    """
    try:
        return overview_module.overview(
            vault_root,
            path=path,
            max_depth=max_depth,
            include_hidden=include_hidden,
            samples=samples,
        )
    except overview_module.OverviewError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e


def op_adopt(
    vault_root: Path,
    path: str = "",
    mode: str = "scan-only",
    max_depth: int = overview_module.DEFAULT_MAX_DEPTH,
    include_hidden: bool = False,
    samples: int = 5,
    pack_limit: int = 6,
    manifest_path: str | None = None,
    selected_paths: list[str] | None = None,
    semantic_max_files: int = semantic_census.DEFAULT_MAX_FILES,
    semantic_max_bytes: int = semantic_census.DEFAULT_MAX_BYTES,
    semantic_example_limit: int = semantic_census.DEFAULT_EXAMPLE_LIMIT,
) -> dict:
    """Adopt / import an existing vault safely: scan first, preserve originals.

    This is the product-facing first step for a vault that already contains
    notes, media, records, or project folders. `mode="scan-only"` returns a
    bounded read-only adoption report: what Exomem found, which content is
    governed versus read-only input, likely knowledge packs, and safe next
    actions. Explicit write modes only write under `Knowledge Base/`:
    `save-manifest` saves the report, `copy-as-sources` copies selected
    legacy text files into governed Sources with original path/hash provenance,
    and `compile-selected` copies selected legacy files when needed then returns
    a reviewable compile plan. It does not create compiled notes automatically.

    Use this when the user says "import my vault", "adopt these notes", "make
    this existing knowledge base usable", or asks what Exomem would do with an
    old folder before committing to migration.

    Args:
        path: Optional vault subtree to scan. Defaults to the vault root.
        mode: Adoption mode: scan-only, save-manifest, copy-as-sources, or compile-selected.
        max_depth: Folder-tree depth cap for the scan.
        include_hidden: Include hidden files/directories in the scan.
        samples: Sample filename count per folder.
        pack_limit: Maximum knowledge-pack suggestions to return.
        manifest_path: Optional markdown destination under Knowledge Base/ for
            save-manifest. A default under _Adoption/ is used when omitted.
        selected_paths: Explicit vault-relative legacy files for copy-as-sources or compile-selected.
        semantic_max_files: Maximum Markdown files read by the scan-only semantic census.
        semantic_max_bytes: Maximum total Markdown bytes read by the scan-only semantic census.
        semantic_example_limit: Maximum examples per semantic census grouping.
    """
    try:
        return adopt_module.adopt(
            vault_root,
            path=path,
            mode=mode,
            max_depth=max_depth,
            include_hidden=include_hidden,
            samples=samples,
            pack_limit=pack_limit,
            manifest_path=manifest_path,
            selected_paths=selected_paths,
            semantic_max_files=semantic_max_files,
            semantic_max_bytes=semantic_max_bytes,
            semantic_example_limit=semantic_example_limit,
        )
    except adopt_module.AdoptError as e:
        raise ValueError(f"adopt: {e.code}: {e.reason}") from e

def op_list_directory(
    vault_root: Path,
    path: str = "",
    recursive: bool = False,
    include_hidden: bool = False,
) -> dict:
    """Tier 2: list files and subfolders at a vault path. Read-only.

    Works anywhere under vault root including curated trees (consistent
    with `get`). For .md files, surfaces the frontmatter `type` field
    so callers can scan typed content quickly.

    Args:
        path: Vault-relative. Empty string lists vault root. Auto-handles
            forward/back slashes.
        recursive: If true, walk subfolders. Default false.
        include_hidden: If true, include dotfiles and _attachments/.
            Default false.

    Returns: {path, entries: [{name, type, path, size_bytes, updated,
             frontmatter_type}]}.

    Errors: INVALID_PATH; NOT_FOUND; NOT_A_DIR.
    """
    try:
        result = list_directory_module.list_directory(
            vault_root,
            path=path,
            recursive=recursive,
            include_hidden=include_hidden,
        )
    except list_directory_module.ListDirectoryError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_move_file(
    vault_root: Path,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
) -> dict:
    """Tier 2: relocate a file, optionally rewriting inbound wikilinks.

    Refuses moves out of OR into Sources/ and Evidence/ (append-only).
    Curated trees on either end need `allow_curated=true`. Refuses to
    overwrite existing destinations.

    When `update_wikilinks=true` (default), scans the full vault for
    `[[<old>]]`, `[[<old.md>]]`, and `[[<old_basename>]]` (only when the
    basename is unique vault-wide) and rewrites them to point at the
    new location. Preserves full-form vs stripped-form per link.

    Args:
        old_path: Vault-relative source.
        new_path: Vault-relative destination (must not exist).
        update_wikilinks: Default true.
        allow_curated: Required if either end is in a curated tree.

    Returns: {old_path, new_path, wikilinks_updated, files_touched, warnings}.
    Errors: INVALID_PATH; NOT_FOUND; DEST_EXISTS; APPEND_ONLY;
            CURATED_PROTECTED.
    """
    old_path = _resolve_memory_identifier(vault_root, old_path)
    try:
        result = move_file_module.move_file(
            vault_root,
            old_path=old_path,
            new_path=new_path,
            update_wikilinks=update_wikilinks,
            allow_curated=allow_curated,
        )
    except move_file_module.MoveFileError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_delete(
    vault_root: Path,
    path: str,
    confirm: bool,
    recursive: bool = False,
    force_orphan: bool = False,
    force_superseded: bool = False,
    allow_curated: bool = False,
    expected_dead_inbound: list[str] | None = None,
) -> dict:
    """Tier 2: trash a file OR folder (auto-detected). Reversible — moves to
    _trash/, not /dev/null.

    Dispatches on the path: a directory is trashed whole (needs
    `recursive=true` if non-empty; folds in the former `delete_directory`),
    otherwise a single file. `force_superseded`/`expected_dead_inbound`
    apply to files; `recursive` applies to folders.

    Deletes are NEVER permanent at this layer. The file moves to
    `Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>.md`
    with a `.meta.json` sidecar capturing original path, timestamp,
    inbound link count, and which force-flags were used. Recovery is
    `move_file` from the trash path back. Permanent removal happens
    desk-side via `rm Knowledge Base/_trash/...`.

    Per SKILL.md rule 6, supersession via `replace` is still preferred
    for compiled material. Use this op for scratch, mistakes outside the
    typed-note set, and cleanup of files that genuinely shouldn't exist.

    Refuses:
    - Sources/, Evidence/ (append-only).
    - Files already in `_trash/` (already trashed — recover via move_file).
    - Curated trees unless `allow_curated=true`.
    - When `confirm=false`.
    - When `superseded_by:` is set (history) unless `force_superseded=true`.
    - When inbound wikilinks exist (after `expected_dead_inbound` filtering)
      unless `force_orphan=true`.

    Args:
        path: Vault-relative.
        confirm: Must be `true` explicitly. Marks the action deliberate.
        recursive: For a non-empty FOLDER, required to confirm you know it
            has contents. Ignored for files.
        force_orphan: Allow trash even if inbound wikilinks exist.
        force_superseded: Allow trash of a file in the supersession chain.
        allow_curated: Required to trash under a curated tree.
        expected_dead_inbound: Vault-relative paths whose inbound links
            to this file should be ignored. Use when you're trashing
            multiple files in one workflow (e.g. cleaning a supersession
            chain) and don't want each step to false-positive on
            links that will die in the same batch.

    Returns (file): {path, trash_path, inbound_link_count,
            inbound_ignored_count, warnings}.
    Returns (dir): {path, trash_path, file_count, inbound_link_count,
            warnings}.
    Errors: UNCONFIRMED; INVALID_PATH; NOT_FOUND; ALREADY_TRASHED;
            APPEND_ONLY; CURATED_PROTECTED; SUPERSEDED_HISTORY;
            INBOUND_LINKS; TRASH_FAILED; (dir) NOT_A_DIR; NOT_EMPTY.
    """
    path = _resolve_memory_identifier(vault_root, path)
    try:
        abs_path, _rel = resolve_under_vault(vault_root, path)
        is_dir = abs_path.is_dir()
    except VaultPathError:
        is_dir = False  # let the file backend raise the precise path error
    try:
        if is_dir:
            result = delete_directory_module.delete_directory(
                vault_root,
                path=path,
                confirm=confirm,
                recursive=recursive,
                force_orphan=force_orphan,
                allow_curated=allow_curated,
            )
        else:
            result = delete_file_module.delete_file(
                vault_root,
                path=path,
                confirm=confirm,
                force_orphan=force_orphan,
                force_superseded=force_superseded,
                allow_curated=allow_curated,
                expected_dead_inbound=expected_dead_inbound,
            )
    except (
        delete_file_module.DeleteFileError,
        delete_directory_module.DeleteDirectoryError,
    ) as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_append_to_file(
    vault_root: Path,
    path: str,
    content: str,
    allow_curated: bool = False,
    validate_only: bool = False,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Tier 2: append text to an existing file.

    Refuses Sources/ (immutable). Allowed on Evidence/ sidecars and
    general vault files. Curated trees need `allow_curated=true`.
    Ensures a single newline boundary between existing tail and new
    content.

    Args:
        path: Vault-relative.
        content: Text to append (text only; binaries go via /upload).
        allow_curated: Required under curated trees.
        validate_only: Validate the complete Markdown result without writing.
        semantic_transition_token: Opaque transition token returned by validate_only.
        relation_disposition: Reviewed relation outcome for a semantic append.
        relation_review_hash: Transition hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.

    Returns: {path, bytes_appended, warnings}.
    Errors: INVALID_APPEND; INVALID_PATH; NOT_FOUND; NOT_A_FILE;
            APPEND_ONLY; CURATED_PROTECTED.
    """
    try:
        result = append_to_file_module.append_to_file(
            vault_root,
            path=path,
            content=content,
            allow_curated=allow_curated,
            validate_only=validate_only,
            semantic_transition_token=semantic_transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    except append_to_file_module.AppendError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_list_trash(
    vault_root: Path,date: str | None = None) -> dict:
    """Tier 2: enumerate recoverable trash entries. Read-only.

    Walks Knowledge Base/_trash/YYYY-MM-DD/ and parses each .meta.json
    sidecar. Returns entries most-recent-first with original path,
    timestamp, kind (file or directory), and which force-flags fired
    at trash time. Also surfaces drift: orphan_sidecars (sidecars with
    no target file) and orphan_files (trashed files with no sidecar).
    Pair with `recover_from_trash` to undo.

    Args:
        date: Optional YYYY-MM-DD filter to scope to one day.

    Returns: {entries: [{trash_path, meta_path, original_path,
             trashed_at, kind, file_count, ...}], count,
             orphan_sidecars, orphan_files}.
    """
    result = list_trash_module.list_trash(vault_root, date=date)
    return result.as_dict()


def op_recover_from_trash(
    vault_root: Path,
    trash_path: str,
    restore_path: str | None = None,
    allow_curated: bool = False,
) -> dict:
    """Tier 2: undo a delete_file/delete_directory.

    Reads the .meta.json sidecar to discover where the file lived
    before being trashed, moves it back there, and cleans up the
    sidecar. If `restore_path` is provided, uses that instead of the
    sidecar's original location (useful when the original parent
    directory has been removed).

    Refuses to overwrite existing files at the restore destination.
    Refuses restore into Sources/Evidence (append-only). Curated trees
    need `allow_curated=true`.

    Args:
        trash_path: Vault-relative path to the trashed entry
            (under `Knowledge Base/_trash/...`).
        restore_path: Optional override; defaults to the original
            location from the sidecar.
        allow_curated: Required if restoring into a curated tree.

    Returns: {trash_path, restored_path, kind, warnings}.
    Errors: INVALID_PATH; NOT_FOUND; NOT_IN_TRASH; NO_RESTORE_PATH;
            RESTORE_INTO_TRASH; APPEND_ONLY; CURATED_PROTECTED;
            DEST_EXISTS; RECOVER_FAILED.
    """
    try:
        result = recover_from_trash_module.recover_from_trash(
            vault_root,
            trash_path=trash_path,
            restore_path=restore_path,
            allow_curated=allow_curated,
        )
    except recover_from_trash_module.RecoverError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_list_inbound_links(vault_root: Path, target: str) -> dict:
    """Tier 2: find files whose wikilinks resolve to `target`. Read-only.

    Useful before `move_file` (preview what update_wikilinks will touch)
    or `delete_file` (preview what would break). Matches three forms:
    - Full path: `[[Knowledge Base/Notes/Insights/foo]]`
    - KB-stripped: `[[Notes/Insights/foo]]`
    - Bare basename (only when unique vault-wide): `[[foo]]`

    Args:
        target: Vault-relative path or bare basename. `.md` optional.

    Returns: {target, inbound: [{path, line_number, context, raw_target}],
             count}.
    Errors: INVALID_TARGET; INVALID_PATH.
    """
    target = _resolve_memory_identifier(vault_root, target)
    try:
        result = list_inbound_links_module.list_inbound_links(
            vault_root, target=target
        )
    except list_inbound_links_module.ListInboundLinksError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    return result.as_dict()


def op_get_video_frames(
    vault_root: Path,
    path: str,
    max_frames: int = 8,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> ToolResult:
    """View / analyze / look inside a vault video: sampled keyframes returned INLINE as images — no download round-trip.

    Use this to see what a video actually contains (slides, screen
    recordings, whiteboards, meetings) directly in the tool result. Frames
    are sampled evenly across the video (or the requested window),
    near-duplicates are collapsed, and each frame comes back as a JPEG
    image content block, preceded by one metadata block with per-frame
    timestamps. Typical loop: overview call first, then zoom into a moment
    with `start_sec`/`end_sec` — e.g. around a `find` hit's
    `clip_match_at`/`scene_match_at` timestamp.

    Args:
        path: Vault-relative path to a video file
            (`.mp4 .mov .mkv .webm .avi .m4v .wmv .flv .mpeg .mpg`).
        max_frames: Maximum frames to return. Default 8, hard-capped
            server-side (16); out-of-range values clamp silently and the
            metadata reports `max_frames_effective`. Each frame costs
            image tokens — prefer a time window over raising this.
        start_sec: Optional window start in seconds — sample at/after this
            timestamp only.
        end_sec: Optional window end in seconds — sample before this
            timestamp only (clamped to the video's duration).

    Returns:
        A metadata block {path, duration_sec, start_sec, end_sec,
        frame_count, frames: [{index, timestamp_sec}], candidates,
        dedup_dropped, max_frames_effective} followed by one JPEG image
        content block per frame in `frames[].index` order (longest side
        ≤768px).

    Errors:
        INVALID_PATH (escapes vault or empty); NOT_FOUND (no such file);
        NOT_A_VIDEO (not a video extension); BAD_RANGE (invalid window);
        VIDEO_DEPS_MISSING (server installed without the media extra);
        NO_DECODABLE_FRAMES (corrupt/streamless video, or a window on a
        video of unknown duration).
    """
    video_frames_module = _video_frames_module()
    try:
        result = video_frames_module.get_frames(
            vault_root,
            path,
            max_frames=max_frames,
            start_sec=start_sec,
            end_sec=end_sec,
        )
    except video_frames_module.VideoFramesError as e:
        raise ValueError(f"{e.code}: {e.reason}") from e
    meta = {
        "path": result.path,
        "duration_sec": result.duration_sec,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "frame_count": len(result.frames),
        "frames": [
            {"index": i, "timestamp_sec": f.timestamp_sec}
            for i, f in enumerate(result.frames)
        ],
        "candidates": result.candidates,
        "dedup_dropped": result.dedup_dropped,
        "max_frames_effective": result.max_frames_effective,
    }
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))]
        + [
            FastMCPImage(data=f.jpeg, format="jpeg").to_image_content()
            for f in result.frames
        ],
        structured_content=meta,
    )



# ----- product command wrappers: public surface over canonical leaves -----

def op_ask_memory(
    vault_root: Path,
    query: str = "",
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    result_level: str = "auto",
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    detail: str = "compact",
    deep: bool = False,
    graph: bool = True,
    rerank: bool | None = None,
    rerank_max_candidates: _RerankCandidateLimit | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    graph_enrich: bool = False,
    include_timings: bool = False,
    explain: bool = False,
) -> list[RetrievalHit] | FindEnvelope:
    """Recall durable knowledge from Exomem with product defaults.

    This is the normal first read: search compiled knowledge, sources,
    evidence, media sidecars, and curated vault files without making the
    caller choose internal primitives. Set `deep=true` to return a packed
    reasoning context instead of only hits. Heavy behavior stays explicit:
    rerank is only forced when `rerank=true`, and graph enrichment is only
    requested when `graph_enrich=true`.

    Args:
        query: Question or search phrase. Empty means recent/filtered recall.
        types: Optional page-type filters.
        projects: Optional project-key filters.
        tags: Optional tag filters.
        speakers: Optional diarized speaker filters.
        file_types: Optional artifact kind filters such as pdf, image, csv, json.
        exclude_file_types: Optional artifact kinds to exclude.
        categories: Semantic-unit category shortcuts, such as config or rule.
        kinds: Semantic-unit kind shortcuts, such as decision or claim.
        filters: Structured page/unit metadata filters.
        result_level: auto, page, unit, or mixed.
        limit: Max hits. Default 15.
        scope: kb, vault, or kb-only.
        mode: hybrid, keyword, or vector.
        detail: compact or full hit detail.
        deep: Return a packed context for reasoning.
        graph: Include graph-neighbour ranking in hybrid/vector search.
        rerank: Force or suppress cross-encoder reranking; omit for mode-aware auto.
        rerank_max_candidates: Bound scorer input to an integer from the effective
            result limit through 300; omission preserves the existing prefix.
        prefer_compiled: Prefer compiled notes over raw sources by default.
        prefer_active: Prefer active conclusions over superseded ones.
        prefer_used: Apply usage boost when explicitly requested.
        graph_enrich: With deep mode, include typed graph neighborhood data.
        include_timings: Include retrieval timings for diagnostics.
        explain: Add bounded retrieval-plan and per-hit ranking evidence.
    """
    result = op_find(
        vault_root,
        query=query,
        types=types,
        projects=projects,
        tags=tags,
        speakers=speakers,
        file_types=file_types,
        exclude_file_types=exclude_file_types,
        categories=categories,
        kinds=kinds,
        filters=filters,
        result_level=result_level,
        limit=limit,
        scope=scope,
        mode=mode,
        graph=graph,
        rerank=rerank,
        rerank_max_candidates=rerank_max_candidates,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        prefer_used=prefer_used,
        pack=deep,
        graph_enrich=graph_enrich,
        detail=detail,
        include_timings=include_timings,
        explain=explain,
    )
    return result


def op_read_memory(
    vault_root: Path,
    path: str,
    frontmatter_only: bool = False,
    include_history: bool = False,
    links: bool = False,
    include_raw: bool = False,
    unit_ref: str | None = None,
) -> dict:
    """Read one memory page or one exact semantic unit by reference.

    Use after `ask_memory` chooses a hit, or when a caller already knows the
    path. With `unit_ref`, returns that exact current semantic unit, its parent
    citation/lifecycle, and at most 2,400 characters of surrounding Markdown.
    Missing, stale, ambiguous, and superseded references are reported through
    the response `status`; no nearby unit is silently substituted. Without
    `unit_ref`, this preserves the existing page-read response exactly.

    Args:
        path: Vault-relative path or Knowledge Base-relative shorthand.
        frontmatter_only: Return only frontmatter for cheap scanning.
        include_history: Include recorded edit/supersession history.
        links: Include inbound and outbound wikilink summaries.
        include_raw: Include the raw markdown file text.
        unit_ref: Exact unit reference returned by unit-level recall. Page-only
            expansion flags are not accepted together with an exact unit read.
    """
    if unit_ref is not None:
        if frontmatter_only or include_history or links or include_raw:
            raise ValueError(
                "INVALID_UNIT_READ_OPTIONS: unit_ref cannot be combined with "
                "frontmatter_only, include_history, links, or include_raw"
            )
        resolved_path = _resolve_memory_identifier(vault_root, path)
        try:
            page = get_page_module.get_page(vault_root, path=resolved_path)
        except get_page_module.GetError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        query_log.log_get_call(
            read_path=page.path,
            frontmatter_only=False,
            include_history=False,
        )
        return semantic_unit_read_module.read_semantic_unit(
            vault_root,
            page=page,
            unit_ref=unit_ref,
        ).as_dict()
    return op_get(
        vault_root,
        path=path,
        frontmatter_only=frontmatter_only,
        include_history=include_history,
        links=links,
        include_raw=include_raw,
    )


def op_browse_memory(
    vault_root: Path,
    path: str = "",
    mode: str = "overview",
    max_depth: int = 3,
    include_hidden: bool = False,
    samples: int = 5,
    recursive: bool = False,
) -> dict:
    """Browse vault structure without reading many files.

    `mode="overview"` returns a bounded product adoption/structure report.
    `mode="list"` returns entries for a folder. Both are read-only.

    Args:
        path: Vault-relative subtree. Empty means vault root.
        mode: overview or list.
        max_depth: Overview tree depth cap.
        include_hidden: Include dotfiles and hidden/system folders.
        samples: Filename samples per folder for overview mode.
        recursive: In list mode, walk subfolders.
    """
    if mode == "overview":
        return op_overview(
            vault_root,
            path=path,
            max_depth=max_depth,
            include_hidden=include_hidden,
            samples=samples,
        )
    if mode == "list":
        return op_list_directory(
            vault_root,
            path=path,
            recursive=recursive,
            include_hidden=include_hidden,
        )
    raise ValueError("INVALID_MODE: browse_memory mode must be 'overview' or 'list'")


def op_remember(
    vault_root: Path,
    content: str,
    title: str,
    slug: str | None = None,
    note_type: str = "insight",
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    suggestions: bool = True,
    project_category: str | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Remember a durable conclusion as compiled governed knowledge.

    This is for distilled thinking, decisions, findings, failures, patterns,
    experiments, and production logs. Raw material belongs in `capture_source`;
    proof artifacts belong in `preserve_evidence`.

    Args:
        content: Full markdown body to write after frontmatter.
        title: Unicode display title stored in frontmatter and the H1.
        slug: Optional lowercase ASCII kebab-case filename component.
        note_type: research-note, insight, failure, pattern, experiment, or production-log.
        project: Required for research-note. __PROJECT_KEYS_HINT__
        projects: Optional project keys for cross-project notes. __PROJECT_KEYS_HINT__
        sources: Source/evidence paths this conclusion draws from.
        tags: Lowercase tags.
        status: Optional status override.
        severity: Failure severity.
        pattern_type: Pattern subtype.
        domain: Experiment domain.
        started: Experiment start date.
        duration: Experiment duration.
        hypothesis: Experiment hypothesis.
        n: Experiment sample size.
        concluded: Experiment conclusion date.
        medium: Production-log medium.
        recorded: Production recording date.
        published: Production publication date.
        host: Production host/creator.
        editor: Production editor/producer.
        suggestions: Include link suggestions in the result.
        project_category: Category for a new project key.
        validate_only: Validate and return an immutable creation draft without writing.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        relation_disposition: Reviewed relation outcome for commit.
        relation_review_hash: Draft hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.
    """
    return op_note(
        vault_root,
        content=content,
        note_type=note_type,
        title=title,
        slug=slug,
        project=project,
        projects=projects,
        sources=sources,
        tags=tags,
        status=status,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        started=started,
        duration=duration,
        hypothesis=hypothesis,
        n=n,
        concluded=concluded,
        medium=medium,
        recorded=recorded,
        published=published,
        host=host,
        editor=editor,
        suggestions=suggestions,
        project_category=project_category,
        validate_only=validate_only,
        draft_id=draft_id,
        draft_hash=draft_hash,
        draft_token=draft_token,
        relation_disposition=relation_disposition,
        relation_review_hash=relation_review_hash,
        relation_review_reason=relation_review_reason,
    )


def op_edit_memory(
    vault_root: Path,
    path: str,
    why: str,
    operation: edit_operations_module.EditOperation = None,  # type: ignore[assignment]
    **legacy: Any,
) -> dict:
    """Edit an existing memory page with an auditable reason.

    Use for small corrections, section edits, batch string edits, opinion-row
    fills, or one frontmatter field. Substantial rewrites should use
    `replace_memory` so history stays explicit.

    Args:
        path: Page to edit.
        why: One-line rationale recorded in the log.
        operation: Required nested edit selected by `kind`. The seven supported
            kinds expose only fields their underlying edit leaf enforces.

    The previous flat keyword arguments remain accepted by direct Python/runtime
    callers for one compatibility release, but are deprecated and intentionally
    absent from public discovery schemas.
    """
    arguments: dict[str, Any] = {"path": path, "why": why, **legacy}
    if operation is not None:
        arguments["operation"] = operation
    normalized = edit_operations_module.normalize_edit_arguments(arguments)
    return op_edit(vault_root, **normalized)


def op_observe_memory(
    vault_root: Path,
    path: str,
    operation: str = "add",
    category: str | None = None,
    content: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    context: str | None = None,
    relations: list[dict] | None = None,
    unit_ref: str | None = None,
    expected_fingerprint: str | None = None,
    expected_hash: str | None = None,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Validate or mutate one semantic unit on a compiled memory page.

    Compact observation is the default form. Supply an explicit governed
    non-observation `kind` for rich semantic-block form and typed relations.
    Use `validate` before a guarded commit when semantic review is required.

    Args:
        path: Parent page path or canonical memory reference.
        operation: add, update, remove, or validate.
        category: Open semantic category for add/update/validate.
        content: Unit content for add/update/validate.
        kind: Optional governed rich kind; omitted means compact observation.
        tags: Optional compact suffix tags or rich metadata tags.
        context: Optional compact suffix context or rich metadata context.
        relations: Rich typed relations as {kind, target} objects.
        unit_ref: Current exact unit reference for update/remove or update validation.
        expected_fingerprint: Current exact unit fingerprint; required for update/remove.
        expected_hash: Current exact parent-page content hash; required for update/remove.
        transition_token: Exact transition token returned by validate.
        relation_disposition: Existing-page semantic review disposition.
        relation_review_hash: Transition hash covered by reviewed-none.
        relation_review_reason: Audit reason for reviewed-none.

    Returns:
        The normalized unit, stable unit reference, parent hashes, bounded
        semantic-contract feedback, and derived-index outcome.
    """
    raw_path = str(path or "").strip()
    if raw_path.startswith(("/", "\\")) or Path(raw_path).is_absolute() or (
        len(raw_path) >= 3
        and raw_path[0].isalpha()
        and raw_path[1] == ":"
        and raw_path[2] in {"/", "\\"}
    ):
        raise ValueError(
            "INVALID_PATH: observe_memory requires a governed KB-relative "
            "path or reference"
        )
    try:
        resolved_path = memory_refs_module.resolve_identifier_read_only(
            vault_root, path
        )
    except memory_refs_module.ReferenceError as error:
        raise ValueError(f"{error.code}: {error.reason}") from error
    if raw_path.lower().startswith(("exomem://vault/", "exomem://source/")) and not (
        resolved_path == kb_dirname()
        or resolved_path.startswith(f"{kb_dirname()}/")
    ):
        raise ValueError(
            "INVALID_PATH: observe_memory parent reference resolves outside "
            f"{kb_dirname()}/"
        )
    try:
        result = observe_memory_module.observe_memory(
            vault_root,
            path=resolved_path,
            operation=operation,  # type: ignore[arg-type]
            category=category,
            content=content,
            kind=kind,
            tags=tags,
            context=context,
            relations=relations,
            unit_ref=unit_ref,
            expected_fingerprint=expected_fingerprint,
            expected_hash=expected_hash,
            transition_token=transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
        if result.get("mutated"):
            query_log.log_write_call(
                tool="observe_memory",
                written_path=str(result.get("path") or "") or None,
                cited_sources=[],
            )
        return result
    except observe_memory_module.ObserveMemoryError as error:
        message = f"{error.code}: {error.reason}"
        if error.remediation:
            message += f" Remediation: {error.remediation}"
        raise ValueError(message) from error


def op_replace_memory(
    vault_root: Path,
    old_path: str,
    content: str,
    title: str,
    slug: str | None = None,
    note_type: str = "insight",
    reason: str | None = None,
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    project_category: str | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Supersede an existing compiled memory with a new version.

    The old page remains readable and points to the new page. Use this for
    meaningful changes in conclusion, not small edits.

    Args:
        old_path: Existing page to supersede.
        content: Full markdown body for the new page.
        title: New page title.
        slug: Optional lowercase ASCII kebab-case filename component.
        note_type: New page type.
        reason: Why the old page is being superseded.
        project: Required for research-note.
        projects: Optional project keys.
        sources: Source/evidence paths for the new conclusion.
        tags: Lowercase tags.
        status: Optional status override.
        severity: Failure severity.
        pattern_type: Pattern subtype.
        domain: Experiment domain.
        started: Experiment start date.
        duration: Experiment duration.
        hypothesis: Experiment hypothesis.
        n: Experiment sample size.
        concluded: Experiment conclusion date.
        medium: Production-log medium.
        recorded: Production recording date.
        published: Production publication date.
        host: Production host/creator.
        editor: Production editor/producer.
        project_category: Category for a new project key.
        validate_only: Validate the replacement draft without writing either page.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        relation_disposition: Reviewed relation outcome for commit.
        relation_review_hash: Draft hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.
    """
    return op_replace(
        vault_root,
        old_path=old_path,
        content=content,
        note_type=note_type,
        title=title,
        slug=slug,
        reason=reason,
        project=project,
        projects=projects,
        sources=sources,
        tags=tags,
        status=status,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        started=started,
        duration=duration,
        hypothesis=hypothesis,
        n=n,
        concluded=concluded,
        medium=medium,
        recorded=recorded,
        published=published,
        host=host,
        editor=editor,
        project_category=project_category,
        validate_only=validate_only,
        draft_id=draft_id,
        draft_hash=draft_hash,
        draft_token=draft_token,
        relation_disposition=relation_disposition,
        relation_review_hash=relation_review_hash,
        relation_review_reason=relation_review_reason,
    )


def op_capture_source(
    vault_root: Path,
    source_schema: object,
    content: str,
    title: str,
    slug: str | None = None,
    source_type: str = "other",
    url: str | None = None,
    tags: list[str] | None = None,
    why_captured: str | None = None,
    compile_guidance: bool = False,
    suggested_title: str | None = None,
) -> dict:
    """Capture raw source material and optionally return compile guidance.

    The raw source is preserved first. If `compile_guidance=true`, Exomem then
    returns a proposal for a future compiled note, without silently converting
    raw provenance into a conclusion.

    Args:
        content: Raw source text.
        title: Source title.
        slug: Optional lowercase ASCII kebab-case filename component.
        source_type: article, session, book, paper, video, or other.
        url: Required for article, paper, or video sources.
        tags: Lowercase tags.
        why_captured: Short reason this source matters.
        compile_guidance: Return a compilation proposal for the captured source.
        suggested_title: Optional title hint for the compilation proposal.
    """
    source = op_add(
        vault_root,
        source_schema,
        content=content,
        source_type=source_type,
        title=title,
        slug=slug,
        url=url,
        tags=tags,
        why_captured=why_captured,
    )
    out: dict = {"source": source}
    if compile_guidance:
        try:
            out["compile_guidance"] = op_propose_compilation(
                vault_root,
                sources=[source["path"]],
                suggested_title=suggested_title,
            )
        except ValueError as exc:
            out["compile_guidance"] = {"available": False, "error": str(exc)}
    return out


def op_compile_source(
    vault_root: Path,
    sources: list[str],
    suggested_title: str | None = None,
) -> dict:
    """Plan a compiled note from one or more raw sources.

    This is read-only: it returns a note skeleton, suggested source links, and
    adjacent compiled pages. The agent or user still writes the conclusion via
    `remember`.

    Args:
        sources: Source paths or wikilinks to compile from.
        suggested_title: Optional title override.
    """
    return op_propose_compilation(vault_root, sources=sources, suggested_title=suggested_title)


def op_preserve_evidence(
    vault_root: Path,
    scope: str,
    category: str,
    filename: str,
    content: str,
    description: str | None = None,
) -> dict:
    """Preserve text evidence as append-only proof material.

    Use for receipts, letters, transcripts, warranty records, legal/dispute
    material, and other factual artifacts. Binary files use `transfer_artifact`
    plus the `/upload` endpoint so bytes do not pass through the model.

    Args:
        scope: Incident, case, project, or domain key.
        category: Evidence category within the scope.
        filename: Artifact filename, including extension.
        content: UTF-8 text to preserve as received.
        description: Optional sidecar description.
    """
    return op_preserve(
        vault_root,
        scope=scope,
        category=category,
        filename=filename,
        content=content,
        description=description,
    )


def op_transfer_artifact(vault_root: Path, operation: str = "upload") -> dict:
    """Prepare out-of-band binary artifact transfer.

    Returns a short-lived token and URL for uploading evidence binaries or
    downloading a vault file into a sandbox. The long-lived server secret never
    leaves the server.

    Args:
        operation: upload or download.
    """
    _ = vault_root
    if operation not in ("upload", "download"):
        raise ValueError("INVALID_MODE: transfer_artifact operation must be 'upload' or 'download'")
    secret = os.environ.get("EXOMEM_UPLOAD_TOKEN", "").strip() or None
    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    large_base_url = os.environ.get("EXOMEM_LARGE_UPLOAD_BASE_URL", "").strip().rstrip("/") or None
    return upload_tokens.mint_for_endpoint(
        secret,
        base_url,
        scope=operation,
        large_base_url=large_base_url if operation == "upload" else None,
    )


def op_process_media(
    vault_root: Path,
    path: str | None = None,
    operation: Literal["process", "status", "retry"] = "process",
) -> dict:
    """Process, inspect, or retry governed media without waiting for extraction.

    Supported media copied into the governed Knowledge Base or uploaded through
    Exomem is processed automatically. Use this action to reconcile one artifact
    immediately, inspect bounded durable status, or retry actionable blocked/failed
    work after remediation. Existing valid transcripts are preserved.

    Args:
        path: Optional governed Knowledge Base media path. Omit for bounded all-media work.
        operation: process, status, or retry.
    """
    from . import index_sync, media_jobs
    from .cli_ops import OpError
    from .writer_lease import active_manager, active_mutation_request_id

    if operation not in {"process", "status", "retry"}:
        raise OpError(
            "INVALID_MEDIA_OPERATION",
            "process_media operation must be process, status, or retry",
        )

    vault_root = Path(vault_root).resolve()
    manager = active_manager()

    def _commit_guard():
        return manager.mutation_guard(
            vault_root,
            request_id=active_mutation_request_id(),
            operation=f"process_media_{operation}_commit",
            holder_kind="command",
        )

    def _drain_index_refresh(paths: list[Path] | list[str] | None = None) -> tuple[int, int]:
        current = index_sync.deferred_work_status(vault_root)["full_upserts"]
        selected = current["paths"] if paths is None else paths
        refreshed = index_sync.drain_deferred_work(
            vault_root,
            limit=media_jobs.STATUS_JOB_LIMIT,
            paths=selected,
        )
        remaining = index_sync.deferred_work_status(vault_root)["full_upserts"][
            "count"
        ]
        return refreshed, remaining

    if operation == "status":
        return {
            "operation": operation,
            **media_jobs.status(vault_root),
            "index_refresh": index_sync.deferred_work_status(vault_root)["full_upserts"],
        }

    if path is None:
        if operation == "retry":
            from . import media_processing

            requeued = media_processing.retry_all_media(
                vault_root,
                limit=media_jobs.STATUS_JOB_LIMIT,
                commit_guard=_commit_guard,
                propagate_transient_errors=True,
            )
            refreshed, remaining = _drain_index_refresh()
            return {
                "operation": operation,
                "requeued": requeued,
                "index_refreshed": refreshed,
                "index_refresh_remaining": remaining,
            }
        from . import media_processing

        if operation == "process":
            reconciled = media_processing.reconcile_all_media(
                vault_root,
                limit=media_processing.DEFAULT_RECONCILE_LIMIT,
                reconcile_one=lambda binary: media_processing.reconcile_media(
                    vault_root,
                    binary,
                    explicit=False,
                    commit_guard=_commit_guard,
                ),
                propagate_transient_errors=True,
            )
            refreshed, remaining = _drain_index_refresh()
            return {
                "operation": operation,
                "reconciled": reconciled,
                "index_refreshed": refreshed,
                "index_refresh_remaining": remaining,
            }

    from . import media_processing

    binary = Path(path)
    if not binary.is_absolute():
        binary = vault_root / binary
    binary = Path(os.path.abspath(binary))
    try:
        binary.relative_to(vault_root / kb_dirname())
    except ValueError as exc:
        raise OpError(
            "MEDIA_PATH_OUTSIDE_KB",
            f"media path must be inside {kb_dirname()}: {path}",
        ) from exc
    if media_processing.classify_media(binary) is None:
        raise OpError("UNSUPPORTED_MEDIA", f"unsupported media type for {binary.name!r}")
    if not binary.exists():
        raise OpError("MEDIA_NOT_FOUND", f"media artifact does not exist: {path}")

    try:
        if operation == "process":
            result = media_processing.reconcile_media(
                vault_root,
                binary,
                explicit=True,
                commit_guard=_commit_guard,
            )
        else:
            result = media_processing.retry_media(
                vault_root,
                binary,
                commit_guard=_commit_guard,
            )
    except media_processing.MediaProcessingError as exc:
        raise OpError(exc.code, exc.reason) from exc

    if result is None:  # explicit supported processing cannot be silently ignored
        raise OpError("UNSUPPORTED_MEDIA", f"unsupported media type for {binary.name!r}")
    payload = {
        "operation": operation,
        "path": binary.relative_to(vault_root).as_posix(),
        "media_type": result.media_type,
        "state": result.state,
        "sidecar_path": result.sidecar_path.relative_to(vault_root).as_posix(),
        "job_id": result.job_id,
    }
    if operation == "retry":
        payload["requeued"] = result.requeued
    refreshed, remaining = _drain_index_refresh([result.sidecar_path])
    payload["index_refreshed"] = refreshed
    payload["index_refresh_remaining"] = remaining
    return payload


def op_read_media(
    vault_root: Path,
    path: str,
    max_frames: int = 8,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> ToolResult:
    """Read sampled video frames inline for visual inspection.

    This is MCP-only because it returns image content blocks. Heavy media
    extraction remains explicit and dependency-gated.

    Args:
        path: Vault-relative video path.
        max_frames: Maximum frames to return.
        start_sec: Optional start timestamp in seconds.
        end_sec: Optional end timestamp in seconds.
    """
    return op_get_video_frames(
        vault_root,
        path=path,
        max_frames=max_frames,
        start_sec=start_sec,
        end_sec=end_sec,
    )


def op_review_memory(
    vault_root: Path,
    mode: str = "attention",
    categories: list[str] | None = None,
    limit: int = 25,
    query: str = "",
    sources: list[str] | None = None,
    suggested_title: str | None = None,
    tag: str | None = None,
    key: str | None = None,
    value: str | None = None,
    path: str | None = None,
    state: str = "open",
    ref: str | None = None,
    detail: Literal["actionable", "full"] = "actionable",
    legacy_sample_limit: _AuditSampleLimit = audit_module.DEFAULT_LEGACY_SAMPLE_LIMIT,
) -> dict:
    """Review memory health, provenance, drift, or source backlog.

    Default mode is read-only attention review. Write-capable repairs are in
    `maintain_memory`, not here.

    Args:
        mode: attention, activation, item, audit, provenance, evolution,
            compilation, stale, contradiction, unprocessed-sources, relation-debt,
            relation-queue, or adoption. `relation-queue` returns the read-only,
            batched relation-acceptance queue (deterministic suggestion candidates
            grouped by source page, with signal fingerprints and coverage
            counters); accept a candidate via
            `connect_memory(operation="accept-relation")` or reject via
            `triage_memory`. `adoption` returns the read-only Adoption Studio
            proposal queue grouped per run (structured agent proposals with signal
            fingerprints); approve a proposal via
            `adoption_studio(action="apply-proposal")` or dismiss via
            `triage_memory`.
        categories: Optional category filter for attention/activation/audit.
        limit: Attention/activation/evolution result cap.
        query: Topic for evolution review.
        sources: Source paths for compilation mode.
        suggested_title: Optional compilation title hint.
        tag: Provenance tag shorthand.
        key: Provenance key filter.
        value: Provenance value filter.
        path: Restrict provenance scan to one path.
        state: For attention/activation, open (default), all, snoozed, or dismissed.
        ref: Stable `exomem://review/<id>` reference for item mode.
        detail: Audit output detail: actionable (default) or full.
        legacy_sample_limit: Audit legacy-backlog sample count, from 0 to 50.
    """
    if path:
        path = _resolve_memory_identifier(vault_root, path)
    if mode == "attention":
        return op_attention(vault_root, categories=categories, limit=limit, state=state)
    if mode == "activation":
        return attention_module.activation(
            vault_root,
            categories=categories,
            limit=limit,
            state=state,
        ).as_dict()
    if mode == "item":
        if not ref:
            raise ValueError("INVALID_REVIEW: item mode requires `ref`")
        return attention_module.item_by_ref(vault_root, ref).as_dict()
    if mode == "audit":
        return op_audit(
            vault_root,
            categories=categories,
            detail=detail,
            legacy_sample_limit=legacy_sample_limit,
        )
    if mode == "stale":
        return op_attention(
            vault_root, categories=["stale_review"], limit=limit, state=state
        )
    if mode == "contradiction":
        return op_attention(
            vault_root,
            categories=["corpus_contradictions"],
            limit=limit,
            state=state,
        )
    if mode == "unprocessed-sources":
        return op_attention(
            vault_root,
            categories=["unprocessed_source"],
            limit=limit,
            state=state,
        )
    if mode == "relation-debt":
        return op_attention(
            vault_root, categories=["relation_debt"], limit=limit, state=state
        )
    if mode == "relation-queue":
        return relation_queue_module.build_queue(vault_root, limit_pages=limit)
    if mode == "adoption":
        adoption_run_id: str | None = None
        if ref and ref.startswith("exomem://adoption/run/"):
            adoption_run_id = ref.rsplit("/", 1)[-1] or None
        return adoption_proposals_module.build_queue(
            vault_root, run_id=adoption_run_id, state=state, limit=limit
        )
    if mode == "provenance":
        return op_provenance_report(vault_root, tag=tag, key=key, value=value, path=path)
    if mode == "evolution":
        return op_evolution(vault_root, query=query, limit=limit)
    if mode == "compilation":
        if not sources:
            raise ValueError("INVALID_REVIEW: compilation mode requires `sources`")
        return op_propose_compilation(vault_root, sources=sources, suggested_title=suggested_title)
    raise ValueError(
        "INVALID_MODE: review_memory mode must be attention, activation, item, audit, "
        "provenance, evolution, compilation, stale, contradiction, "
        "unprocessed-sources, relation-debt, relation-queue, or adoption"
    )


def op_review_item_context(
    vault_root: Path,
    ref: str,
    expected_fingerprint: str | None = None,
    max_body_chars: int = 4000,
    max_related_pages: int = 8,
    max_graph_nodes: int = 30,
    max_graph_edges: int = 60,
    max_history: int = 10,
    max_evolution_versions: int = 10,
) -> dict:
    """Inspect one stable review item with bounded recorded context.

    Resolves an Inbox or corpus-activation item by `exomem://review/<id>` and
    composes its target, related summaries, provenance/evidence, graph, history,
    and path-specific supersession evolution. This is deterministic read-only
    assembly: it runs no model, makes no epistemic judgment, and never writes.

    Args:
        ref: Stable `exomem://review/<id>` reference. An
            `exomem://review/adoption/<id>` ref returns the bounded Adoption
            Studio proposal context (proposal record, live binding check, and
            target-page summary) instead.
        expected_fingerprint: Optional reviewed fingerprint; a mismatch asks the
            caller to refresh instead of presenting stale context.
        max_body_chars: Maximum target body characters.
        max_related_pages: Maximum related-page summaries.
        max_graph_nodes: Maximum graph nodes.
        max_graph_edges: Maximum graph edges.
        max_history: Maximum recorded history entries.
        max_evolution_versions: Maximum recorded supersession versions.
    """
    if adoption_proposals_module.is_adoption_ref(ref):
        return adoption_proposals_module.assemble_context(
            vault_root,
            ref=ref,
            expected_fingerprint=expected_fingerprint,
            max_body_chars=max_body_chars,
            max_related_pages=max_related_pages,
        )
    return review_context_module.assemble(
        vault_root,
        ref=ref,
        expected_fingerprint=expected_fingerprint,
        max_body_chars=max_body_chars,
        max_related_pages=max_related_pages,
        max_graph_nodes=max_graph_nodes,
        max_graph_edges=max_graph_edges,
        max_history=max_history,
        max_evolution_versions=max_evolution_versions,
    )


def op_triage_memory(
    vault_root: Path,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict:
    """Triage one Epistemic Inbox item explicitly.

    This is the write-capable companion to read-only `review_memory`. Decisions
    bind to the current signal fingerprint, so materially changed knowledge
    resurfaces automatically.

    Args:
        ref: Stable `exomem://review/<id>` reference from review_memory. An
            `exomem://review/adoption/<id>` ref triages an Adoption Studio
            proposal instead, keyed the same way (`review_id:fingerprint`).
        action: dismiss, snooze, or reopen.
        until: Snooze-through date as YYYY-MM-DD; required only for snooze.
        why: Optional short rationale stored with the review decision.
        expected_fingerprint: Optional reviewed fingerprint; a mismatch refuses
            the write and asks the caller to refresh.
    """
    if adoption_proposals_module.is_adoption_ref(ref):
        return adoption_proposals_module.triage(
            vault_root,
            ref=ref,
            action=action,
            until=until,
            why=why,
            expected_fingerprint=expected_fingerprint,
        )
    if relation_queue_module.is_relation_ref(ref):
        return relation_queue_module.triage(
            vault_root,
            ref=ref,
            action=action,
            until=until,
            why=why,
            expected_fingerprint=expected_fingerprint,
        )
    item = attention_module.item_by_ref(
        vault_root, ref, expected_fingerprint=expected_fingerprint
    )
    if expected_fingerprint and item.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the review signal changed; refresh the worklist "
            f"and inspect {item.ref} again"
        )
    result = review_state_module.ReviewStateStore(vault_root).apply(
        item.item_id or review_state_module.parse_review_ref(ref),
        item.fingerprint or "",
        action=action,
        until=until,
        why=why,
    )
    result.update(
        {
            "path": item.path,
            "target_ref": item.target_ref,
            "categories": item.categories,
        }
    )
    return result


def op_connect_memory(
    vault_root: Path,
    operation: str = _CONNECT_MEMORY_DEFAULT_OPERATION,
    path: str | None = None,
    target: str | None = None,
    query: str | None = None,
    unit_ref: str | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    draft_title: str | None = None,
    draft_body: str | None = None,
    limit: int = 8,
    scope: str = "kb",
    include_model_suggestions: bool = False,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
    max_body_chars: int = 3000,
    entity_type: EntityTypeId | None = None,
    name: str | None = None,
    slug: str | None = None,
    summary: str | None = None,
    why_in_kb: str | None = None,
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    affiliation: str | None = None,
    relationship: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
    ref: str | None = None,
    expected_hash: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict | list[dict]:
    """Connect memory through links, typed graph context, or entities.

    Proposal modes are read-only. `operation="create-entity"` is an explicit
    additive write that creates a typed graph node through the canonical entity
    writer. `operation="accept-relation"` is a governed additive write that
    authors one reviewed relation-queue candidate.

    Args:
        operation: context, suggest-links, suggest-relations, graph-context,
            inbound-links, resolve-entity, create-entity, or accept-relation.
        path: Existing page path for link, graph, or relation context.
        target: Target path for inbound-links; defaults to path.
        query: Query seed for graph-context.
        unit_ref: Exact current semantic-unit seed for graph-context.
        categories: Registry-resolved semantic-unit category allowlist.
        kinds: Governed semantic-unit kind allowlist.
        draft_title: Draft title for suggestion modes.
        draft_body: Draft body for suggestion modes.
        limit: Candidate cap for suggestion modes.
        scope: Search scope for link suggestions.
        include_model_suggestions: Request optional model-backed relation suggestions.
        depth: Graph traversal depth.
        relation_types: Graph relation-type allowlist.
        node_types: Graph node-type allowlist.
        max_nodes: Graph node cap.
        max_edges: Graph edge cap.
        traversal_profile: Deterministic graph lens; omission preserves `all`.
        max_body_chars: Per-document stored-body cap for context.
        entity_type: Entity type for create-entity.
        name: Entity name for create-entity.
        slug: Optional lowercase ASCII kebab-case entity filename component.
        summary: Entity summary for create-entity.
        why_in_kb: Optional entity relevance paragraph.
        tags: Entity tags.
        connections: Entity connection paths.
        affiliation: Person affiliation.
        relationship: Person relationship.
        domain: Concept domain.
        language: Library language.
        repo: Library repository.
        license: Library license.
        used_in: Library usage project keys.
        decided: Decision date.
        project: Decision project key.
        decision_status: Decision status.
        ref: Relation-queue item ref for accept-relation.
        expected_hash: Target page `content_hash` drift guard for accept-relation.
            Required for accept-relation.
        why: Audit reason recorded with the accept-relation edit.
        expected_fingerprint: Reviewed candidate fingerprint for accept-relation.
            Required for accept-relation (not optional — a mismatch, or an
            omitted value, refuses the write); accept re-validates live
            eligibility too, so a candidate that stopped being open between
            the queue read and this call also refuses.
    """
    if operation == "accept-relation":
        if not ref:
            raise ValueError("INVALID_MODE: accept-relation requires `ref`")

        def _accept_relations_edit(vault_root: Path, **kw: Any) -> dict:
            """`edit_memory` for the relation queue: identical to op_edit_memory,
            but creates the canonical `## Relations` section when a note has none
            (remember() doesn't emit one), so accepting the first relation into a
            note doesn't fail HEADING_NOT_FOUND. create_missing stays server-side
            only — it is not exposed on the edit_memory MCP tool."""
            try:
                result = edit_module.edit(
                    vault_root, create_missing_section=True, **kw
                )
            except edit_module.EditError as e:
                msg = f"{e.code}: {e.reason}"
                if getattr(e, "missing", None):
                    msg += f" (missing: {e.missing})"
                if getattr(e, "candidates", None):
                    msg += f" (candidates: {e.candidates})"
                raise ValueError(msg) from e
            return result.as_dict()

        return relation_queue_module.accept(
            vault_root,
            ref=ref,
            expected_hash=expected_hash,
            why=why,
            expected_fingerprint=expected_fingerprint,
            edit_memory=_accept_relations_edit,
        )
    if path:
        path = _resolve_memory_identifier(vault_root, path)
    if target:
        target = _resolve_memory_identifier(vault_root, target)
    if operation == "suggest-links":
        return op_suggest_links(
            vault_root,
            path=path,
            draft_title=draft_title,
            draft_body=draft_body,
            limit=limit,
            scope=scope,
        )
    if operation == "suggest-relations":
        return op_suggest_relations(
            vault_root,
            path=path,
            draft_title=draft_title,
            draft_body=draft_body,
            include_model_suggestions=include_model_suggestions,
            limit=limit,
        )
    if operation in ("context", "graph-context"):
        return memory_context_module.assemble_context(
            vault_root,
            path=path,
            query=query,
            unit_ref=unit_ref,
            categories=categories,
            kinds=kinds,
            depth=depth,
            relation_types=relation_types,
            node_types=node_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
            traversal_profile=traversal_profile,
            limit=limit,
            max_body_chars=max_body_chars,
        )
    if operation == "inbound-links":
        target_path = target or path
        if not target_path:
            raise ValueError("INVALID_TARGET: inbound-links requires `target` or `path`")
        return op_list_inbound_links(vault_root, target=target_path)
    if operation == "resolve-entity":
        if not name:
            raise ValueError("INVALID_TARGET: resolve-entity requires `name`")
        return entity_candidates_module.resolve_entity_candidate(
            vault_root, name=name, entity_type=entity_type, limit=limit
        )
    if operation == "create-entity":
        missing = [
            field
            for field, value in (("entity_type", entity_type), ("name", name), ("summary", summary))
            if not value
        ]
        if missing:
            raise ValueError("INVALID_LINK: create-entity requires " + ", ".join(missing))
        return op_link(
            vault_root,
            entity_type=entity_type or "",
            name=name or "",
            slug=slug,
            summary=summary or "",
            why_in_kb=why_in_kb,
            tags=tags,
            connections=connections,
            affiliation=affiliation,
            relationship=relationship,
            domain=domain,
            language=language,
            repo=repo,
            license=license,
            used_in=used_in,
            decided=decided,
            project=project,
            decision_status=decision_status,
        )
    raise ValueError(
        "INVALID_MODE: connect_memory operation must be context, suggest-links, "
        "suggest-relations, graph-context, inbound-links, resolve-entity, create-entity, or "
        "accept-relation"
    )


def op_adopt_vault(
    vault_root: Path,
    path: str = "",
    mode: str = _ADOPT_VAULT_DEFAULT_MODE,
    max_depth: int = overview_module.DEFAULT_MAX_DEPTH,
    include_hidden: bool = False,
    samples: int = 5,
    pack_limit: int = 6,
    manifest_path: str | None = None,
    selected_paths: list[str] | None = None,
    semantic_max_files: int = semantic_census.DEFAULT_MAX_FILES,
    semantic_max_bytes: int = semantic_census.DEFAULT_MAX_BYTES,
    semantic_example_limit: int = semantic_census.DEFAULT_EXAMPLE_LIMIT,
) -> dict:
    """Adopt an existing vault safely without replacing originals.

    Default mode scans only. Copy/compile modes write under the governed
    Knowledge Base layer and preserve original path/hash provenance.

    Args:
        path: Vault subtree to scan.
        mode: scan-only, save-manifest, copy-as-sources, or compile-selected.
        max_depth: Folder tree depth cap.
        include_hidden: Include hidden files/directories.
        samples: Filename sample count per folder.
        pack_limit: Max suggested knowledge packs.
        manifest_path: Optional manifest destination.
        selected_paths: Explicit legacy files for copy/compile modes.
        semantic_max_files: Maximum Markdown files read by the semantic census.
        semantic_max_bytes: Maximum total Markdown bytes read by the semantic census.
        semantic_example_limit: Maximum bounded semantic examples per grouping.
    """
    return op_adopt(
        vault_root,
        path=path,
        mode=mode,
        max_depth=max_depth,
        include_hidden=include_hidden,
        samples=samples,
        pack_limit=pack_limit,
        manifest_path=manifest_path,
        selected_paths=selected_paths,
        semantic_max_files=semantic_max_files,
        semantic_max_bytes=semantic_max_bytes,
        semantic_example_limit=semantic_example_limit,
    )


_ADOPTION_STUDIO_ACTIONS = (
    "start",
    "status",
    "select",
    "plan",
    "apply",
    "cancel",
    "finish",
    "work-item",
    "propose",
    "apply-proposal",
)


def _load_adoption_proposals():
    """Lazily import the Lane B proposal engine, or fail with a clear message."""
    try:
        from . import adoption_proposals as adoption_proposals_module
    except ImportError as exc:
        raise ValueError(
            "NOT_IMPLEMENTED: adoption_studio actions 'work-item', 'propose', and "
            "'apply-proposal' require the adoption_proposals module, which is not "
            "installed in this build"
        ) from exc
    return adoption_proposals_module


def op_adoption_studio(
    vault_root: Path,
    action: str,
    run_id: str | None = None,
    path: str = "",
    include_hidden: bool = False,
    initialize_kb: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    overrides: list[str] | None = None,
    include_junk: bool = False,
    plan_id: str | None = None,
    retry_failed: bool = False,
    only_paths: list[str] | None = None,
    why: str | None = None,
    write_manifest: bool = True,
    sources: list[str] | None = None,
    max_sources: int = 5,
    max_chars_per_source: int = 2000,
    proposals: list[dict] | None = None,
    ref: str | None = None,
    expected_fingerprint: str | None = None,
    expected_hash: str | None = None,
) -> dict:
    """Run a governed, resumable Adoption Studio session over existing material.

    Adoption Studio turns a messy legacy vault into governed Exomem knowledge
    without ever rewriting, moving, or deleting an original file. It is a durable,
    canonical-file-backed run with a preview-exact-actions contract: you see the
    precise imports before anything is written, and `apply` commits exactly that
    plan or refuses. One required `action` multiplexes the whole lifecycle; the
    read-only default (`status`) is safe and `start` is explicitly guarded.

    Lifecycle actions: `start` scans a subtree read-only and snapshots a candidate
    inventory; `select` materializes a folder-rule selection server-side; `plan`
    previews exact targets, titles, hashes, and frontmatter; `apply` copies the
    validated subset into governed Sources with provenance in one atomic batch;
    `cancel` closes a pre-apply run; `finish` proves recall and hands you a first
    question. Agent actions ride the run afterwards: `work-item` returns bounded
    read-only context, `propose` submits structured proposals, and
    `apply-proposal` approves one through an existing governed leaf.

    Args:
        action: Required. One of start, status, select, plan, apply, cancel,
            finish, work-item, propose, or apply-proposal.
        run_id: Stable adoption run id from `start`; required by every action
            except `start` and the run-listing form of `status`.
        path: For `start`, the vault subtree to scan. Defaults to the vault root.
        include_hidden: For `start`, include hidden files/directories in the scan.
        initialize_kb: For `start`, bootstrap the Knowledge Base scaffold first
            when it does not exist yet (otherwise `start` refuses with
            KB_NOT_INITIALIZED).
        include: For `select`, folder or file paths whose eligible files are
            selected (server materializes the concrete set).
        exclude: For `select`, folder or file paths to remove from the selection.
        overrides: For `select`, explicit per-file paths to force-select.
        include_junk: For `select`, include junk (e.g. zero-byte) files that are
            otherwise demoted. Default false.
        plan_id: For `apply`, the plan id echoed from `plan`/`status`; a mismatch
            or a changed selection is refused with PLAN_STALE.
        retry_failed: For `apply`, re-plan and re-apply only the failed subset.
        only_paths: For `apply`, restrict the (retry) apply to these originals.
        why: Required approver rationale for `apply-proposal`; also records the
            reason on `cancel`.
        write_manifest: For `finish`, write an optional run manifest under
            `Knowledge Base/_Adoption/`. Default true.
        sources: For `work-item`, explicit source paths to include instead of the
            first `max_sources` applied imports.
        max_sources: For `work-item`, the maximum sources returned. Default 5.
        max_chars_per_source: For `work-item`, the per-source excerpt cap. Default 2000.
        proposals: For `propose`, the list of structured proposal objects to submit.
        ref: For `apply-proposal`, the `exomem://review/adoption/<id>` proposal ref.
        expected_fingerprint: For `apply-proposal`, the reviewed fingerprint that
            must still match, or the write is refused.
        expected_hash: For `apply-proposal`, the target page hash for relation and
            reconciliation-relate approvals.
    """
    action = (action or "").strip()
    if action not in _ADOPTION_STUDIO_ACTIONS:
        raise ValueError(
            "INVALID_MODE: adoption_studio action must be start, status, select, "
            "plan, apply, cancel, finish, work-item, propose, or apply-proposal"
        )
    try:
        if action == "start":
            return adoption_run_module.start(
                vault_root, path=path, include_hidden=include_hidden, initialize_kb=initialize_kb
            )
        if action == "status":
            return adoption_run_module.status(vault_root, run_id=run_id)
        if action == "select":
            return adoption_run_module.select(
                vault_root,
                run_id=run_id,
                include=include,
                exclude=exclude,
                overrides=overrides,
                include_junk=include_junk,
            )
        if action == "plan":
            return adoption_run_module.plan(vault_root, run_id=run_id)
        if action == "apply":
            return adoption_run_module.apply(
                vault_root,
                run_id=run_id,
                plan_id=plan_id,
                retry_failed=retry_failed,
                only_paths=only_paths,
            )
        if action == "cancel":
            return adoption_run_module.cancel(vault_root, run_id=run_id, why=why)
        if action == "finish":
            return adoption_run_module.finish(
                vault_root, run_id=run_id, write_manifest=write_manifest
            )
        proposals_module = _load_adoption_proposals()
        if action == "work-item":
            return proposals_module.work_item(
                vault_root,
                run_id=run_id,
                sources=sources,
                max_sources=max_sources,
                max_chars_per_source=max_chars_per_source,
            )
        if action == "propose":
            return proposals_module.propose(vault_root, run_id=run_id, proposals=proposals or [])
        # apply-proposal
        return proposals_module.apply_proposal(
            vault_root,
            ref=ref,
            expected_fingerprint=expected_fingerprint,
            why=why,
            expected_hash=expected_hash,
        )
    except adoption_run_module.AdoptionRunError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc
    except Exception as exc:  # structured proposal errors carry code/reason
        code = getattr(exc, "code", None)
        reason = getattr(exc, "reason", None)
        if code is not None and reason is not None:
            raise ValueError(f"{code}: {reason}") from exc
        raise


def op_maintain_memory(
    vault_root: Path,
    mode: str = "audit",
    categories: list[str] | None = None,
    dry_run: bool | None = None,
    rebuild_embeddings: bool = False,
    detail: Literal["actionable", "full"] = "actionable",
    legacy_sample_limit: _AuditSampleLimit = audit_module.DEFAULT_LEGACY_SAMPLE_LIMIT,
) -> dict:
    """Maintain vault health with explicit write-capable modes.

    Default mode is read-only audit. `mode="fix"` and `mode="backfill-ids"`
    rewrite content (wikilinks, frontmatter, stable IDs) and default to
    dry-run here as a safety net. `mode="reconcile"` only heals index-count
    and sidecar drift from out-of-band edits — the same canonical default as
    `op_reconcile` itself (idempotent, non-destructive) — so it defaults to
    writing; pass `dry_run=true` to preview instead.

    Args:
        mode: audit, fix, reconcile, or backfill-ids.
        categories: Optional audit category filter.
        dry_run: Report without writing when true. Defaults to true for
            fix/backfill-ids (safety net) and false for reconcile (matches
            `op_reconcile`'s own default). Pass explicitly to override either way.
        rebuild_embeddings: For fix mode, rebuild embeddings when explicitly requested.
        detail: Audit output detail: actionable (default) or full.
        legacy_sample_limit: Audit legacy-backlog sample count, from 0 to 50.
    """
    if mode == "audit":
        return op_audit(
            vault_root,
            categories=categories,
            detail=detail,
            legacy_sample_limit=legacy_sample_limit,
        )
    if mode == "fix":
        return op_audit_fix(
            vault_root,
            dry_run=True if dry_run is None else dry_run,
            rebuild_embeddings=rebuild_embeddings,
        )
    if mode == "reconcile":
        return op_reconcile(vault_root, dry_run=False if dry_run is None else dry_run)
    if mode == "backfill-ids":
        return memory_refs_module.backfill_ids(
            vault_root, dry_run=True if dry_run is None else dry_run
        )
    raise ValueError(
        "INVALID_MODE: maintain_memory mode must be audit, fix, reconcile, or backfill-ids"
    )


def op_schema_memory(
    vault_root: Path,
    operation: str,
    name: str | None = None,
    subject: str = "contract",
    project: str | None = None,
    page_type: str | None = None,
    save: bool = False,
    expected_hash: str | None = None,
    strict: bool = False,
    compare_to: str | None = None,
    proposal: dict | None = None,
    include_model_suggestions: bool = False,
) -> dict:
    """Infer, validate, diff, or save governed memory schemas.

    Contracts describe recurring frontmatter fields, semantic blocks, and typed
    relations without changing ordinary write validation. Inference is read-only
    unless `save=true`; an existing contract can only be overwritten with its
    current content hash.

    Args:
        operation: infer, validate, or diff.
        name: Lowercase contract slug; required only for `subject="contract"`.
        subject: `contract`, `categories`, `relations`, or `traversal-profiles`.
        project: Optional project scope for inference.
        page_type: Optional page-type scope for inference.
        save: Persist an inferred proposal. Default false.
        expected_hash: Required current hash when overwriting a saved contract.
        strict: In validate mode, signal a failing CLI/CI outcome on findings.
        compare_to: In diff mode, compare to this saved contract instead of corpus reality.
        proposal: Reviewed semantic-language, relation, or traversal-profile proposal.
        include_model_suggestions: Request response-only optional relation suggestions.

    Returns:
        A structured profile/proposal, validation report, or contract diff.
    """
    operation = operation.strip().lower()
    subject = subject.strip().lower()
    if subject == "categories":
        if operation == "infer":
            result = memory_schema_module.infer_category_registry(
                vault_root,
                project=project,
                page_type=page_type,
            )
            if save:
                if proposal is None or not isinstance(proposal, dict) or not {
                    "categories",
                    "kinds",
                } <= set(proposal):
                    raise ValueError(
                        "INCOMPLETE_SEMANTIC_LANGUAGE_PROPOSAL: "
                        "save requires one reviewed categories-and-kinds document"
                    )
                current = semantic_language_registry_module.load_registry(vault_root)
                candidate = semantic_language_registry_module.load_registry(
                    proposal=proposal
                )
                registry_file_exists = semantic_language_registry_module.registry_path(
                    vault_root
                ).exists()
                if (
                    registry_file_exists
                    and not candidate.findings
                    and semantic_language_registry_module.registry_proposal(current)["kinds"]
                    != semantic_language_registry_module.registry_proposal(candidate)["kinds"]
                ):
                    raise ValueError(
                        "CATEGORY_SAVE_KIND_CHANGE: category governance must preserve "
                        "the reviewed custom-kind namespace"
                    )
                result["saved"] = semantic_language_registry_module.save_registry(
                    vault_root,
                    proposal,
                    expected_hash=expected_hash,
                )
            return result
        if save:
            raise ValueError(
                "INVALID_SCHEMA_OPERATION: save is supported only for infer"
            )
        if operation == "validate":
            return memory_schema_module.validate_category_registry(
                vault_root,
                proposal=proposal,
                project=project,
                page_type=page_type,
                strict=strict,
            )
        if operation == "diff":
            before = semantic_language_registry_module.load_registry(vault_root)
            if proposal is not None:
                after = semantic_language_registry_module.load_registry(proposal=proposal)
                comparison = "proposal"
            else:
                inferred = memory_schema_module.infer_category_registry(
                    vault_root,
                    project=project,
                    page_type=page_type,
                )
                after = semantic_language_registry_module.load_registry(
                    proposal=inferred["proposal"]
                )
                comparison = "corpus"
            result = memory_schema_module.diff_category_registries(before, after)
            result.update(
                {
                    "content_hash": before.content_hash,
                    "comparison": comparison,
                    "registry_findings": [
                        item.as_dict() for item in after.findings
                    ],
                }
            )
            return result
        raise ValueError(
            "INVALID_SCHEMA_OPERATION: operation must be infer, validate, or diff"
        )
    if subject == "relations":
        if operation == "infer":
            result = memory_schema_module.infer_relation_registry(
                vault_root,
                project=project,
                page_type=page_type,
                include_model_suggestions=include_model_suggestions,
            )
            if save:
                if proposal is None:
                    raise ValueError("INCOMPLETE_RELATION_PROPOSAL: save requires a reviewed proposal")
                observed = {
                    item["raw_relation"]
                    for item in memory_schema_module.relation_observations(vault_root)
                }
                result["saved"] = relation_registry_module.save_registry(
                    vault_root,
                    proposal,
                    expected_hash=expected_hash,
                    observed_keys=observed,
                )
            return result
        if save:
            raise ValueError("INVALID_SCHEMA_OPERATION: save is supported only for infer")
        if operation == "validate":
            return memory_schema_module.validate_relation_registry(
                vault_root,
                proposal=proposal,
                project=project,
                page_type=page_type,
                strict=strict,
            )
        if operation == "diff":
            before = relation_registry_module.load_registry(vault_root)
            if proposal is not None:
                after = relation_registry_module.load_registry(vault_root, proposal=proposal)
                comparison = "proposal"
            else:
                inferred = memory_schema_module.infer_relation_registry(
                    vault_root, project=project, page_type=page_type
                )
                after = relation_registry_module.load_registry(
                    vault_root, proposal=inferred["proposal"]
                )
                comparison = "corpus"
            result = memory_schema_module.diff_relation_registries(before, after)
            result.update({"content_hash": before.extension_hash, "comparison": comparison})
            return result
        raise ValueError("INVALID_SCHEMA_OPERATION: operation must be infer, validate, or diff")
    if subject == "traversal-profiles":
        if operation == "infer":
            result = memory_schema_module.infer_traversal_profiles(vault_root)
            if save:
                if proposal is None:
                    raise ValueError("INCOMPLETE_PROFILE_PROPOSAL: save requires a reviewed proposal")
                result["saved"] = traversal_profiles_module.save_profiles(
                    vault_root, proposal, expected_hash=expected_hash
                )
            return result
        if save:
            raise ValueError("INVALID_SCHEMA_OPERATION: save is supported only for infer")
        current = traversal_profiles_module.load_profiles(vault_root)
        candidate = (
            traversal_profiles_module.load_profiles(vault_root, proposal=proposal)
            if proposal is not None
            else current
        )
        if operation == "validate":
            findings = list(candidate.findings)
            return {
                "subject": subject,
                "valid": not findings,
                "strict": strict,
                "strict_failed": bool(strict and findings),
                "content_hash": current.content_hash,
                "findings": findings,
            }
        if operation == "diff":
            before = {key: value.as_dict() for key, value in current.profiles.items()}
            after = {key: value.as_dict() for key, value in candidate.profiles.items()}
            return {
                "subject": subject,
                "changed": before != after,
                "content_hash": current.content_hash,
                "changes": {
                    "added": sorted(set(after) - set(before)),
                    "removed": sorted(set(before) - set(after)),
                    "modified": sorted(key for key in set(before) & set(after) if before[key] != after[key]),
                },
            }
        raise ValueError("INVALID_SCHEMA_OPERATION: operation must be infer, validate, or diff")
    if subject != "contract":
        raise ValueError(
            "INVALID_SCHEMA_SUBJECT: subject must be contract, categories, relations, "
            "or traversal-profiles"
        )
    if not name:
        raise ValueError("INVALID_CONTRACT: name is required for contract governance")
    if operation == "infer":
        inferred = memory_schema_module.infer_contract(
            vault_root, name=name, project=project, page_type=page_type
        )
        if save:
            inferred["saved"] = memory_schema_module.save_contract(
                vault_root,
                inferred["proposal"],
                expected_hash=expected_hash,
            )
        return inferred
    if save:
        raise ValueError("INVALID_SCHEMA_OPERATION: save is supported only for infer")
    contract, content_hash, path = memory_schema_module.load_contract(vault_root, name)
    if operation == "validate":
        result = memory_schema_module.validate_contract(
            vault_root, contract, strict=strict
        )
        result.update({"path": path, "content_hash": content_hash})
        return result
    if operation == "diff":
        if compare_to:
            after, after_hash, after_path = memory_schema_module.load_contract(
                vault_root, compare_to
            )
            comparison = {"kind": "contract", "path": after_path, "content_hash": after_hash}
        else:
            inferred = memory_schema_module.infer_contract(
                vault_root,
                name=name,
                project=contract.scope.project,
                page_type=contract.scope.page_type,
            )
            after = memory_schema_module.contract_from_dict(inferred["proposal"])
            comparison = {"kind": "corpus", "sample_size": inferred["sample_size"]}
        result = memory_schema_module.diff_contracts(contract, after)
        result.update(
            {
                "path": path,
                "content_hash": content_hash,
                "comparison": comparison,
            }
        )
        return result
    raise ValueError("INVALID_SCHEMA_OPERATION: operation must be infer, validate, or diff")


def op_manage_memory_file(
    vault_root: Path,
    operation: str = "list",
    path: str = "",
    content: str = "",
    frontmatter: dict | None = None,
    overwrite: bool = False,
    allow_curated: bool = False,
    kind: str = "file",
    parents: bool = True,
    recursive: bool = False,
    include_hidden: bool = False,
    old_path: str | None = None,
    new_path: str | None = None,
    update_wikilinks: bool = True,
    confirm: bool = False,
    force_orphan: bool = False,
    force_superseded: bool = False,
    expected_dead_inbound: list[str] | None = None,
    trash_path: str | None = None,
    restore_path: str | None = None,
    date: str | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> dict:
    """Manage files through one governed file operation.

    This is the tier-2 escape hatch for structures that do not fit typed
    memory commands. Destructive operations require the same explicit flags as
    their canonical leaves.

    Args:
        operation: list, create, append, move, delete, trash-list, or recover.
        path: Path for list/create/append/delete, or default recover trash path.
        content: Text body for create/append.
        frontmatter: Optional frontmatter for create.
        overwrite: Allow create to replace an existing file.
        allow_curated: Permit operations in curated trees where canonical leaves allow it.
        kind: file or dir for create.
        parents: Create parent folders in dir mode.
        recursive: Recurse for list or delete-directory.
        include_hidden: Include hidden files for list.
        old_path: Source path for move.
        new_path: Destination path for move.
        update_wikilinks: Rewrite inbound wikilinks on move.
        confirm: Required for delete.
        force_orphan: Allow delete despite inbound links.
        force_superseded: Allow delete of superseded history.
        expected_dead_inbound: Links expected to die in the same workflow.
        trash_path: Trash entry to recover.
        restore_path: Optional recovery destination.
        date: YYYY-MM-DD filter for trash-list.
        validate_only: Validate a Markdown create or append operation without writing.
        draft_id: Draft identity returned by validate_only.
        draft_hash: Exact reviewed draft hash returned by validate_only.
        draft_token: Opaque destination/date token returned by validate_only.
        semantic_transition_token: Opaque append transition token from validate_only.
        relation_disposition: Reviewed relation outcome for semantic create or append.
        relation_review_hash: Draft or transition hash covered by the relation review.
        relation_review_reason: Audit reason for a reviewed-none disposition.
    """
    creation_review_requested = any(
        value is not None
        for value in (
            draft_id,
            draft_hash,
            draft_token,
        )
    )
    append_review_requested = semantic_transition_token is not None
    shared_review_requested = validate_only or any(
        value is not None
        for value in (
            relation_disposition,
            relation_review_hash,
            relation_review_reason,
        )
    )
    if operation not in {"create", "append"} and (
        creation_review_requested
        or append_review_requested
        or shared_review_requested
    ):
        raise ValueError(
            "INVALID_FILE_OPERATION: validation and review fields require "
            "operation='create' or operation='append'"
        )
    if operation != "create" and creation_review_requested:
        raise ValueError(
            "INVALID_FILE_OPERATION: creation review fields require operation='create'"
        )
    if operation != "append" and append_review_requested:
        raise ValueError(
            "INVALID_FILE_OPERATION: semantic_transition_token requires "
            "operation='append'"
        )
    if operation == "list":
        return op_list_directory(vault_root, path=path, recursive=recursive, include_hidden=include_hidden)
    if operation == "create":
        return op_create_file(
            vault_root,
            path=path,
            content=content,
            frontmatter=frontmatter,
            overwrite=overwrite,
            allow_curated=allow_curated,
            kind=kind,
            parents=parents,
            validate_only=validate_only,
            draft_id=draft_id,
            draft_hash=draft_hash,
            draft_token=draft_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    if operation == "append":
        return op_append_to_file(
            vault_root,
            path=path,
            content=content,
            allow_curated=allow_curated,
            validate_only=validate_only,
            semantic_transition_token=semantic_transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    if operation == "move":
        if not old_path or not new_path:
            raise ValueError("INVALID_MOVE: move requires `old_path` and `new_path`")
        return op_move_file(
            vault_root,
            old_path=old_path,
            new_path=new_path,
            update_wikilinks=update_wikilinks,
            allow_curated=allow_curated,
        )
    if operation == "delete":
        return op_delete(
            vault_root,
            path=path,
            confirm=confirm,
            recursive=recursive,
            force_orphan=force_orphan,
            force_superseded=force_superseded,
            allow_curated=allow_curated,
            expected_dead_inbound=expected_dead_inbound,
        )
    if operation == "trash-list":
        return op_list_trash(vault_root, date=date)
    if operation == "recover":
        target = trash_path or path
        if not target:
            raise ValueError("INVALID_PATH: recover requires `trash_path` or `path`")
        return op_recover_from_trash(
            vault_root,
            trash_path=target,
            restore_path=restore_path,
            allow_curated=allow_curated,
        )
    raise ValueError(
        "INVALID_MODE: manage_memory_file operation must be list, create, append, "
        "move, delete, trash-list, or recover"
    )


def op_query_dataset(
    vault_root: Path,
    path: str,
    record_path: str | None = None,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 100,
    offset: int = 0,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
) -> dict:
    """Query a CSV, TSV, or JSON dataset under the vault.

    Use after `ask_memory` or `browse_memory` identifies a dataset card or raw
    file. This returns exact rows or aggregates without dumping whole files.

    Args:
        path: Vault-relative dataset path.
        record_path: Dotted JSON array path.
        filters: List of filter objects.
        columns: Columns to project.
        sort_by: Column to sort by.
        descending: Sort descending.
        limit: Row cap.
        offset: Pagination offset.
        aggregate: count, profile, or func:column.
        date_from: Date range start.
        date_to: Date range end.
        date_column: Date column name.
    """
    return op_query_data(
        vault_root,
        path=path,
        record_path=record_path,
        filters=filters,
        columns=columns,
        sort_by=sort_by,
        descending=descending,
        limit=limit,
        offset=offset,
        aggregate=aggregate,
        date_from=date_from,
        date_to=date_to,
        date_column=date_column,
    )

def remember_description(project_keys_hint: str) -> str:
    """The `remember` MCP description with the live project-key hint substituted."""
    return (op_remember.__doc__ or "").replace("__PROJECT_KEYS_HINT__", project_keys_hint)


def op_coordination_status(vault_root: Path) -> dict:
    """Report this replica's writer-lease role and coordinator health.

    Read-only and safe during coordinator outages. Credentials and vault content
    are never included.
    """
    from .writer_lease import coordination_status

    return coordination_status(vault_root)

def note_description(project_keys_hint: str) -> str:
    """The `note` MCP description with the live project-key hint substituted in.

    `note` is a hand-registered MCP exception precisely because its description is
    per-vault: the build injects the current project-key list/contract here so the
    tool schema advertises live keys instead of a frozen list.
    """
    return (op_note.__doc__ or "").replace("__PROJECT_KEYS_HINT__", project_keys_hint)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
# (name, leaf, tier, cli_writes, needs_schema, cli_positional, surfaces)
_CONNECT_MEMORY_READ_ONLY_OPERATIONS = frozenset(
    {
        "suggest-links",
        "suggest-relations",
        "context",
        "graph-context",
        "inbound-links",
        "resolve-entity",
    }
)
_ADOPT_VAULT_READ_ONLY_MODES = frozenset({"scan-only"})
_ADOPTION_STUDIO_READ_ONLY_ACTIONS = frozenset({"status", "work-item"})
_PROCESS_MEDIA_READ_ONLY_OPERATIONS = frozenset({"status"})
_OBSERVE_MEMORY_READ_ONLY_OPERATIONS = frozenset({"validate"})
_MISSING_SELECTOR_DEFAULT = object()


def _resolved_invocation_selector(
    command: Command, kwargs: dict[str, Any], selector: str
) -> Any:
    if selector in kwargs:
        return kwargs[selector]
    try:
        parameter = inspect.signature(command.leaf).parameters.get(selector)
    except (TypeError, ValueError):
        return _MISSING_SELECTOR_DEFAULT
    if parameter is None or parameter.default is inspect.Parameter.empty:
        return _MISSING_SELECTOR_DEFAULT
    return parameter.default


def invocation_is_read_only(command: Command, kwargs: dict[str, Any]) -> bool:
    """Classify one resolved product-command invocation for lease gating.

    Write-capable product commands default to requiring the lease. Mixed
    read/write commands opt into a finite read-only allowlist, with their
    Python signature defaults applied only when the selector was truly omitted.
    """
    if command.read_only:
        return True
    if command.name == "connect_memory":
        operation = _resolved_invocation_selector(command, kwargs, "operation")
        return (
            isinstance(operation, str)
            and operation in _CONNECT_MEMORY_READ_ONLY_OPERATIONS
        )
    if command.name == "adopt_vault":
        mode = _resolved_invocation_selector(command, kwargs, "mode")
        return isinstance(mode, str) and mode in _ADOPT_VAULT_READ_ONLY_MODES
    if command.name == "adoption_studio":
        action = _resolved_invocation_selector(command, kwargs, "action")
        return isinstance(action, str) and action in _ADOPTION_STUDIO_READ_ONLY_ACTIONS
    if command.name == "process_media":
        operation = _resolved_invocation_selector(command, kwargs, "operation")
        return (
            isinstance(operation, str)
            and operation in _PROCESS_MEDIA_READ_ONLY_OPERATIONS
        )
    if command.name == "observe_memory":
        operation = _resolved_invocation_selector(command, kwargs, "operation")
        return (
            isinstance(operation, str)
            and operation in _OBSERVE_MEMORY_READ_ONLY_OPERATIONS
        )
    if command.name == "maintain_memory":
        mode = _resolved_invocation_selector(command, kwargs, "mode")
        return mode == "audit"
    if command.name == "edit_memory":
        if kwargs.get("validate_only") is True:
            return True
        operation = kwargs.get("operation")
        if isinstance(operation, dict):
            return (
                operation.get("kind")
                in {"replace_string", "batch_replace", "patch_frontmatter"}
                and operation.get("validate_only") is True
            )
        return False
    if command.name in {"remember", "replace_memory"}:
        # A validate-only remember builds and returns an immutable draft and
        # writes nothing (note() returns its preflight before any commit), so
        # it needs neither writer authority nor the mutation boundary. Any
        # inconsistency from reading beside a concurrent write is caught by
        # the fresh under-lock re-validation at commit time.
        return kwargs.get("validate_only") is True
    if command.name == "manage_memory_file":
        operation = _resolved_invocation_selector(command, kwargs, "operation")
        return (
            operation in {"create", "append"}
            and kwargs.get("validate_only") is True
        )
    return False


_PRODUCT_ACTIONS: tuple[str, ...] = ("save", "adopt", "ask", "prove", "review", "update", "connect")
_SIMPLE_ACTIONS: tuple[str, ...] = (
    "ask",
    "remember",
    "capture",
    "review",
    "connect",
    "adopt",
    "maintain",
)
_SIMPLE_ACTION_PACK_ALIASES: dict[str, tuple[str, ...]] = {
    "ask": ("ask",),
    "remember": ("save", "update"),
    "capture": ("save", "prove"),
    "review": ("review",),
    "connect": ("connect",),
    "adopt": ("adopt",),
    "maintain": ("review", "update"),
}
_SIMPLE_ACTION_DEFS: dict[str, dict] = {
    "ask": {
        "intent": "Recall durable knowledge and cite useful context.",
        "route": {"tool": "ask_memory", "args": {"detail": "compact", "rerank": False}},
        "deep_route": {
            "tool": "ask_memory",
            "args": {"detail": "compact", "rerank": False, "deep": True},
        },
        "safety": "read-only; deep mode assembles context and graph enrichment stays explicit",
        "advanced": ["read_memory", "connect_memory", "review_memory"],
    },
    "remember": {
        "intent": "Save a durable conclusion as compiled governed knowledge.",
        "route": {"tool": "remember", "args": {"note_type": "insight"}},
        "safety": "additive write; uses note validation and does not preserve raw provenance unless sources are provided",
        "advanced": [
            "replace_memory",
            "edit_memory",
            "observe_memory",
            "connect_memory",
        ],
    },
    "capture": {
        "intent": "Capture raw material or proof-bearing text without turning it into a conclusion.",
        "route": {"tool": "capture_source", "args": {"source_type": "other"}},
        "evidence_route": {"tool": "preserve_evidence", "args": {}},
        "safety": "additive write; Sources and Evidence preserve originals/provenance",
        "advanced": ["transfer_artifact", "compile_source"],
    },
    "review": {
        "intent": "Review stale, contradictory, disconnected, or unprocessed knowledge before acting.",
        "route": {"tool": "review_memory", "args": {"mode": "attention"}},
        "audit_route": {"tool": "review_memory", "args": {"mode": "audit"}},
        "safety": "read-only by default; triage state changes are explicit through triage_memory",
        "advanced": [
            "review_memory",
            "review_item_context",
            "triage_memory",
            "compile_source",
        ],
    },
    "connect": {
        "intent": "Find links or typed relations that make the knowledge graph denser.",
        "route": {"tool": "connect_memory", "args": {"operation": "suggest-links"}},
        "relations_route": {"tool": "connect_memory", "args": {"operation": "suggest-relations"}},
        "safety": "proposal-only by default; suggested relations never write automatically",
        "advanced": ["connect_memory"],
    },
    "adopt": {
        "intent": "Assess or import an existing vault safely.",
        "route": {"tool": "adopt_vault", "args": {"mode": "scan-only"}},
        "safety": "scan-only by default; copy/compile modes require explicit options and preserve originals",
        "advanced": ["browse_memory", "compile_source"],
    },
    "maintain": {
        "intent": "Check vault health and repair drift only when explicitly requested.",
        "route": {"tool": "maintain_memory", "args": {"mode": "audit"}},
        "fix_route": {"tool": "maintain_memory", "args": {"mode": "fix", "dry_run": False}},
        "reconcile_route": {"tool": "maintain_memory", "args": {"mode": "reconcile", "dry_run": False}},
        "safety": "read-only by default; write-capable fixes require explicit flags",
        "advanced": ["maintain_memory", "doctor"],
    },
}
_PRODUCT_METADATA: dict[str, dict] = {
    "coordination_status": {"surface": "advanced", "actions": ("review",), "first_run_safe": True},
    "bootstrap": {"surface": "primary", "actions": (), "first_run_safe": True},
    "adopt": {"surface": "primary", "actions": ("adopt",), "first_run_safe": True},
    "overview": {"surface": "primary", "actions": ("adopt",), "first_run_safe": True},
    "search": {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    "fetch": {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    "find": {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    "get": {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    "add": {"surface": "primary", "actions": ("save",), "first_run_safe": False},
    "note": {"surface": "primary", "actions": ("save", "update"), "first_run_safe": False},
    "preserve": {"surface": "primary", "actions": ("prove", "save"), "first_run_safe": False},
    "attention": {"surface": "primary", "actions": ("review",), "first_run_safe": True},
    "review_item_context": {
        "surface": "primary",
        "actions": ("review", "ask"),
        "first_run_safe": True,
    },
    "audit": {"surface": "primary", "actions": ("review",), "first_run_safe": True},
    "edit": {"surface": "primary", "actions": ("update",), "first_run_safe": False},
    "replace": {"surface": "primary", "actions": ("update",), "first_run_safe": False},
    "link": {"surface": "primary", "actions": ("connect", "save"), "first_run_safe": False},
    "suggest_links": {"surface": "primary", "actions": ("connect", "ask"), "first_run_safe": True},
    "graph_context": {"surface": "primary", "actions": ("ask", "connect"), "first_run_safe": True},
    "suggest_relations": {"surface": "primary", "actions": ("connect", "ask"), "first_run_safe": True},
    "propose_compilation": {"surface": "primary", "actions": ("review", "save"), "first_run_safe": True},
    "provenance_report": {"surface": "advanced", "actions": ("ask", "prove"), "first_run_safe": True},
    "evolution": {"surface": "advanced", "actions": ("ask", "review"), "first_run_safe": True},
    "reconcile": {"surface": "advanced", "actions": ("update",), "first_run_safe": False},
    "audit_fix": {"surface": "advanced", "actions": ("review", "update"), "first_run_safe": False},
}
_MCRC = frozenset({"mcp", "rest", "cli"})
_RC = frozenset({"rest", "cli"})
# `get_video_frames` returns MCP image content blocks (a FastMCP ToolResult) —
# meaningless through the REST/CLI JSON envelopes, so it is mcp-only.
_M = frozenset({"mcp"})
_SPEC: tuple[tuple, ...] = (
    ("coordination_status", op_coordination_status, 1, False, False, None, _MCRC),
    ("bootstrap", op_bootstrap, 1, False, False, None, _MCRC),
    ("search", op_search, 1, False, False, "query", _MCRC),
    ("fetch", op_fetch, 1, False, False, "id", _MCRC),
    ("find", op_find, 1, False, False, "query", _MCRC),
    ("suggest_links", op_suggest_links, 1, False, False, None, _MCRC),
    ("graph_context", op_graph_context, 1, False, False, "path", _MCRC),
    ("suggest_relations", op_suggest_relations, 1, False, False, None, _MCRC),
    ("add", op_add, 1, True, True, None, _MCRC),
    ("audit", op_audit, 1, False, False, None, _MCRC),
    ("attention", op_attention, 1, False, False, None, _MCRC),
    ("review_item_context", op_review_item_context, 1, False, False, "ref", _MCRC),
    ("overview", op_overview, 1, False, False, "path", _MCRC),
    ("adopt", op_adopt, 1, True, False, "path", _MCRC),
    ("evolution", op_evolution, 1, False, False, "query", _MCRC),
    ("audit_fix", op_audit_fix, 1, True, False, None, _MCRC),
    ("reconcile", op_reconcile, 1, True, False, None, _MCRC),
    ("provenance_report", op_provenance_report, 1, False, False, None, _MCRC),
    ("propose_compilation", op_propose_compilation, 1, False, False, None, _MCRC),
    ("get", op_get, 1, False, False, "path", _MCRC),
    ("edit", op_edit, 1, True, False, "path", _MCRC),
    ("observe_memory", op_observe_memory, 1, True, False, "path", _MCRC),
    ("replace", op_replace, 1, True, False, "old_path", _MCRC),
    ("link", op_link, 1, True, False, None, _MCRC),
    ("preserve", op_preserve, 1, True, False, None, _MCRC),
    # `note` is a hand-registered MCP exception (per-vault description); the registry
    # still drives its REST route + CLI subcommand from the same leaf.
    ("note", op_note, 1, True, False, None, _RC),
    ("query_data", op_query_data, 2, False, False, "path", _MCRC),
    ("create_file", op_create_file, 2, True, False, "path", _MCRC),
    ("list_directory", op_list_directory, 2, False, False, "path", _MCRC),
    ("move_file", op_move_file, 2, True, False, None, _MCRC),
    ("delete", op_delete, 2, True, False, "path", _MCRC),
    ("append_to_file", op_append_to_file, 2, True, False, "path", _MCRC),
    ("list_trash", op_list_trash, 2, False, False, None, _MCRC),
    ("recover_from_trash", op_recover_from_trash, 2, True, False, "trash_path", _MCRC),
    ("list_inbound_links", op_list_inbound_links, 2, False, False, "target", _MCRC),
    ("schema_memory", op_schema_memory, 1, True, False, None, _MCRC),
    ("get_video_frames", op_get_video_frames, 2, False, False, None, _M),
)


def _build_commands() -> tuple[Command, ...]:
    cmds: list[Command] = []
    for name, leaf, tier, writes, needs_schema, positional, surfaces in _SPEC:
        meta = _PRODUCT_METADATA.get(name, {})
        skip = 2 if needs_schema else 1
        desc = leaf.__doc__ or ""
        if name == "note":
            # Keep the registry description (OpenAPI/help) free of the MCP-only
            # placeholder; the live-hint substitution happens at MCP registration.
            desc = desc.replace("__PROJECT_KEYS_HINT__", "(any slug; unknown keys auto-register on first use)")
        cmds.append(
            Command(
                name=name,
                leaf=leaf,
                params=_derive_params(leaf, skip=skip, positional=positional),
                surfaces=surfaces,
                tier=tier,
                cli_writes=writes,
                needs_schema=needs_schema,
                description=desc,
                product_surface=meta.get("surface", "advanced"),
                product_actions=tuple(meta.get("actions", ())),
                first_run_safe=bool(meta.get("first_run_safe", False)),
            )
        )
    return tuple(cmds)


COMMANDS: tuple[Command, ...] = _build_commands()

_PRODUCT_SPEC: tuple[tuple, ...] = (
    (
        "coordination_status",
        op_coordination_status,
        1,
        False,
        False,
        None,
        _MCRC,
        ("coordination_status",),
        {"surface": "advanced", "actions": ("review",), "first_run_safe": True},
    ),
    (
        "bootstrap",
        op_bootstrap,
        1,
        False,
        False,
        None,
        _MCRC,
        ("bootstrap",),
        {"surface": "primary", "actions": (), "first_run_safe": True},
    ),
    (
        "ask_memory",
        op_ask_memory,
        1,
        False,
        False,
        "query",
        _MCRC,
        ("search", "find"),
        {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    ),
    (
        "read_memory",
        op_read_memory,
        1,
        False,
        False,
        "path",
        _MCRC,
        ("fetch", "get"),
        {"surface": "primary", "actions": ("ask",), "first_run_safe": True},
    ),
    (
        "browse_memory",
        op_browse_memory,
        1,
        False,
        False,
        "path",
        _MCRC,
        ("overview", "list_directory"),
        {"surface": "primary", "actions": ("adopt", "ask"), "first_run_safe": True},
    ),
    (
        "remember",
        op_remember,
        1,
        True,
        False,
        None,
        _MCRC,
        ("note",),
        {"surface": "primary", "actions": ("save", "update"), "first_run_safe": False},
    ),
    (
        "edit_memory",
        op_edit_memory,
        1,
        True,
        False,
        "path",
        _MCRC,
        ("edit",),
        {"surface": "primary", "actions": ("update",), "first_run_safe": False},
    ),
    (
        "observe_memory",
        op_observe_memory,
        1,
        True,
        False,
        "path",
        _MCRC,
        ("observe_memory",),
        {"surface": "primary", "actions": ("update", "save"), "first_run_safe": False},
    ),
    (
        "replace_memory",
        op_replace_memory,
        1,
        True,
        False,
        "old_path",
        _MCRC,
        ("replace",),
        {"surface": "primary", "actions": ("update",), "first_run_safe": False},
    ),
    (
        "capture_source",
        op_capture_source,
        1,
        True,
        True,
        None,
        _MCRC,
        ("add", "propose_compilation"),
        {"surface": "primary", "actions": ("save",), "first_run_safe": False},
    ),
    (
        "compile_source",
        op_compile_source,
        1,
        False,
        False,
        None,
        _MCRC,
        ("propose_compilation",),
        {"surface": "primary", "actions": ("review", "save"), "first_run_safe": True},
    ),
    (
        "preserve_evidence",
        op_preserve_evidence,
        1,
        True,
        False,
        None,
        _MCRC,
        ("preserve",),
        {"surface": "primary", "actions": ("prove", "save"), "first_run_safe": False},
    ),
    (
        "transfer_artifact",
        op_transfer_artifact,
        1,
        True,
        False,
        None,
        _MCRC,
        ("transfer_token",),
        {"surface": "primary", "actions": ("prove",), "first_run_safe": True},
    ),
    (
        "process_media",
        op_process_media,
        1,
        True,
        False,
        None,
        _MCRC,
        (),
        {
            "surface": "advanced",
            "actions": ("prove", "review", "update"),
            "first_run_safe": False,
        },
    ),
    (
        "review_memory",
        op_review_memory,
        1,
        False,
        False,
        None,
        _MCRC,
        ("attention", "audit", "evolution", "provenance_report", "propose_compilation"),
        {"surface": "primary", "actions": ("review", "ask", "prove"), "first_run_safe": True},
    ),
    (
        "review_item_context",
        op_review_item_context,
        1,
        False,
        False,
        "ref",
        _MCRC,
        ("review_item_context",),
        {"surface": "primary", "actions": ("review", "ask"), "first_run_safe": True},
    ),
    (
        "triage_memory",
        op_triage_memory,
        1,
        True,
        False,
        "ref",
        _MCRC,
        ("attention",),
        {"surface": "primary", "actions": ("review", "update"), "first_run_safe": False},
    ),
    (
        "connect_memory",
        op_connect_memory,
        1,
        True,
        False,
        None,
        _MCRC,
        ("suggest_links", "graph_context", "suggest_relations", "link", "list_inbound_links"),
        {"surface": "primary", "actions": ("connect", "ask", "save"), "first_run_safe": True},
    ),
    (
        "adopt_vault",
        op_adopt_vault,
        1,
        True,
        False,
        "path",
        _MCRC,
        ("adopt",),
        {"surface": "primary", "actions": ("adopt",), "first_run_safe": True},
    ),
    (
        "adoption_studio",
        op_adoption_studio,
        1,
        True,
        False,
        None,
        _MCRC,
        ("adopt",),
        {"surface": "primary", "actions": ("adopt", "review", "save"), "first_run_safe": True},
    ),
    (
        "maintain_memory",
        op_maintain_memory,
        1,
        True,
        False,
        None,
        _MCRC,
        ("audit", "audit_fix", "reconcile"),
        {"surface": "advanced", "actions": ("review", "update"), "first_run_safe": True},
    ),
    (
        "schema_memory",
        op_schema_memory,
        1,
        True,
        False,
        None,
        _MCRC,
        ("schema_memory",),
        {"surface": "advanced", "actions": ("review", "update"), "first_run_safe": True},
    ),
    (
        "manage_memory_file",
        op_manage_memory_file,
        2,
        True,
        False,
        None,
        _MCRC,
        (
            "create_file",
            "list_directory",
            "move_file",
            "delete",
            "append_to_file",
            "list_trash",
            "recover_from_trash",
        ),
        {"surface": "advanced", "actions": ("update", "ask"), "first_run_safe": False},
    ),
    (
        "query_dataset",
        op_query_dataset,
        2,
        False,
        False,
        "path",
        _MCRC,
        ("query_data",),
        {"surface": "advanced", "actions": ("ask",), "first_run_safe": True},
    ),
    (
        "read_media",
        op_read_media,
        2,
        False,
        False,
        "path",
        _M,
        ("get_video_frames",),
        {"surface": "advanced", "actions": ("ask",), "first_run_safe": True},
    ),
)


def _build_product_commands() -> tuple[Command, ...]:
    cmds: list[Command] = []
    for name, leaf, tier, writes, needs_schema, positional, surfaces, routes, meta in _PRODUCT_SPEC:
        skip = 2 if needs_schema else 1
        desc = leaf.__doc__ or ""
        params = _derive_params(leaf, skip=skip, positional=positional)
        if name == "edit_memory":
            params = tuple(
                Param(
                    name=param.name,
                    type=param.type,
                    required=True if param.name == "operation" else param.required,
                    help=param.help,
                    cli_positional=param.cli_positional,
                    choices=param.choices,
                )
                for param in params
                if param.name in {"path", "why", "operation"}
            )
        if writes:
            params = (
                *params,
                Param(
                    name="response_detail",
                    type="str",
                    help=(
                        "Successful committed mutation detail: compact (default), "
                        "full diagnostics, or legacy raw leaf result."
                    ),
                    choices=("compact", "full", "legacy"),
                ),
            )
        if name == "remember":
            generic_hint = "(any slug; unknown keys auto-register on first use)"
            desc = desc.replace("__PROJECT_KEYS_HINT__", generic_hint)
            params = tuple(
                Param(
                    name=p.name,
                    type=p.type,
                    required=p.required,
                    help=p.help.replace("__PROJECT_KEYS_HINT__", generic_hint),
                    cli_positional=p.cli_positional,
                    choices=p.choices,
                )
                for p in params
            )
        if name in {
            "remember",
            "replace_memory",
            "observe_memory",
            "edit_memory",
            "manage_memory_file",
        }:
            desc = semantic_authoring_module.project_tool_description(name, desc)
            params = tuple(
                Param(
                    name=param.name,
                    type=param.type,
                    required=param.required,
                    help=(
                        " ".join(
                            part
                            for part in (
                                param.help.strip(),
                                semantic_authoring_module.render_parameter_guidance(
                                    name, param.name
                                ),
                            )
                            if part
                        )
                    ),
                    cli_positional=param.cli_positional,
                    choices=param.choices,
                )
                for param in params
            )
        cmds.append(
            Command(
                name=name,
                leaf=leaf,
                params=params,
                surfaces=surfaces,
                tier=tier,
                cli_writes=writes,
                needs_schema=needs_schema,
                description=desc,
                product_surface=meta.get("surface", "advanced"),
                product_actions=tuple(meta.get("actions", ())),
                first_run_safe=bool(meta.get("first_run_safe", False)),
                routes=tuple(routes),
                response_detail=writes,
            )
        )
    return tuple(cmds)


PRODUCT_COMMANDS: tuple[Command, ...] = _build_product_commands()
PRODUCT_PUBLIC_NAMES: tuple[str, ...] = tuple(c.name for c in PRODUCT_COMMANDS)
PRODUCT_ROUTE_HELPERS: frozenset[str] = frozenset({"transfer_token"})
HAND_REGISTERED_EXCEPTIONS: frozenset[str] = frozenset()

HOSTED_ALPHA_AGENT_PROFILE = "hosted-alpha-agent-v1"


@dataclass(frozen=True, slots=True)
class ProductSurfaceProfile:
    """One immutable, ordered exposure policy over canonical product commands."""

    name: str
    command_names: tuple[str, ...]

    def __post_init__(self) -> None:
        names = tuple(self.command_names)
        if not self.name:
            raise ValueError("product surface profile name must be non-empty")
        if not names or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("product surface profile commands must be non-empty strings")
        if len(names) != len(set(names)):
            raise ValueError("product surface profile contains duplicate commands")
        object.__setattr__(self, "command_names", names)


PRODUCT_SURFACE_PROFILES = MappingProxyType(
    {
        HOSTED_ALPHA_AGENT_PROFILE: ProductSurfaceProfile(
            name=HOSTED_ALPHA_AGENT_PROFILE,
            command_names=(
                "bootstrap",
                "ask_memory",
                "read_memory",
                "browse_memory",
                "remember",
                "observe_memory",
                "capture_source",
                "compile_source",
                "preserve_evidence",
                "review_memory",
                "review_item_context",
                "triage_memory",
                "connect_memory",
            ),
        )
    }
)


def commands_for(surface: str, *, expose_tier2: bool = True) -> tuple[Command, ...]:
    """Canonical implementation commands exposed by the old primitive registry."""
    return tuple(
        c for c in COMMANDS if surface in c.surfaces and (expose_tier2 or c.tier == 1)
    )


def product_commands_for(surface: str, *, expose_tier2: bool = True) -> tuple[Command, ...]:
    """Product commands exposed on a public surface, honoring tier-2 opt-out."""
    return tuple(
        c
        for c in PRODUCT_COMMANDS
        if surface in c.surfaces and (expose_tier2 or c.tier == 1)
    )


def product_commands_for_profile(
    profile: str,
    surface: str,
) -> tuple[Command, ...]:
    """Resolve a pinned surface profile to its canonical command objects."""

    definition = PRODUCT_SURFACE_PROFILES.get(profile)
    if definition is None:
        raise ValueError(f"unsupported product surface profile: {profile!r}")

    canonical = {command.name: command for command in PRODUCT_COMMANDS}
    selected: list[Command] = []
    for name in definition.command_names:
        command = canonical.get(name)
        if command is None:
            raise RuntimeError(
                f"product surface profile {profile!r} references missing command {name!r}"
            )
        if command.tier != 1 or surface not in command.surfaces:
            raise RuntimeError(
                f"product surface profile {profile!r} cannot expose {name!r} on {surface!r}"
            )
        selected.append(command)
    return tuple(selected)


def validate_product_registry() -> dict:
    """Validate product route metadata against canonical implementation leaves."""
    canonical = {c.name for c in COMMANDS}
    route_refs = {route for cmd in PRODUCT_COMMANDS for route in cmd.routes}
    unknown = route_refs - canonical - PRODUCT_ROUTE_HELPERS
    if unknown:
        raise RuntimeError(f"product command route(s) reference unknown leaves: {sorted(unknown)}")

    covered = route_refs & canonical
    public_canonical = {
        c.name for c in COMMANDS if c.surfaces & _MCRC
    }
    missing = public_canonical - covered
    if missing:
        raise RuntimeError(f"canonical capability missing product route: {sorted(missing)}")
    return {
        "product_commands": [c.name for c in PRODUCT_COMMANDS],
        "canonical_covered": sorted(covered),
        "helpers": sorted(PRODUCT_ROUTE_HELPERS & route_refs),
    }


validate_product_registry()


def _active_bootstrap_descriptor() -> capabilities_module.ActiveSurfaceDescriptor:
    """Resolve trusted adapter context or the direct-Python compatibility default."""

    active = capabilities_module.current_active_surface()
    if active is not None:
        return active
    return capabilities_module.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="canonical-full-product",
        tier2_enabled=True,
        product_commands=tuple(
            command.name
            for command in product_commands_for("mcp", expose_tier2=True)
        ),
    )


def product_tool_catalog(
    available_tools: frozenset[str] | set[str] | None = None,
    *,
    callable_tools: frozenset[str] | set[str] | None = None,
) -> dict:
    """Registry-derived product surface: primary tools first, advanced tools visible."""
    selected = tuple(
        command
        for command in PRODUCT_COMMANDS
        if available_tools is None or command.name in available_tools
    )
    primary = [c.name for c in selected if c.product_surface == "primary"]
    advanced = [c.name for c in selected if c.product_surface != "primary"]
    return {
        "primary": primary,
        "advanced": advanced,
        "first_run_safe": [c.name for c in selected if c.first_run_safe],
        "routes": {
            c.name: [
                route
                for route in c.routes
                if callable_tools is None or route in callable_tools
            ]
            for c in selected
        },
    }


_DROP_BOOTSTRAP_VALUE = object()


def _bootstrap_known_callable_names() -> frozenset[str]:
    return frozenset(
        {
            *PRODUCT_PUBLIC_NAMES,
            *(command.name for command in COMMANDS),
            *PRODUCT_ROUTE_HELPERS,
            *simple_action_names(),
        }
    )


def _mentions_unavailable_callable(
    value: str, unavailable: frozenset[str]
) -> bool:
    for name in unavailable:
        escaped = re.escape(name)
        if "_" in name or name in PRODUCT_PUBLIC_NAMES:
            if re.search(rf"(?<!\w){escaped}(?!\w)", value):
                return True
        elif re.search(rf"(?<!\w){escaped}\s*\(", value):
            return True
    return False


def _filter_bootstrap_payload(
    payload: dict,
    descriptor: capabilities_module.ActiveSurfaceDescriptor,
) -> dict:
    """Remove recommendations that the trusted active surface cannot execute."""

    unavailable = _bootstrap_known_callable_names() - descriptor.callable_commands
    unavailable_products = frozenset(PRODUCT_PUBLIC_NAMES) - frozenset(
        descriptor.product_commands
    )
    if not unavailable and not unavailable_products:
        return payload

    def filter_value(value: object) -> object:
        if isinstance(value, str):
            if value in unavailable_products or _mentions_unavailable_callable(
                value, unavailable
            ):
                return _DROP_BOOTSTRAP_VALUE
            return value
        if isinstance(value, (list, tuple)):
            filtered = []
            for item in value:
                candidate = filter_value(item)
                if candidate is not _DROP_BOOTSTRAP_VALUE:
                    filtered.append(candidate)
            return tuple(filtered) if isinstance(value, tuple) else filtered
        if isinstance(value, dict):
            advertised_tool = value.get("tool")
            if isinstance(advertised_tool, str) and advertised_tool in unavailable:
                return _DROP_BOOTSTRAP_VALUE
            call = value.get("call")
            if isinstance(call, str) and _mentions_unavailable_callable(
                call, unavailable
            ):
                return _DROP_BOOTSTRAP_VALUE
            route = value.get("route")
            if isinstance(route, str) and route in unavailable:
                return _DROP_BOOTSTRAP_VALUE

            filtered_dict: dict = {}
            for child_key, child in value.items():
                if child_key in unavailable_products:
                    continue
                candidate = filter_value(child)
                if candidate is _DROP_BOOTSTRAP_VALUE:
                    if child_key == "route":
                        filtered_dict["available"] = False
                        filtered_dict["unavailable_reason"] = (
                            "No route for this action is exported by the active surface."
                        )
                    continue
                filtered_dict[child_key] = candidate
            return filtered_dict
        return value

    filtered = filter_value(payload)
    assert isinstance(filtered, dict)
    return filtered


def _catalog_route_tools(entry: dict) -> set[str]:
    tools: set[str] = set()
    for key, value in entry.items():
        if key == "route" or key.endswith("_route"):
            if isinstance(value, dict) and value.get("tool"):
                tools.add(str(value["tool"]))
    for value in entry.get("advanced", []):
        tools.add(str(value))
    return tools


def simple_action_names() -> tuple[str, ...]:
    """The stable, beginner-facing action vocabulary."""
    return _SIMPLE_ACTIONS


def simple_action_catalog(
    selected_packs: dict | None = None,
    *,
    available_tools: frozenset[str] | set[str] | None = None,
) -> dict:
    """Product action map over product commands; no duplicate command logic."""
    known_commands = {command.name for command in PRODUCT_COMMANDS} | {"doctor"}
    out: dict[str, dict] = {}
    for action in _SIMPLE_ACTIONS:
        definition = _SIMPLE_ACTION_DEFS[action]
        missing = sorted(_catalog_route_tools(definition) - known_commands)
        if missing:
            raise RuntimeError(
                f"simple action {action!r} references unknown route(s): {missing}"
            )
        out[action] = {
            "intent": definition["intent"],
            "safety": definition["safety"],
            "advanced": [
                tool
                for tool in definition.get("advanced", [])
                if available_tools is None or tool in available_tools
            ],
        }
        primary_route = definition["route"]
        if available_tools is None or primary_route["tool"] in available_tools:
            out[action]["route"] = primary_route
        else:
            out[action]["available"] = False
            out[action]["unavailable_reason"] = (
                "No route for this action is exported by the active surface."
            )
        for key in (
            "deep_route",
            "evidence_route",
            "audit_route",
            "relations_route",
            "fix_route",
            "reconcile_route",
        ):
            if key in definition and (
                available_tools is None
                or definition[key]["tool"] in available_tools
            ):
                out[action][key] = definition[key]

    packs = (selected_packs or {}).get("packs") or []
    if packs:
        for action, aliases in _SIMPLE_ACTION_PACK_ALIASES.items():
            alias_set = set(aliases)
            guidance = []
            for pack in packs:
                if not (alias_set & set(pack.get("actions") or [])):
                    continue
                guidance.append(
                    {
                        "pack_id": pack.get("id"),
                        "name": pack.get("name"),
                        "agent_instructions": pack.get("agent_instructions"),
                        "suggested_workflows": pack.get("suggested_workflows") or [],
                    }
                )
            if guidance:
                out[action]["selected_pack_guidance"] = guidance
    return out


def product_front_door_catalog(
    selected_packs: dict | None = None,
    *,
    available_tools: frozenset[str] | set[str] | None = None,
) -> dict:
    """Map simple product verbs to the typed tools that enforce governance."""
    out = {
        action: {"primary_tools": [], "advanced_tools": []}
        for action in _PRODUCT_ACTIONS
    }
    for command in PRODUCT_COMMANDS:
        if available_tools is not None and command.name not in available_tools:
            continue
        bucket = "primary_tools" if command.product_surface == "primary" else "advanced_tools"
        for action in command.product_actions:
            if action in out:
                out[action][bucket].append(command.name)
    out["adopt"]["contract"] = "scan-only by default; write modes preserve originals and stay under Knowledge Base/"
    out["ask"]["contract"] = "retrieve with citations; prefer compiled notes, then sources/evidence for provenance"
    out["prove"]["contract"] = "use Evidence/proof for cases, claims, disputes, warranties, records, or other proof contexts"
    out["review"]["contract"] = "surface review queues and lint findings; do not auto-change conclusions"
    out["save"]["contract"] = "raw material becomes Sources; durable conclusions become governed notes/entities"
    out["update"]["contract"] = "edit or supersede with an explicit reason; keep history"
    out["connect"]["contract"] = "link entities and related notes so the graph compounds"

    packs = (selected_packs or {}).get("packs") or []
    if packs:
        for action in out:
            guidance = []
            for pack in packs:
                if action not in set(pack.get("actions") or []):
                    continue
                guidance.append(
                    {
                        "pack_id": pack.get("id"),
                        "name": pack.get("name"),
                        "agent_instructions": pack.get("agent_instructions"),
                        "suggested_workflows": pack.get("suggested_workflows") or [],
                    }
                )
            if guidance:
                out[action]["selected_pack_guidance"] = guidance
    return out
