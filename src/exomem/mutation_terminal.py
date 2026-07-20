"""Pure construction and presentation of committed mutation terminals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

ResponseDetail = Literal["compact", "full", "legacy"]

_TERMINAL_MARKER = "exomem.mutation-terminal"
_TERMINAL_VERSION = 1
_RESPONSE_DETAILS = frozenset({"compact", "full", "legacy"})


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
        "status": "committed",
        "mutated": True,
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


def split_response_detail(
    kwargs: Mapping[str, Any],
) -> tuple[dict[str, Any], ResponseDetail]:
    """Remove presentation detail from an owned invocation-payload copy."""
    payload = dict(kwargs)
    detail = payload.pop("response_detail", "compact")
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
    compact = {
        key: result[key]
        for key in ("ok", "status", "mutated", "path", "paths", "request_id", "receipt_id")
        if key in result
    }
    if "idempotency_key" in result:
        compact["idempotency_key"] = result["idempotency_key"]
    compact["warnings_count"] = result["warnings_count"]
    if detail == "full":
        compact["diagnostics"] = result["leaf_result"]
    return compact
