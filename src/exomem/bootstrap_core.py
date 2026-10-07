"""The compact bootstrap as a small core plus named, on-demand sections.

`commands.op_bootstrap` still builds the complete compact payload (the "reference"
payload) exactly as before. This module projects it two ways:

- `project_core` keeps the rules that must be in front of an agent on every session
  and replaces reference detail with a one-line pointer;
- `section_payload` returns the original blocks, byte-identical, grouped by the task
  that needs them.

Every top-level key of the reference payload is either carried by the core verbatim
or belongs to exactly one section (`SECTIONS`), so nothing is lost by the split.
`tests/test_bootstrap_core.py` pins that, and pins the rule manifest the core must
carry (`CORE_RULES`).
"""

from __future__ import annotations

import json
from typing import Any

#: section name -> (reference-payload keys it returns, when to fetch it).
SECTIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "authoring": (
        ("authoring_contract", "semantic_authoring"),
        "writing a compiled note or a semantic unit: recipes, reviewed creation, advisories",
    ),
    "entities": (
        ("entity_registry", "vocabulary_workflow", "relation_vocabulary", "source_taxonomy"),
        "vocabulary_workflow (the workflow loop's vocabulary steps), entity types, relations, source classification",
    ),
    "records_planning": (
        ("records", "planning", "workflow_contracts"),
        "an observed outcome, a plan item, or a workflow-contract question",
    ),
    "routing": (
        (
            "simple_actions",
            "front_door_actions",
            "product_commands",
            "tool_defaults",
            "common_tools",
            "common_actions",
            "search_guidance",
        ),
        "choosing among tools beyond the core routing table; find knobs and score reading",
    ),
    "adoption": (
        ("knowledge_packs", "memory_model", "workflow_skills"),
        "first run, adopting a vault, choosing a knowledge pack",
    ),
    "envelope": (
        ("engagement",),
        "the delegation envelope classes and protocol, in full",
    ),  # returns only `engagement.envelope`; see `_section_blocks`
    "epistemics": (
        ("epistemic_contract", "due_state"),
        "the epistemic vocabulary, capture-the-outcome and prediction guidance; the due-item list",
    ),
    "diagnostics_reading": (
        ("performance_profiles", "server", "latency"),
        "reading timings; recall-latency breaches; compute policy and tool-surface identity in full",
    ),
}

#: What the core says about recall latency, and only while a breach is active (absent, so
#: 0 bytes, when healthy). The full block, with its spans and figures, is in the
#: `diagnostics_reading` section and in a session profile.
LATENCY_POINTER = "breach; section=diagnostics_reading"

#: The core lists at most this many entity type ids; the rest are in the `entities`
#: section. Vault-declared types are unbounded, so the core's size must not follow them.
CORE_ENTITY_TYPE_CAP = 12

#: Keys the core carries verbatim from the reference payload.
_VERBATIM = ("contract_version", "profile", "governance", "workflow", "active_capabilities")

#: Post-write handling the installed skill does not carry, served to a session client.
SESSION_POST_WRITE_KEYS = (
    "due_state",
    "due_state_handling",
    "due_state_authority",
    "capture_sweep_handling",
    "artifact_role_state_handling",
    "review_reason",
    "family_disposition",
    "family_disposition_reading",
)

#: Live, vault-derived keys added after the reference payload is built. The core serves
#: neither in full: `due_state` as a counts summary (`_due_summary`), `latency` not at
#: all. Both are bounded only by what the vault holds, so their full forms live in
#: sections (`epistemics` and `diagnostics_reading`); a session client gets both in full.
_PASSTHROUGH = ("due_state", "latency")


def accepted_sections() -> tuple[str, ...]:
    return ("all", "index", *SECTIONS)


def _size(value: object) -> int:
    return len(json.dumps(value))


def _section_blocks(reference: dict, name: str) -> dict:
    """The reference blocks a section returns. `envelope` is one sub-block of
    `engagement`: the rest of `engagement` is already in the core verbatim."""
    if name == "envelope":
        envelope = reference.get("engagement", {}).get("envelope")
        return {"engagement": {"envelope": envelope}} if envelope is not None else {}
    keys, _when = SECTIONS[name]
    return {key: reference[key] for key in keys if key in reference}


