"""Pure construction and presentation of committed mutation terminals."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

log = logging.getLogger(__name__)

ResponseDetail = Literal["compact", "full", "legacy"]

_TERMINAL_MARKER = "exomem.mutation-terminal"
_TERMINAL_VERSION = 1
_RESPONSE_DETAILS = frozenset({"compact", "full", "legacy"})
_RECORD_RECEIPT_FIELDS = (
    "operation",
    "collection_id",
    "item_key",
    "before_item_hash",
    "after_item_hash",
    "before_manifest_hash",
    "after_manifest_hash",
    "before_container_hash",
    "after_container_hash",
    "affected_paths",
    "payload_hash",
    "outcome",
    "audit_correlation",
    "continuity",
    "acknowledged_gap_codes",
    "gap_fingerprint",
    "checkpoint_snapshot_hash",
    "minimum_reader_version",
)
_RECORD_RECEIPT_MARKER = "exomem.records-mutation"
_RECORD_RECEIPT_VERSION = 1
_LIFECYCLE_RECEIPT_VERSION = 2
_PLAN_RECEIPT_MARKER = "exomem.planning-mutation"
_PLAN_RECEIPT_FIELDS = (
    "operation",
    "collection_id",
    "plan_id",
    "before_item_hash",
    "after_item_hash",
    "before_container_hash",
    "after_container_hash",
    "affected_paths",
    "payload_hash",
    "outcome",
    "audit_correlation",
)

#: The closed mutation state machine. A client may branch on exactly these values and
#: MUST NOT infer any other. `needs_review` is explicitly NONTERMINAL: it means the
#: guarded write is mid-flight and the caller should complete the review step, not that
#: anything failed. Conflating it with failure is the 2026-08-06 misclassification.
STATES = ("needs_review", "committed", "rejected", "retryable", "indeterminate")
TERMINAL_STATES = frozenset({"committed", "rejected"})

#: The closed derived-graph outcome vocabulary a client may branch on. Absent
#: means no graph work was required; `pending` means the write is durable but
#: its derived graph is still rebuilding behind the response.
#:
#: Public because it is a cross-surface contract, not a projection detail:
#: `scripts/records_live_acceptance.py` validates against it and is NOT run by
#: CI, so a second hand-maintained copy of this vocabulary would drift silently
#: until someone ran the script against a real build. One definition, imported.
GRAPH_SYNC_OUTCOMES = frozenset({"completed", "failed", "pending"})
DERIVED_SYNC_OUTCOMES = frozenset({"completed", "failed", "pending"})
ADVISORY_SYNC_OUTCOMES = frozenset(
    {"completed", "failed", "not_required", "pending"}
)

_DERIVED_COMPONENT_NAMES = frozenset(
    {
        "freshness",
        "memory_refs",
        "resolver",
        "semantic_purge",
        "lexstore",
        "embeddings",
        "claims",
    }
)
_DERIVED_FAILURE = (
    "DERIVED_COMPONENT_FAILED",
    "Run reconcile to repair derived state.",
)
_ADVISORY_FAILURE = (
    "WRITE_ADVISORY_FAILED",
    "Query the advisory result or retry reconciliation.",
)
_MAX_DERIVED_DIAGNOSTICS = 9

#: Keys a client may branch on. Everything else in a response is advisory.
_ENVELOPE_KEYS = (
    "ok",
    "state",
    "terminal",
    "status",
    "mutated",
    "path",
    "paths",
    "operation_id",
    "error_code",
    "next_action",
    "request_id",
    "receipt_id",
)

#: Bounds on the free-text `warnings` compact carries beside `warnings_count`.
#: Same shape the client-artifact rows already use (see `_artifact_receipt_projection`),
#: because compact otherwise carries no unbounded strings at all and a bulk
#: `delete_directory` emits one warning per path.
_MAX_WARNINGS = 8
_MAX_WARNING_CHARS = 300


def _operation_id(result: Any) -> str | None:
    """Correlate a validate call with its later commit.

    Both halves of a guarded write already carry the same `draft_id` — it is minted at
    validation and handed back on commit — so the correlation identity exists and only
    needs naming. Nothing has to be threaded through the writer lease.
    """
    if not isinstance(result, Mapping):
        return None
    draft_id = result.get("draft_id")
    if isinstance(draft_id, str) and draft_id:
        return draft_id
    for nested_key in ("creation_commit", "creation_validation", "source"):
        nested = result.get(nested_key)
        if isinstance(nested, Mapping):
            nested_id = nested.get("draft_id")
            if isinstance(nested_id, str) and nested_id:
                return nested_id
    return None


def _without_graph_rebuild_handoff(result: Any) -> Any:
    if isinstance(result, Mapping) and "_graph_rebuild_handoff" in result:
        return {key: value for key, value in result.items() if key != "_graph_rebuild_handoff"}
    return result


def _warning_count(result: Any) -> int:
    if not isinstance(result, Mapping):
        return 0
    artifact_receipt = _artifact_receipt_projection(result)
    if artifact_receipt:
        return sum(
            len(item.get("warnings", []))
            for item in artifact_receipt["files"]
            if item["outcome"] == "stored"
        )
    warnings = result.get("warnings")
    if isinstance(warnings, (list, tuple)):
        return len(warnings)
    if warnings:
        return 1
    source = result.get("source")
    if isinstance(source, Mapping):
        source_warnings = source.get("warnings")
        if isinstance(source_warnings, (list, tuple)):
            return len(source_warnings)
        return 1 if source_warnings else 0
    return 0


def _warning_texts(result: Any, *, has_artifact_receipt: bool) -> list[str]:
    """The warnings behind ``warnings_count``, bounded for the compact envelope.

    A bare count is not actionable: the caller learns something was wrong but
    not what, and the texts were only reachable by re-issuing the whole call
    with ``detail="full"``.

    Follows ``_warning_count``'s source order exactly so the list and the count
    always describe the same warnings. Artifact receipts are skipped because
    ``_artifact_receipt_projection`` already puts their per-file warnings in the
    compact ``files`` rows; repeating them here would show each one twice.

    Bounded deliberately. These are the only free-text strings compact carries,
    and ``delete_directory``/``adopt`` emit one per path. ``warnings_count``
    stays authoritative, so a caller seeing fewer entries than the count knows
    the remainder was dropped and can ask for ``detail="full"``.
    """
    if not isinstance(result, Mapping) or has_artifact_receipt:
        return []
    warnings = result.get("warnings")
    if isinstance(warnings, (list, tuple)):
        candidates: Sequence[Any] = warnings
    elif warnings:
        candidates = [warnings]
    else:
        source = result.get("source")
        source_warnings = source.get("warnings") if isinstance(source, Mapping) else None
        if isinstance(source_warnings, (list, tuple)):
            candidates = source_warnings
        elif source_warnings:
            candidates = [source_warnings]
        else:
            return []
    return [
        (warning if isinstance(warning, str) else str(warning))[:_MAX_WARNING_CHARS]
        for warning in candidates[:_MAX_WARNINGS]
    ]


def _path_projection(result: Any) -> dict[str, Any]:
    """Adapt only the small set of explicit mutation path shapes we own."""
    if not isinstance(result, Mapping):
        return {"paths": []}
    artifact_receipt = _artifact_receipt_projection(result)
    if artifact_receipt:
        paths = [
            item["stored_path"] for item in artifact_receipt["files"] if item["outcome"] == "stored"
        ]
        if len(paths) == 1:
            return {"path": paths[0]}
        if paths:
            return {"paths": paths}
    path = result.get("path")
    if isinstance(path, str):
        return {"path": path}
    affected_paths = result.get("affected_paths")
    if (
        valid_collection_receipt(result)
        and isinstance(affected_paths, (list, tuple))
        and all(isinstance(item, str) for item in affected_paths)
    ):
        return {"paths": list(affected_paths)}
    raw_paths = result.get("paths")
    if isinstance(raw_paths, (list, tuple)) and all(isinstance(item, str) for item in raw_paths):
        return {"paths": list(raw_paths)}
    source = result.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("path"), str):
        return {"path": source["path"]}
    manifest = result.get("manifest")
    if isinstance(manifest, Mapping) and isinstance(manifest.get("path"), str):
        return {"path": manifest["path"]}
    copy = result.get("copy")
    if isinstance(copy, Mapping):
        copied_sources = copy.get("copied_sources")
        if isinstance(copied_sources, (list, tuple)):
            copied_paths = [
                item["source_path"]
                for item in copied_sources
                if isinstance(item, Mapping) and isinstance(item.get("source_path"), str)
            ]
            return {"paths": copied_paths}
    compile_plan = result.get("compile_plan")
    if isinstance(compile_plan, Mapping):
        copied_sources = compile_plan.get("copied_sources")
        if isinstance(copied_sources, (list, tuple)):
            copied_paths = [
                item["source_path"]
                for item in copied_sources
                if isinstance(item, Mapping) and isinstance(item.get("source_path"), str)
            ]
            return {"paths": copied_paths}
    old_path = result.get("old_path")
    new_path = result.get("new_path")
    if isinstance(old_path, str) and isinstance(new_path, str):
        return {"paths": [old_path, new_path]}
    restored_path = result.get("restored_path")
    if isinstance(restored_path, str):
        return {"path": restored_path}
    return {"paths": []}


def _artifact_receipt_projection(result: Any) -> dict[str, Any]:
    """Keep the bounded client-artifact outcome visible in compact terminals."""

    def string(value: Any, *, limit: int, allow_none: bool = False) -> bool:
        return (allow_none and value is None) or (
            isinstance(value, str) and 0 < len(value) <= limit
        )

    def nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def sha256(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    if not isinstance(result, Mapping):
        return {}
    files = result.get("files")
    summary = result.get("summary")
    if (
        not isinstance(files, (list, tuple))
        or not 1 <= len(files) <= 8
        or not isinstance(summary, Mapping)
    ):
        return {}

    def invalid_row(item: Any, index: int) -> dict[str, str]:
        file_id = item.get("file_id") if isinstance(item, Mapping) else None
        return {
            "file_id": file_id
            if isinstance(file_id, str) and string(file_id, limit=256)
            else f"invalid-file-{index + 1}",
            "outcome": "failed",
            "code": "INVALID_ARTIFACT_RECEIPT",
            "reason": "artifact result was invalid",
        }

    projected: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, Mapping) or not string(item.get("file_id"), limit=256):
            projected.append(invalid_row(item, index))
            continue
        outcome = item.get("outcome")
        if outcome == "stored":
            if not (
                string(item.get("stored_path"), limit=2048)
                and nonnegative_int(item.get("size"))
                and sha256(item.get("hash"))
                and item.get("hash_algorithm") == "sha256"
                and string(item.get("content_type"), limit=255, allow_none=True)
                and string(item.get("media_id"), limit=512, allow_none=True)
                and isinstance(item.get("warnings"), list)
                and len(item["warnings"]) <= 8
                and all(string(warning, limit=300) for warning in item["warnings"])
            ):
                projected.append(invalid_row(item, index))
                continue
            row = {
                key: item[key]
                for key in (
                    "file_id",
                    "outcome",
                    "stored_path",
                    "size",
                    "hash",
                    "hash_algorithm",
                    "media_id",
                    "content_type",
                    "warnings",
                )
                if key in item
            }
        elif (
            outcome == "failed"
            and string(item.get("code"), limit=64)
            and string(item.get("reason"), limit=300)
        ):
            row = {key: item[key] for key in ("file_id", "outcome", "code", "reason")}
        else:
            projected.append(invalid_row(item, index))
            continue
        projected.append(row)
    stored = sum(item["outcome"] == "stored" for item in projected)
    failed = len(projected) - stored
    return {"files": projected, "summary": {"stored": stored, "failed": failed}}


#: Bounds on the advisory structural suggestion compact may carry. Same posture as
#: `warnings`: advisory, projected from the leaf, and never something a client
#: branches on for the outcome of the mutation (see `_ENVELOPE_KEYS`).
_STRUCTURE_STRENGTHS = frozenset({"strong", "moderate"})
_MAX_STRUCTURE_REASONS = 8
_MAX_STRUCTURE_TERMS = 6
_MAX_STRUCTURE_TOKEN_CHARS = 64
#: Advisory kind emitted by the capture path. Its payload names the domain the
#: fallback captures share, not the off-scope units a compiled write reports.
_SOURCE_CLASSIFICATION_KIND = "source_classification_debt"


def _structure_suggestion_projection(leaf: Any) -> dict[str, Any] | None:
    """Lift one advisory structural suggestion out of a write leaf.

    Compiled creations carry it under `creation`, compiled edits under `semantic`,
    and a source capture carries it under `source`. The shape is re-validated
    here rather than trusted, so a malformed or oversized advisory is dropped
    instead of widening the wire contract.

    Each advisory kind declares the payload it is allowed to carry. Adding a kind
    therefore extends these bounds rather than bypassing them — the alternative,
    passing an unvalidated body through, is how an advisory channel turns into an
    arbitrary one.
    """
    if not isinstance(leaf, Mapping):
        return None
    for container_key in ("creation", "semantic", "source", None):
        container = leaf if container_key is None else leaf.get(container_key)
        if not isinstance(container, Mapping):
            continue
        value = container.get("structure_suggestion")
        if not isinstance(value, Mapping):
            continue
        kind = value.get("kind")
        strength = value.get("strength")
        reasons = value.get("reasons")
        if not isinstance(kind, str) or not kind or len(kind) > _MAX_STRUCTURE_TOKEN_CHARS:
            continue
        if strength not in _STRUCTURE_STRENGTHS:
            continue
        if not _bounded_tokens(reasons, _MAX_STRUCTURE_REASONS):
            continue
        common = {"kind": kind, "strength": strength, "reasons": list(reasons)}
        if kind == _SOURCE_CLASSIFICATION_KIND:
            domain = value.get("domain")
            captures = value.get("fallback_captures")
            if not isinstance(domain, str) or not 0 < len(domain) <= (_MAX_STRUCTURE_TOKEN_CHARS):
                continue
            if type(captures) is not int or captures < 0:
                continue
            return {**common, "domain": domain, "fallback_captures": captures}
        terms = value.get("cluster_terms")
        units = value.get("off_scope_units")
        if type(units) is not int or units < 0:
            continue
        if not _bounded_tokens(terms, _MAX_STRUCTURE_TERMS):
            continue
        return {**common, "off_scope_units": units, "cluster_terms": list(terms)}
    return None


#: Wire bounds on the advisory due-state block. Deliberately this module's own
#: numbers rather than an import of the producer's: the terminal re-validates what
#: a leaf attached instead of trusting it, and a bound that came from the producer
#: would validate nothing. The category vocabulary is NOT restated here for the
#: opposite reason — `audit` owns which categories exist, and a second copy of that
#: list would drift the day a category ships, so a category is checked as a bounded
#: token and its meaning is left to the module that owns it.
_MAX_DUE_STATE_CATEGORIES = 8
_MAX_DUE_STATE_TOP = 5
_MAX_DUE_STATE_TOKEN_CHARS = 64
_MAX_DUE_STATE_REF_CHARS = 256
#: Every reference this substrate puts on the wire is an `exomem://` URI.
_ADVISORY_REF_SCHEME = "exomem://"


def _due_state_projection(leaf: Any) -> tuple[dict[str, Any] | None, str]:
    """Lift one advisory due-state block out of a write leaf.

    Same posture as `_structure_suggestion_projection`, and for the same reason: a
    malformed or oversized advisory is DROPPED rather than allowed to widen the
    wire contract. The difference is what it describes — a structural suggestion is
    evidence about the page just written, while this is a bounded count of what the
    vault owes. Neither is ever a key a client branches on for the outcome of the
    mutation (see `_ENVELOPE_KEYS`), and both are absent — never null, never empty —
    when there is nothing to say.

    Returns the validated block and the vault it was computed for. The vault rides
    on the leaf under a server-internal `_vault` key that never reaches the wire —
    the block below is rebuilt from validated fields only — because the terminal is
    where emission is DECIDED and one process can serve more than one vault.
    """
    if not isinstance(leaf, Mapping):
        return None, ""
    for container_key in ("creation", "semantic", "source", None):
        container = leaf if container_key is None else leaf.get(container_key)
        if not isinstance(container, Mapping):
            continue
        value = container.get("due_state")
        if not isinstance(value, Mapping):
            continue
        total = value.get("total")
        categories = value.get("categories")
        top = value.get("top")
        if type(total) is not int or total <= 0:
            continue
        if not isinstance(categories, Mapping) or not (
            0 < len(categories) <= _MAX_DUE_STATE_CATEGORIES
        ):
            continue
        if not all(
            _due_state_token(name) and type(count) is int and count >= 0
            for name, count in categories.items()
        ):
            continue
        if not isinstance(top, (list, tuple)) or len(top) > _MAX_DUE_STATE_TOP:
            continue
        rows: list[dict[str, Any]] = []
        for row in top:
            if not isinstance(row, Mapping):
                break
            category = row.get("category")
            ref = row.get("ref")
            due_since = row.get("due_since")
            if not _due_state_token(category):
                break
            if (
                not isinstance(ref, str)
                or not ref.startswith(_ADVISORY_REF_SCHEME)
                or len(ref) > _MAX_DUE_STATE_REF_CHARS
            ):
                break
            if not _iso_date(due_since):
                break
            rows.append({"category": category, "ref": ref, "due_since": due_since})
        else:
            hint = value.get("_vault")
            return (
                {
                    "total": total,
                    "categories": {str(name): categories[name] for name in categories},
                    "top": rows,
                },
                str(hint) if isinstance(hint, str) else "",
            )
    return None, ""


def _admit_due_state(block: dict[str, Any], vault_hint: str) -> bool:
    """Decide, at the point of DELIVERY, whether this response carries the block.

    This is the one choke point every mutating command reaches, and it is called
    once per command invocation -- so "a multi-write product command emits at most
    once, at its terminal" (D9) holds by construction rather than by each writer
    remembering to behave. It is also the first moment the detail level is known,
    which matters: deciding at production burnt the session's single emission on
    `legacy` responses that strip the block and on blocks the validation above
    drops, leaving the caller told nothing and the next response silent.

    Fails CLOSED. If the governor cannot be consulted, the safe direction for an
    anti-nagging mechanism is silence -- the items are still on the review surface.
    """
    try:
        from . import due_state as due_state_module

        vault = vault_hint or None
        if not due_state_module.would_emit(block, vault_root=vault):
            return False
        due_state_module.mark_emitted(block, vault_root=vault)
        return True
    except Exception:  # noqa: BLE001 -- an ungovernable advisory is not delivered
        log.debug("due-state emission governor unavailable; dropping", exc_info=True)
        return False


def _due_state_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_DUE_STATE_TOKEN_CHARS
        and all(character.isalnum() or character == "_" for character in value)
    )


def _iso_date(value: Any) -> bool:
    """A strict `YYYY-MM-DD`, checked without importing a date parser into a pure module."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    year, sep_one, rest = value.partition("-")
    month, sep_two, day = rest.partition("-")
    return (
        sep_one == "-"
        and sep_two == "-"
        and len(year) == 4
        and len(month) == 2
        and len(day) == 2
        and year.isdigit()
        and month.isdigit()
        and day.isdigit()
        and 1 <= int(month) <= 12
        and 1 <= int(day) <= 31
    )


