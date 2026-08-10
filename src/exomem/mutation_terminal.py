"""Pure construction and presentation of committed mutation terminals."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal, cast

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
    "before_container_hash",
    "after_container_hash",
    "affected_paths",
    "payload_hash",
    "outcome",
    "audit_correlation",
)
_RECORD_RECEIPT_MARKER = "exomem.records-mutation"
_RECORD_RECEIPT_VERSION = 1

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


def _path_projection(result: Any) -> dict[str, Any]:
    """Adapt only the small set of explicit mutation path shapes we own."""
    if not isinstance(result, Mapping):
        return {"paths": []}
    artifact_receipt = _artifact_receipt_projection(result)
    if artifact_receipt:
        paths = [
            item["stored_path"]
            for item in artifact_receipt["files"]
            if item["outcome"] == "stored"
        ]
        if len(paths) == 1:
            return {"path": paths[0]}
        if paths:
            return {"paths": paths}
    path = result.get("path")
    if isinstance(path, str):
        return {"path": path}
    affected_paths = result.get("affected_paths")
    if valid_record_receipt(result) and isinstance(affected_paths, (list, tuple)) and all(
        isinstance(item, str) for item in affected_paths
    ):
        return {"paths": list(affected_paths)}
    paths = result.get("paths")
    if isinstance(paths, (list, tuple)) and all(isinstance(item, str) for item in paths):
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
        return (allow_none and value is None) or (isinstance(value, str) and 0 < len(value) <= limit)

    def nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def sha256(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    if not isinstance(result, Mapping):
        return {}
    files = result.get("files")
    summary = result.get("summary")
    if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= 8 or not isinstance(summary, Mapping):
        return {}
    projected: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, Mapping) or not string(item.get("file_id"), limit=256):
            return {}
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
                return {}
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
        elif outcome == "failed" and string(item.get("code"), limit=64) and string(
            item.get("reason"), limit=300
        ):
            row = {key: item[key] for key in ("file_id", "outcome", "code", "reason")}
        else:
            return {}
        projected.append(row)
    stored = summary.get("stored")
    failed = summary.get("failed")
    if (
        not nonnegative_int(stored)
        or not nonnegative_int(failed)
        or stored + failed != len(projected)
        or stored != sum(item["outcome"] == "stored" for item in projected)
        or failed != sum(item["outcome"] == "failed" for item in projected)
    ):
        return {}
    return {"files": projected, "summary": {"stored": stored, "failed": failed}}


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


def replayed_terminal(
    leaf_result: Any,
    *,
    request_id: str,
    receipt_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Present a verified Records no-op replay without fabricating a commit."""
    if not valid_record_receipt(leaf_result) or leaf_result.get("outcome") != "replayed":
        raise ValueError("replayed terminal requires a valid replayed record receipt")

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
    if detail == "legacy":
        return result["leaf_result"]
    compact = {key: result[key] for key in _ENVELOPE_KEYS if key in result}
    if "idempotency_key" in result:
        compact["idempotency_key"] = result["idempotency_key"]
    leaf = result["leaf_result"]
    if _is_record_receipt(leaf):
        compact.update({key: leaf[key] for key in _RECORD_RECEIPT_FIELDS if key in leaf})
    compact.update(_artifact_receipt_projection(leaf))
    compact["warnings_count"] = result["warnings_count"]
    if detail == "full":
        compact["diagnostics"] = result["leaf_result"]
    return compact


def valid_record_receipt(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
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
