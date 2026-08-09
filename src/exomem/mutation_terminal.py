"""Pure construction and presentation of committed mutation terminals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

ResponseDetail = Literal["compact", "full", "legacy"]

_TERMINAL_MARKER = "exomem.mutation-terminal"
_TERMINAL_VERSION = 1
_RESPONSE_DETAILS = frozenset({"compact", "full", "legacy"})

#: The closed mutation state machine. A client may branch on exactly these values and
#: MUST NOT infer any other. `needs_review` is explicitly NONTERMINAL: it means the
#: guarded write is mid-flight and the caller should complete the review step, not that
#: anything failed. Conflating it with failure is the 2026-08-06 misclassification.
STATES = ("needs_review", "committed", "rejected", "retryable", "indeterminate")
TERMINAL_STATES = frozenset({"committed", "rejected"})

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


def _warning_count(result: Any) -> int:
    if not isinstance(result, Mapping):
        return 0
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


def _path_projection(result: Any) -> dict[str, Any]:
    """Adapt only the small set of explicit mutation path shapes we own."""
    if not isinstance(result, Mapping):
        return {"paths": []}
    path = result.get("path")
    if isinstance(path, str):
        return {"path": path}
    paths = result.get("paths")
    if isinstance(paths, (list, tuple)) and all(
        isinstance(item, str) for item in paths
    ):
        return {"paths": list(paths)}
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
                if isinstance(item, Mapping)
                and isinstance(item.get("source_path"), str)
            ]
            return {"paths": copied_paths}
    compile_plan = result.get("compile_plan")
    if isinstance(compile_plan, Mapping):
        copied_sources = compile_plan.get("copied_sources")
        if isinstance(copied_sources, (list, tuple)):
            copied_paths = [
                item["source_path"]
                for item in copied_sources
                if isinstance(item, Mapping)
                and isinstance(item.get("source_path"), str)
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
    return any(
        key in result
        for key in ("committable_after_review", "draft_hash", "draft_token")
    )


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
        raise ValueError(
            "response_detail must be one of: compact, full, legacy"
        )
    return payload, detail


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
    if detail == "legacy":
        return result["leaf_result"]
    compact = {key: result[key] for key in _ENVELOPE_KEYS if key in result}
    if "idempotency_key" in result:
        compact["idempotency_key"] = result["idempotency_key"]
    compact["warnings_count"] = result["warnings_count"]
    if detail == "full":
        compact["diagnostics"] = result["leaf_result"]
    return compact