def _without_advisory_due_state(result: Any) -> Any:
    """Strip the due-state block from the leaf the legacy detail and diagnostics see.

    The legacy detail returns the leaf verbatim, so leaving the block there would
    put an advisory on exactly the response shape the spec says omits it. Mirrors
    `_without_graph_rebuild_handoff`: the leaf attaches, the terminal decides who
    sees it.
    """
    if not isinstance(result, Mapping):
        return result
    changed = False
    stripped: dict[str, Any] = {}
    for key, value in result.items():
        if key == "due_state":
            changed = True
            continue
        if (
            key in ("creation", "semantic", "source")
            and isinstance(value, Mapping)
            and ("due_state" in value)
        ):
            stripped[key] = {inner: value[inner] for inner in value if inner != "due_state"}
            changed = True
            continue
        stripped[key] = value
    return stripped if changed else result


def _bounded_tokens(value: Any, limit: int) -> bool:
    return (
        isinstance(value, (list, tuple))
        and 0 < len(value) <= limit
        and all(
            isinstance(item, str) and 0 < len(item) <= _MAX_STRUCTURE_TOKEN_CHARS for item in value
        )
    )


def receipt_leaf_projection(leaf_result: Any) -> dict[str, Any]:
    """Portable graph receipts never retain collection paths or content metadata."""
    # Collection receipts are public API results, not portable graph protocol
    # authority: both shapes include affected paths.  A local exact retry can
    # reconstruct only the envelope after a SQLite cut, which is preferable
    # to copying user paths into a synced hidden file.
    del leaf_result
    return {}