def sections_index(reference: dict) -> dict[str, int]:
    """Served-JSON bytes of each section against this reference payload."""
    return {name: _size(_section_blocks(reference, name)) for name in SECTIONS}


def section_payload(reference: dict, name: str) -> dict:
    """The blocks of one section, byte-identical to the reference payload."""
    if name == "all":
        return reference
    if name == "index":
        return {
            "contract_version": reference["contract_version"],
            "profile": "compact",
            "section": "index",
            "sections": {
                section: {"bytes": size, "fetch_when": SECTIONS[section][1]}
                for section, size in sections_index(reference).items()
            },
        }
    if name not in SECTIONS:
        raise ValueError(
            f"bootstrap: section must be one of {', '.join(accepted_sections())}, got {name!r}"
        )
    return {
        "contract_version": reference["contract_version"],
        "profile": "compact",
        "section": name,
        **_section_blocks(reference, name),
    }


def _route(entry: object) -> str | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
        return None
    args = entry.get("args")
    if isinstance(args, dict) and args:
        return entry["tool"] + "(" + ", ".join(f"{k}={v}" for k, v in args.items()) + ")"
    return entry["tool"]


def _routing(reference: dict) -> dict:
    """One intent-to-tool table in place of five statements of the same routing."""
    table: dict[str, str] = {}
    for action, entry in reference.get("simple_actions", {}).items():
        if not isinstance(entry, dict):
            continue
        routes = [
            text
            for key, value in entry.items()
            if key == "route" or key.endswith("_route")
            for text in (_route(value),)
            if text
        ]
        if routes:
            table[action] = "; ".join(dict.fromkeys(routes))
    search = reference.get("search_guidance", {})
    routing: dict[str, Any] = {"by_intent": table}
    for key in ("prefer_compiled_default", "compiled_types", "raw_types"):
        if key in search:
            routing[key] = search[key]
    # The graph-value benchmark's assistant-bootstrap probe requires the compact payload
    # to teach filter-only lookup; the full wording is `search_guidance` in `routing`.
    if "filter_only" in search.get("semantic_recall", {}):
        routing["filter_only"] = (
            "an empty query with filters is a filter-only lookup, most recent first, "
            "not a text match"
        )
    return routing


def _write(reference: dict) -> dict:
    authoring = reference.get("authoring_contract", {})
    post = authoring.get("post_write", {})
    write: dict[str, Any] = {}
    for key in ("canonical_loop", "preflight"):
        if key in authoring:
            write[key] = authoring[key]
    for key in ("due_state_handling", "due_state_authority"):
        if key in post:
            write[key] = post[key]
    return write


def _capture_semantics(reference: dict) -> dict:
    semantic = reference.get("semantic_authoring", {})
    compact = semantic.get("compact", {})
    unit = semantic.get("minimum_semantic_unit", {})
    entity = reference.get("entity_registry", {})
    out: dict[str, Any] = {}
    taxonomy = reference.get("source_taxonomy", {})
    for key in ("kind_rule", "migration"):
        if key in taxonomy:
            out[key] = taxonomy[key]
    for key in ("syntax", "canonical_section"):
        if key in compact:
            out[key] = compact[key]
    if "rule" in unit:
        out["minimum_unit"] = unit["rule"]
    if "capture_rule" in entity:
        out["entity_capture_rule"] = entity["capture_rule"]
    if "types" in entity:
        ids = [t["id"] for t in entity["types"] if isinstance(t, dict) and "id" in t]
        out["entity_types"] = ids[:CORE_ENTITY_TYPE_CAP]
        if len(ids) > CORE_ENTITY_TYPE_CAP:
            out["entity_types_more"] = (
                f"+{len(ids) - CORE_ENTITY_TYPE_CAP} more: section=entities"
            )
    return out


def _pointers(reference: dict) -> dict:
    """Where the reference detail lives; presence and route only."""
    pointers: dict[str, Any] = {}
    for key, source in (
        ("records", ("records", "route")),
        ("planning", ("planning", "route")),
        ("workflow_contracts", ("workflow_contracts", "route")),
    ):
        block = reference.get(source[0], {}).get(source[1])
        if block is not None:
            pointers[key] = block
    packs = reference.get("knowledge_packs", {})
    selected = packs.get("selected", {}).get("selected_pack_ids")
    if selected is not None:
        pointers["selected_packs"] = selected
    return pointers


