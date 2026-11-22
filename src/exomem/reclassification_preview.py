"""Serve small Source previews and page complete background approval reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Any

from . import (
    auth_sessions,
    capabilities,
    freshness,
    maintenance_jobs,
    query_data,
    recall_policy,
    reclassify_source,
    request_budget,
)
from .cli_ops import OpError
from .governance import connector_boundary
from .governance.principal import (
    RequestPrincipal,
    effective_principal,
    invocation_scope,
    request_scope,
)

# Bound work before reading candidates' bodies. Results also meet the normal response-byte bound.
_INLINE_CANDIDATES = 256
_JOB_REPORT: ContextVar[dict[str, Any] | None] = ContextVar("source_reclassification_job_report", default=None)


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _not_found() -> OpError:
    return OpError(
        "NOT_FOUND", "The paged preview is unavailable for this request.",
        "Request a new preview without a run identifier.",
    )


def _authority_binding(root: Path, who: RequestPrincipal) -> str:
    if reclassify_source.managed_preview_stop(root, who) is None:
        raise _not_found()
    policy = recall_policy.recall_policy_identity(root)
    # Page delivery follows the same caller and current authority across corpus changes.
    return _digest((policy, connector_boundary.cache_identity(root, who)[-1], asdict(who)))


def _snapshot_key(root: Path) -> str | None:
    generation = freshness.preview_reference_generation(root)
    if generation is None or generation[1] is None or generation[3] is not None:
        return None
    return _digest(generation)


def _entries(report: Mapping[str, Any]) -> Iterator[tuple[str, str, str | None]]:
    referrers = report["referrers"]
    for kind in ("rewritten", "pointer_repointed"):
        for path in referrers[kind]:
            yield kind, path, None
    for form, paths in referrers["append_only_unchanged"].items():
        for path in paths:
            yield "append_only_unchanged", path, form
    for refusal in report["refusals"]:
        yield "refusal", refusal["path"], _bytes(refusal).decode()


def _build(
    root: Path, args: Mapping[str, Any], who: RequestPrincipal,
    surface: capabilities.ActiveSurfaceDescriptor | None, binding: str, snapshot_key: str, stop: Event,
) -> tuple[dict[str, Any], Iterator[tuple[str, str, str | None]]]:
    from .command_surface import resolve_command
    from .commands import PRODUCT_COMMANDS
    from .writer_lease import invoke_command

    command = resolve_command(
        "manage_memory_file", (surface.contributing_commands if surface is not None else ()) or PRODUCT_COMMANDS,
    )
    if command is None:
        raise _not_found()
    budget = request_budget.RequestBudget(seconds=request_budget.budget_seconds())
    check = request_budget.ReadCheckpoint(budget, bound_disclosure=True)
    original: dict[str, Any] = {}
    token = _JOB_REPORT.set(original)
    try:
        with (
            request_scope(who), invocation_scope(), request_budget.checking(check),
            capabilities.active_surface(surface) if surface is not None else nullcontext(),
        ):
            if (auth_sessions.origin_revalidates_in_background(who)
                    and auth_sessions.origin_login_state_sync(who) is not auth_sessions.OriginLogin.VALID):
                raise _not_found()
            if stop.is_set() or _authority_binding(root, who) != binding:
                raise _not_found()
            if _snapshot_key(root) != snapshot_key:
                raise reclassify_source.preview_warming()
            result = invoke_command(command, root, operation="propose-reclassification", **args)
            if (not original or _digest({key: result.get(key) for key in original["keys"]}) != original["digest"]
                    or stop.is_set() or _authority_binding(root, who) != binding):
                raise _not_found()
            if _snapshot_key(root) != snapshot_key:
                raise reclassify_source.preview_warming()
            if (auth_sessions.origin_revalidates_in_background(who)
                    and auth_sessions.origin_login_state_sync(who) is not auth_sessions.OriginLogin.VALID):
                raise _not_found()
            check()
    except request_budget.ReadBudgetExhausted:
        raise reclassify_source.preview_warming() from None
    finally:
        _JOB_REPORT.reset(token)
    summary = {key: value for key, value in result.items() if key not in {"referrers", "refusals"}}
    return summary, _entries(result)


def report(
    root: Path, path: str, *, source_kind: str | None, domain: str | None,
    preview_run_id: str | None = None, cursor: str | None = None,
) -> dict[str, Any]:
    original = _JOB_REPORT.get()
    if original is not None:
        result = reclassify_source.propose(root, path, source_kind=source_kind, domain=domain, paged=True).as_dict()
        original.update(keys=tuple(result), digest=_digest(result))
        return result

    check = request_budget.current_checkpoint()
    managed = check is not None and check.bound_disclosure
    if cursor is not None and preview_run_id is None:
        raise OpError("INVALID_ARGUMENTS", "A report cursor requires its preview run identifier.")
    if preview_run_id is None:
        try:
            result = reclassify_source.propose(
                root, path, source_kind=source_kind, domain=domain,
                candidate_limit=_INLINE_CANDIDATES if managed else None,
            ).as_dict()
            if not managed or len(_bytes(result)) <= query_data.MAX_RESPONSE_BYTES:
                return result
        except reclassify_source.PreviewNeedsJob:
            pass
    if not managed:
        raise _not_found()
    who = effective_principal()
    binding = _authority_binding(root, who)
    snapshot_key = _snapshot_key(root)
    args = {"path": path, "source_kind": source_kind, "domain": domain}
    request_key = _digest(args)
    follow = {"operation": "propose-reclassification",
              **{key: value for key, value in args.items() if value is not None}}
    if preview_run_id is not None:
        result = maintenance_jobs.source_preview_status(
            root, binding=binding, request_key=request_key, snapshot_key=snapshot_key,
            run_id=preview_run_id, cursor=cursor,
        )
        if result.get("error_code") == "RECLASSIFICATION_PREVIEW_UNAVAILABLE":
            raise reclassify_source.preview_unavailable()
        if result.get("error_code") == "RECLASSIFICATION_PREVIEW_WARMING":
            result["retry_after_ms"] = reclassify_source.preview_warming().details["retry_after_ms"]
            result["next"] = capabilities.active_call_route("manage_memory_file", follow)
    else:
        if snapshot_key is None:
            raise reclassify_source.preview_warming()
        surface = capabilities.current_active_surface()
        result = maintenance_jobs.start_source_preview(
            root, binding=binding, request_key=request_key, snapshot_key=snapshot_key,
            build=lambda stop: _build(root, args, who, surface, binding, snapshot_key, stop),
        )
    if _authority_binding(root, who) != binding:
        raise _not_found()
    if preview_run_id is None or result.get("state") == maintenance_jobs.RUNNING or result.get("next_cursor") is not None:
        follow["preview_run_id"] = result["run_id"]
        if result.get("next_cursor") is not None:
            follow["cursor"] = result["next_cursor"]
        result["next"] = capabilities.active_call_route("manage_memory_file", follow)
    return result