def committed_terminal(
    leaf_result: Any,
    *,
    request_id: str,
    receipt_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Own one canonical successful result before receipt persistence."""
    terminal: dict[str, Any] = {
        "_terminal": _TERMINAL_MARKER,
        "version": _TERMINAL_VERSION,
        "ok": True,
        # `state` is the closed machine; `status` is retained verbatim for existing
        # consumers and always agrees with it.
        "state": "committed",
        "status": "committed",
        "terminal": True,
        "mutated": True,
    }
    terminal.update(_path_projection(leaf_result))
    operation_id = _operation_id(leaf_result)
    if operation_id is not None:
        terminal["operation_id"] = operation_id
    terminal.update(
        request_id=request_id,
        receipt_id=receipt_id,
        warnings_count=_warning_count(leaf_result),
        leaf_result=leaf_result,
    )
    if idempotency_key is not None:
        terminal["idempotency_key"] = idempotency_key
    return terminal


def with_fast_acknowledgement(
    terminal: Mapping[str, Any],
    *,
    derived_sync: str,
    derived_sync_components: Sequence[str] = (),
    component_diagnostics: Sequence[Mapping[str, Any]] = (),
    advisory_sync: str,
    advisory_result_ref: str | None = None,
    graph_sync: str | None = None,
) -> dict[str, Any]:
    """Freeze one bounded derived-custody snapshot into a committed terminal."""
    if (
        terminal.get("_terminal") != _TERMINAL_MARKER
        or terminal.get("version") != _TERMINAL_VERSION
        or terminal.get("state") != "committed"
    ):
        raise ValueError("fast acknowledgement requires a committed mutation terminal")
    if derived_sync not in DERIVED_SYNC_OUTCOMES:
        raise ValueError("unknown derived acknowledgement outcome")
    if advisory_sync not in ADVISORY_SYNC_OUTCOMES:
        raise ValueError("unknown advisory acknowledgement outcome")
    if graph_sync is not None and graph_sync not in GRAPH_SYNC_OUTCOMES:
        raise ValueError("unknown graph acknowledgement outcome")

    components = tuple(sorted(set(derived_sync_components)))
    if len(components) > len(_DERIVED_COMPONENT_NAMES) or any(
        component not in _DERIVED_COMPONENT_NAMES for component in components
    ):
        raise ValueError("derived acknowledgement components are invalid")
    if derived_sync == "completed" and components:
        raise ValueError("completed derived acknowledgement cannot name unfinished work")

    diagnostics: list[dict[str, str | None]] = []
    for item in component_diagnostics:
        if len(diagnostics) >= _MAX_DERIVED_DIAGNOSTICS:
            break
        component = item.get("component")
        state = item.get("state")
        code = item.get("code")
        if (
            not isinstance(component, str)
            or not component
            or len(component) > 32
            or not isinstance(state, str)
            or not state
            or len(state) > 32
            or (code is not None and (not isinstance(code, str) or len(code) > 64))
        ):
            raise ValueError("derived acknowledgement diagnostic is invalid")
        diagnostics.append({"component": component, "state": state, "code": code})
    diagnostics.sort(key=lambda item: item["component"] or "")

    applicable_advisory = advisory_sync != "not_required"
    if applicable_advisory != (
        isinstance(advisory_result_ref, str)
        and advisory_result_ref.startswith("exomem://write-advisory-result/")
        and len(advisory_result_ref) <= 256
    ):
        raise ValueError("applicable advisory acknowledgement requires one stable result ref")

    frozen = dict(terminal)
    frozen["derived_sync"] = derived_sync
    if components:
        frozen["derived_sync_components"] = list(components)
    else:
        frozen.pop("derived_sync_components", None)
    if derived_sync == "failed":
        frozen["derived_sync_code"], frozen["derived_sync_next_action"] = _DERIVED_FAILURE
    else:
        frozen.pop("derived_sync_code", None)
        frozen.pop("derived_sync_next_action", None)
    frozen["advisory_sync"] = advisory_sync
    if advisory_result_ref is not None:
        frozen["advisory_result_ref"] = advisory_result_ref
    else:
        frozen.pop("advisory_result_ref", None)
    if advisory_sync == "failed":
        frozen["advisory_sync_code"], frozen["advisory_sync_next_action"] = (
            _ADVISORY_FAILURE
        )
    else:
        frozen.pop("advisory_sync_code", None)
        frozen.pop("advisory_sync_next_action", None)
    if graph_sync is not None:
        frozen["graph_sync"] = graph_sync
    frozen["_derived_diagnostics"] = diagnostics
    return frozen


def replayed_terminal(
    leaf_result: Any,
    *,
    request_id: str,
    receipt_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Present a verified governed no-op replay without fabricating a commit."""
    lifecycle_replay = (
        isinstance(leaf_result, Mapping)
        and leaf_result.get("receipt_version") == _LIFECYCLE_RECEIPT_VERSION
        and leaf_result.get("outcome") == "committed"
    )
    if not (
        valid_collection_receipt(leaf_result) or valid_structured_files_receipt(leaf_result)
    ) or (leaf_result.get("outcome") != "replayed" and not lifecycle_replay):
        raise ValueError("replayed terminal requires a valid governed mutation receipt")

    terminal: dict[str, Any] = {
        "_terminal": _TERMINAL_MARKER,
        "version": _TERMINAL_VERSION,
        "ok": True,
        "status": "replayed",
        "mutated": False,
    }
    terminal.update(_path_projection(leaf_result))
    terminal.update(
        request_id=request_id,
        receipt_id=receipt_id,
        warnings_count=_warning_count(leaf_result),
        leaf_result=leaf_result,
    )
    if idempotency_key is not None:
        terminal["idempotency_key"] = idempotency_key
    return terminal


def _is_guarded_precommit(result: Any) -> bool:
    """Whether a non-committing leaf result is a guarded write awaiting review.

    Deliberately narrow. Most non-committing leaves are ordinary reads or unrelated
    shapes and MUST pass through untouched; only a validated draft that explicitly
    reports it did not mutate earns the nonterminal envelope.
    """
    if not isinstance(result, Mapping):
        return False
    if result.get("mutated") is not False:
        return False
    if _operation_id(result) is None:
        return False
    return any(key in result for key in ("committable_after_review", "draft_hash", "draft_token"))


def needs_review_terminal(leaf_result: Any) -> Any:
    """Own the NONTERMINAL half of a guarded write; pass anything else through.

    A precommit refusal is not a failure: the operation is mid-flight and the caller
    should complete the review step. Before this, validation returned a shape with no
    relationship to the eventual success envelope, so a client had nothing tying the
    two calls together and could reasonably read the first response as the outcome —
    which is exactly the misclassification observed on 2026-08-06.
    """
    if not _is_guarded_precommit(leaf_result):
        return leaf_result
    committable = bool(leaf_result.get("committable_after_review"))
    terminal: dict[str, Any] = {
        "_terminal": _TERMINAL_MARKER,
        "version": _TERMINAL_VERSION,
        "ok": True,
        "state": "needs_review",
        "terminal": False,
        "mutated": False,
        "next_action": (
            "Re-issue the same call with the returned draft identity and an explicit "
            "relation disposition to commit it. This response is not a failure."
            if committable
            else "Resolve the reported blocking findings, then re-validate. This "
            "response is not a failure."
        ),
    }
    operation_id = _operation_id(leaf_result)
    if operation_id is not None:
        terminal["operation_id"] = operation_id
    terminal.update(
        warnings_count=_warning_count(leaf_result),
        leaf_result=leaf_result,
    )
    return terminal


def split_response_detail(
    kwargs: Mapping[str, Any],
    *,
    default: ResponseDetail = "compact",
) -> tuple[dict[str, Any], ResponseDetail]:
    """Remove presentation detail from an owned invocation-payload copy."""
    payload = dict(kwargs)
    detail = payload.pop("response_detail", default)
    if not isinstance(detail, str) or detail not in _RESPONSE_DETAILS:
        raise ValueError("response_detail must be one of: compact, full, legacy")
    return payload, cast(ResponseDetail, detail)


def project_terminal(result: Any, detail: ResponseDetail = "compact") -> Any:
    """Project a canonical terminal, preserving unversioned legacy results."""
    if not isinstance(detail, str) or detail not in _RESPONSE_DETAILS:
        raise ValueError("response_detail must be one of: compact, full, legacy")
    if (
        not isinstance(result, Mapping)
        or result.get("_terminal") != _TERMINAL_MARKER
        or result.get("version") != _TERMINAL_VERSION
        or "leaf_result" not in result
    ):
        return result
    raw_leaf = result["leaf_result"]
    due_state, due_state_vault = _due_state_projection(raw_leaf)
    leaf = _without_advisory_due_state(_without_graph_rebuild_handoff(raw_leaf))
    if detail == "legacy":
        return leaf
    compact = {key: result[key] for key in _ENVELOPE_KEYS if key in result}
    if "idempotency_key" in result:
        compact["idempotency_key"] = result["idempotency_key"]
    if result.get("derived_sync") in DERIVED_SYNC_OUTCOMES:
        compact["derived_sync"] = result["derived_sync"]
        components = result.get("derived_sync_components")
        if isinstance(components, (list, tuple)):
            bounded_components = sorted(
                {
                    component
                    for component in components
                    if isinstance(component, str)
                    and component in _DERIVED_COMPONENT_NAMES
                }
            )
            if bounded_components:
                compact["derived_sync_components"] = bounded_components
        if result["derived_sync"] == "failed":
            if result.get("derived_sync_code") == _DERIVED_FAILURE[0]:
                compact["derived_sync_code"] = _DERIVED_FAILURE[0]
            if result.get("derived_sync_next_action") == _DERIVED_FAILURE[1]:
                compact["derived_sync_next_action"] = _DERIVED_FAILURE[1]
    if result.get("advisory_sync") in ADVISORY_SYNC_OUTCOMES:
        compact["advisory_sync"] = result["advisory_sync"]
        advisory_ref = result.get("advisory_result_ref")
        if (
            result["advisory_sync"] != "not_required"
            and isinstance(advisory_ref, str)
            and advisory_ref.startswith("exomem://write-advisory-result/")
            and len(advisory_ref) <= 256
        ):
            compact["advisory_result_ref"] = advisory_ref
        if result["advisory_sync"] == "failed":
            if result.get("advisory_sync_code") == _ADVISORY_FAILURE[0]:
                compact["advisory_sync_code"] = _ADVISORY_FAILURE[0]
            if result.get("advisory_sync_next_action") == _ADVISORY_FAILURE[1]:
                compact["advisory_sync_next_action"] = _ADVISORY_FAILURE[1]
    if _is_record_receipt(leaf):
        if leaf["receipt_version"] == _LIFECYCLE_RECEIPT_VERSION:
            compact.update(
                {
                    "_record_receipt": leaf["_record_receipt"],
                    "receipt_version": leaf["receipt_version"],
                }
            )
        compact.update({key: leaf[key] for key in _RECORD_RECEIPT_FIELDS if key in leaf})
    elif valid_planning_receipt(leaf):
        compact.update({key: leaf[key] for key in _PLAN_RECEIPT_FIELDS if key in leaf})
    artifact_receipt = _artifact_receipt_projection(leaf)
    compact.update(artifact_receipt)
    # `pending` (#576) is the fourth outcome: canonical bytes committed, the
    # registered derived-graph rebuild has not converged yet. It has to survive
    # into `compact` -- the default detail -- or a bounded write would be
    # indistinguishable from one whose graph is current, which is the exact
    # dishonesty the bound is not allowed to introduce.
    graph_result = result if result.get("graph_sync") in GRAPH_SYNC_OUTCOMES else leaf
    if isinstance(graph_result, Mapping) and graph_result.get("graph_sync") in GRAPH_SYNC_OUTCOMES:
        compact["graph_sync"] = graph_result["graph_sync"]
        for key in (
            "graph_sync_code",
            "graph_sync_checkpoint",
            "graph_sync_remediation",
        ):
            value = graph_result.get(key)
            if isinstance(value, str):
                compact[key] = value
    if (
        isinstance(leaf, Mapping)
        and all(
        type(leaf.get(key)) is bool
        for key in ("graph_rebuild_requested", "graph_rebuild_applicable")
        )
        and leaf.get("graph_rebuild_status")
        in {
        "not_requested",
        "not_applicable",
        "would_quarantine",
        "quarantined",
        "cleared",
        "retained",
        "failed",
        }
    ):
        compact.update(
            {
                key: leaf[key]
                for key in (
                    "graph_rebuild_requested",
                    "graph_rebuild_applicable",
                    "graph_rebuild_status",
                )
            }
        )
        quarantine_id = leaf.get("graph_quarantine_id")
        if isinstance(quarantine_id, str) and len(quarantine_id) == 24:
            compact["graph_quarantine_id"] = quarantine_id
        warning = leaf.get("graph_rebuild_warning")
        if isinstance(warning, str) and 0 < len(warning) <= 128:
            compact["graph_rebuild_warning"] = warning
    structure_suggestion = _structure_suggestion_projection(leaf)
    if structure_suggestion is not None:
        compact["structure_suggestion"] = structure_suggestion
    if due_state is not None and _admit_due_state(due_state, due_state_vault):
        compact["due_state"] = due_state
    compact["warnings_count"] = result["warnings_count"]
    # Projected from the leaf, never from the receipt. Receipt recovery replaces
    # `leaf_result` with `{}` on purpose (the portable receipt must not retain
    # leaf paths or content) while `warnings_count` survives in the receipt
    # fields, so that path keeps reporting a count with no texts — by design.
    warnings = _warning_texts(leaf, has_artifact_receipt=bool(artifact_receipt))
    if warnings:
        compact["warnings"] = warnings
    if detail == "full":
        derived_diagnostics = result.get("_derived_diagnostics")
        if isinstance(derived_diagnostics, (list, tuple)):
            compact["diagnostics"] = {
                "leaf_result": leaf,
                "derived_components": list(
                    derived_diagnostics[:_MAX_DERIVED_DIAGNOSTICS]
                ),
            }
        else:
            compact["diagnostics"] = leaf
    return compact


def valid_record_receipt(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if (
        type(value.get("receipt_version")) is int
        and value.get("receipt_version") == _LIFECYCLE_RECEIPT_VERSION
    ):
        return _valid_lifecycle_record_receipt(value)
    operation = value.get("operation")
    if (
        value.get("_record_receipt") != _RECORD_RECEIPT_MARKER
        or type(value.get("receipt_version")) is not int
        or value.get("receipt_version") != _RECORD_RECEIPT_VERSION
        or operation not in {"create", "append", "update"}
        or not _normalized_uuid(value.get("collection_id"))
        or not isinstance(value.get("affected_paths"), list)
        or len(value["affected_paths"]) > 16
        or not all(
            isinstance(path, str) and 0 < len(path) <= 1024 for path in value["affected_paths"]
        )
        or value.get("outcome") not in {"committed", "replayed"}
    ):
        return False
    outcome = value.get("outcome")
    correlation = value.get("audit_correlation")
    if operation == "create":
        if (
            value.get("item_key") is not None
            or outcome != "committed"
            or value.get("before_item_hash") is not None
            or (
                value.get("after_item_hash") is not None and not _hash(value.get("after_item_hash"))
            )
            or value.get("before_container_hash") is not None
            or value.get("payload_hash") is not None
            or not _hash(value.get("after_container_hash"))
        ):
            return False
    elif not _normalized_uuid(value.get("item_key")):
        return False
    for name in (
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "payload_hash",
    ):
        hash_value = value.get(name)
        if hash_value is not None and (
            not isinstance(hash_value, str)
            or len(hash_value) != 64
            or any(character not in "0123456789abcdef" for character in hash_value)
        ):
            return False
    if not (
        isinstance(correlation, str)
        and len(correlation) == 24
        and all(character in "0123456789abcdef" for character in correlation)
    ):
        return False
    if operation == "append" and outcome == "committed":
        return (
            value.get("before_item_hash") is None
            and _hash(value.get("after_item_hash"))
            and _hash(value.get("before_container_hash"))
            and _hash(value.get("after_container_hash"))
            and _hash(value.get("payload_hash"))
        )
    if operation == "update" and outcome == "committed":
        return (
            _hash(value.get("before_item_hash"))
            and _hash(value.get("after_item_hash"))
            and _hash(value.get("before_container_hash"))
            and _hash(value.get("after_container_hash"))
            and value.get("payload_hash") is None
        )
    if operation == "append" and outcome == "replayed":
        return (
            _hash(value.get("before_item_hash"))
            and _hash(value.get("after_item_hash"))
            and _hash(value.get("before_container_hash"))
            and _hash(value.get("after_container_hash"))
            and _hash(value.get("payload_hash"))
        )
    if operation == "create":
        return True
    return False


def _valid_lifecycle_record_receipt(value: Mapping[str, Any]) -> bool:
    if set(value) != {
        "_record_receipt",
        "receipt_version",
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    }:
        return False
    if not (
        value.get("_record_receipt") == _RECORD_RECEIPT_MARKER
        and type(value.get("receipt_version")) is int
        and value.get("receipt_version") == _LIFECYCLE_RECEIPT_VERSION
        and value.get("operation") in {"revise", "rebaseline"}
        and _normalized_uuid(value.get("collection_id"))
        and value.get("item_key") is None
        and value.get("before_item_hash") is None
        and value.get("after_item_hash") is None
        and all(
            _hash(value.get(name))
            for name in (
                "before_manifest_hash",
                "after_manifest_hash",
                "before_container_hash",
                "after_container_hash",
                "payload_hash",
            )
        )
    ):
        return False
    paths = value.get("affected_paths")
    correlation = value.get("audit_correlation")
    codes = value.get("acknowledged_gap_codes")
    if not (
        isinstance(paths, list)
        and len(paths) == 1
        and isinstance(paths[0], str)
        and 0 < len(paths[0]) <= 1024
        and value.get("outcome") == "committed"
        and isinstance(correlation, str)
        and len(correlation) == 24
        and all(character in "0123456789abcdef" for character in correlation)
        and type(value.get("continuity")) is bool
        and isinstance(codes, list)
        and all(type(code) is str and code and len(code.encode("utf-8")) <= 256 for code in codes)
        and type(value.get("minimum_reader_version")) is int
        and value.get("minimum_reader_version") == 2
    ):
        return False
    if value["operation"] == "revise":
        return (
            value["continuity"] is True
            and codes == []
            and value.get("gap_fingerprint") is None
            and value.get("checkpoint_snapshot_hash") is None
        )
    return (
        value["continuity"] is False
        and codes == sorted(set(codes))
        and bool(codes)
        and _hash(value.get("gap_fingerprint"))
        and _hash(value.get("checkpoint_snapshot_hash"))
    )


def valid_planning_receipt(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("receipt_version") == _LIFECYCLE_RECEIPT_VERSION:
        return _valid_lifecycle_planning_receipt(value)
    operation = value.get("operation")
    if (
        value.get("_plan_receipt") != _PLAN_RECEIPT_MARKER
        or value.get("receipt_version") != _RECORD_RECEIPT_VERSION
        or operation not in {"create", "add", "update", "triage"}
        or not _normalized_uuid(value.get("collection_id"))
        or not isinstance(value.get("affected_paths"), list)
        or len(value["affected_paths"]) > 16
        or not all(
            isinstance(path, str) and 0 < len(path) <= 1024 for path in value["affected_paths"]
        )
        or value.get("outcome") not in {"committed", "replayed"}
    ):
        return False
    if operation == "create":
        if value.get("plan_id") is not None or value.get("outcome") != "committed":
            return False
    elif not _normalized_uuid(value.get("plan_id")):
        return False
    for name in _PLAN_RECEIPT_FIELDS[3:7]:
        hash_value = value.get(name)
        if hash_value is not None and not _hash(hash_value):
            return False
    correlation = value.get("audit_correlation")
    if not (
        isinstance(correlation, str)
        and len(correlation) == 24
        and all(character in "0123456789abcdef" for character in correlation)
    ):
        return False
    if operation == "create":
        return (
            value.get("before_item_hash") is None
            and value.get("before_container_hash") is None
            and value.get("payload_hash") is None
            and _hash(value.get("after_container_hash"))
        )
    if operation == "add":
        return (
            (
            _hash(value.get("before_item_hash"))
            if value.get("outcome") == "replayed"
            else value.get("before_item_hash") is None
            )
            and _hash(value.get("after_item_hash"))
            and _hash(value.get("before_container_hash"))
            and _hash(value.get("after_container_hash"))
            and _hash(value.get("payload_hash"))
        )
    return (
        value.get("outcome") == "committed"
        and _hash(value.get("before_item_hash"))
        and _hash(value.get("after_item_hash"))
        and _hash(value.get("before_container_hash"))
        and _hash(value.get("after_container_hash"))
        and value.get("payload_hash") is None
    )


def _valid_lifecycle_planning_receipt(value: Mapping[str, Any]) -> bool:
    expected = {
        "_plan_receipt",
        "receipt_version",
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    }
    if set(value) != expected:
        return False
    if not (
        value.get("_plan_receipt") == _PLAN_RECEIPT_MARKER
        and value.get("receipt_version") == _LIFECYCLE_RECEIPT_VERSION
        and value.get("operation") in {"revise", "rebaseline"}
        and _normalized_uuid(value.get("collection_id"))
        and value.get("item_key") is None
        and value.get("before_item_hash") is None
        and value.get("after_item_hash") is None
        and all(
            _hash(value.get(name))
            for name in (
                "before_manifest_hash",
                "after_manifest_hash",
                "before_container_hash",
                "after_container_hash",
                "payload_hash",
            )
        )
    ):
        return False
    paths = value.get("affected_paths")
    correlation = value.get("audit_correlation")
    codes = value.get("acknowledged_gap_codes")
    if not (
        isinstance(paths, list)
        and 1 <= len(paths) <= 513
        and all(isinstance(path, str) and 0 < len(path.encode("utf-8")) <= 1024 for path in paths)
        and value.get("outcome") == "committed"
        and isinstance(correlation, str)
        and len(correlation) == 24
        and all(character in "0123456789abcdef" for character in correlation)
        and type(value.get("continuity")) is bool
        and isinstance(codes, list)
        and all(type(code) is str and code and len(code.encode("utf-8")) <= 256 for code in codes)
        and value.get("minimum_reader_version") == 2
    ):
        return False
    if value["operation"] == "revise":
        return (
            value["continuity"] is True
            and codes == []
            and value.get("gap_fingerprint") is None
            and value.get("checkpoint_snapshot_hash") is None
        )
    return (
        value["continuity"] is False
        and codes == sorted(set(codes))
        and bool(codes)
        and _hash(value.get("gap_fingerprint"))
        and _hash(value.get("checkpoint_snapshot_hash"))
    )


def valid_collection_receipt(value: Any) -> bool:
    return valid_record_receipt(value) or valid_planning_receipt(value)


def valid_structured_files_receipt(value: Any) -> bool:
    """Whether *value* is one bounded terminal structured-file receipt."""
    if not isinstance(value, Mapping) or set(value) != {
        "_structured_files_receipt",
        "receipt_version",
        "operation",
        "collection_id",
        "manifest_path",
        "plan_id",
        "source_snapshot",
        "outcome",
        "rationale",
        "inverse",
    }:
        return False
    inverse = value.get("inverse")
    if not (
        value.get("_structured_files_receipt") == "exomem.structured-files-migration"
        and value.get("receipt_version") == 1
        and value.get("operation") == "structured-files"
        and _normalized_uuid(value.get("collection_id"))
        and isinstance(value.get("manifest_path"), str)
        and 0 < len(value["manifest_path"].encode("utf-8")) <= 1024
        and _hash(value.get("plan_id"))
        and _hash(value.get("source_snapshot"))
        and value.get("outcome") in {"committed", "replayed"}
        and isinstance(value.get("rationale"), str)
        and 0 < len(value["rationale"].encode("utf-8")) <= 512
        and isinstance(inverse, list)
        and len(inverse) <= 2048
    ):
        return False
    return all(
        isinstance(entry, Mapping)
        and set(entry) == {"before_path", "after_path", "before_hash", "after_hash"}
        and all(
            isinstance(entry.get(name), str) and 0 < len(entry[name].encode("utf-8")) <= 1024
            for name in ("before_path", "after_path")
        )
        and _hash(entry.get("before_hash"))
        and _hash(entry.get("after_hash"))
        for entry in inverse
    )


_is_record_receipt = valid_record_receipt


def _hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _normalized_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