def _envelope_digest(envelope: dict) -> dict:
    """The envelope with its four-step protocol reduced to one line, that line keeping
    the unclassified-action rule and naming the section that holds the full protocol.

    Everything else is kept as served, including any live `ignored` report of stored
    classes this version does not know: the spec requires every class with its ceiling,
    disposition and provenance, and a stored-config problem must stay visible.
    """
    digest = {key: value for key, value in envelope.items() if key != "protocol"}
    digest["protocol"] = (
        "name the action class first; an unclassified action has no authority; "
        "intent above its ceiling is a proposal, never an act; honour the disposition; "
        "record the outcome through triage; more: section=envelope"
    )
    return digest


def _due_summary(block: object) -> dict:
    """The core's view of the due-state advisory: how many, the biggest category, and
    where the list is. Bounded by construction, whatever the vault holds."""
    summary: dict[str, Any] = {}
    if isinstance(block, dict):
        if "total" in block:
            summary["total"] = block["total"]
        categories = block.get("categories")
        if isinstance(categories, dict) and categories:
            summary["top_category"] = max(categories, key=lambda name: categories[name])
    summary["list"] = "section=epistemics"
    return summary


def project_core(reference: dict) -> dict:
    """The always-served core for a reference compact payload."""
    core: dict[str, Any] = {key: reference[key] for key in _VERBATIM if key in reference}
    engagement = dict(reference["engagement"])
    if isinstance(engagement.get("envelope"), dict):
        engagement["envelope"] = _envelope_digest(engagement["envelope"])
    core["engagement"] = engagement
    server = reference.get("server", {})
    core["server"] = {
        key: server[key]
        for key in (
            "name",
            "version",
            "published_mcp_tool_surface_sha256",
            "published_mcp_tool_surface_scope",
        )
        if key in server
    }
    epistemic = reference.get("epistemic_contract", {})
    if "commitments" in epistemic:
        core["rules"] = {"epistemic": epistemic["commitments"]}
    core["write"] = _write(reference)
    core["routing"] = _routing(reference)
    core["capture_semantics"] = _capture_semantics(reference)
    core["pointers"] = _pointers(reference)
    core["sections"] = {
        "how": "bootstrap(section=<name>|index|all)",
        **sections_index(reference),
    }
    if "due_state" in reference:
        core["due_state"] = _due_summary(reference["due_state"])
    return core


#: Live, vault-derived blocks a session client receives verbatim: the skill carries the
#: static operating rules, not this state.
_SESSION_LIVE_KEYS = (
    "contract_version",
    "active_capabilities",
    "governance",
    "workflow_contracts",
    "relation_vocabulary",
    "entity_registry",
    "source_taxonomy",
    "vocabulary_workflow",
)


def project_session(reference: dict, core: dict) -> dict:
    """Live state for a client that already holds the installed skill's rules.

    Built on the core for what the core serves (`engagement`, `server`, the section
    index) and on the reference payload for the live blocks the skill cannot carry.
    The static rule digests (`rules`, `write`, `routing`, `capture_semantics`) are omitted:
    the skill carries them.
    """
    post_write = reference.get("authoring_contract", {}).get("post_write", {})
    session: dict[str, Any] = {
        key: reference[key] for key in _SESSION_LIVE_KEYS if key in reference
    }
    session["server"] = core["server"]
    session["engagement"] = core["engagement"]
    session["sections"] = core["sections"]
    session["profile"] = "session"
    packs = reference.get("knowledge_packs", {})
    session["knowledge_packs"] = {
        key: packs[key] for key in ("selected", "selection_rule") if key in packs
    }
    session["workflow"] = {"requested": reference["workflow"]["requested"]}
    session["authoring_contract"] = {
        "post_write": {k: post_write[k] for k in SESSION_POST_WRITE_KEYS if k in post_write}
    }
    for key in _PASSTHROUGH:
        if key in reference:
            session[key] = reference[key]
    session["loaded_operating_rules_prerequisite"] = (
        "The installed skill operating rules are loaded and match skill_contract."
    )
    session["compact_fallback"] = (
        "Request profile='compact' when local operating rules are unavailable or stale."
    )
    return session
