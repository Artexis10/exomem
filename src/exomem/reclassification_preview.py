"""Serve small Source previews and page complete background approval reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any

from . import (
    auth_sessions,
    capabilities,
    freshness,
    maintenance_jobs,
    move_file,
    path_mentions,
    query_data,
    recall_policy,
    reclassify_source,
    request_budget,
    reserved_paths,
    vault,
)
from .cli_ops import OpError
from .governance import connector_boundary, lifecycle
from .governance.principal import (
    RequestPrincipal,
    effective_principal,
    invocation_scope,
    request_scope,
)

# Bound work before reading candidates' bodies. Results also meet the normal response-byte bound.
_INLINE_CANDIDATES = 256
# A background build has its own budget, not a request's: a request budget is a latency bound for one
# answer, and a build is one bounded job that holds the single preview slot. On 10,000 governed referrers in
# one folder the build took about 29 s on a laptop (linear in referrers), so ten minutes bounds a build of
# about 200,000 referrers and frees the slot within that time. Cancellation still stops it per referrer.
_BUILD_BUDGET_SECONDS = 600.0
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


def _stale_report() -> OpError:
    return OpError(
        "RECLASSIFICATION_PREVIEW_STALE", "The complete report cannot be released from this snapshot.",
        "Request a new preview without a run identifier.",
    )


def _interrupted() -> OpError:
    return OpError(
        "RECLASSIFICATION_PREVIEW_INTERRUPTED", "The preview stopped before its report was complete.",
        "Request a new preview without a run identifier.",
    )


class RetainedReport(dict):
    """Refuse a whole receipt page if the terminal filter changes any reviewed value."""

    def __init__(
        self,
        result: Mapping[str, Any],
        source: str,
        held_evidence: Mapping[str, maintenance_jobs.HeldEvidence] | None = None,
    ) -> None:
        super().__init__(result)
        self._proof = _digest(result)
        self.exact_paths = frozenset({source, *(entry["path"] for entry in result.get("entries", ()))})
        self.held_evidence: Mapping[str, maintenance_jobs.HeldEvidence] = held_evidence or {}

    def require_complete_projection(self, result: Any) -> None:
        if _digest(result) != self._proof:
            raise _stale_report()


@dataclass(frozen=True)
class _Authority:
    key: str
    complete: bool
    marker_revision: str | None


@dataclass(frozen=True)
class _RetainedAdmission:
    authority: _Authority
    snapshot: str


_RETAINED_ADMISSION: ContextVar[_RetainedAdmission | None] = ContextVar(
    "source_reclassification_report_admission", default=None,
)


def _basis_key(root: Path, who: RequestPrincipal, basis: reclassify_source.PreviewReleaseBasis) -> str:
    return _digest((asdict(basis), connector_boundary.cache_identity(root, who)[-1], asdict(who)))


def _authority_binding(root: Path, who: RequestPrincipal) -> _Authority:
    if reclassify_source.managed_preview_stop(root, who) is None:
        raise _not_found()
    admission = reclassify_source.preview_release_authority(root, who)
    basis_key = _basis_key(root, who, admission.basis)
    key = _digest((basis_key, admission.erasure.fingerprint))
    return _Authority(key, admission.basis.complete, admission.erasure.marker_revision)


def _snapshot_key(root: Path) -> str | None:
    generation = freshness.preview_reference_generation(root)
    if generation is None or generation[1] is None or generation[3] is not None:
        return None
    return _digest(generation)


#: More changes than this since a build are not checked one by one: the report is rebuilt instead.
_MAX_CHECKED_CHANGES = 256


def _build_basis(root: Path) -> dict[str, Any]:
    """The private start point against which later changes are judged for this build's report."""
    return {
        "vault": list(freshness.consumer_checkpoint(root, "vault")),
        "recall": list(recall_policy.recall_policy_identity(root)),
    }


def _changes_leave_report(
    root: Path, basis: Mapping[str, Any] | None, source: str, in_report: Callable[[set[str]], bool | None],
) -> bool:
    """Whether every change since the build started leaves its report as it is.

    A change alters the report when it touches the Source or a page the build read (``in_report``),
    adds or removes a page with the Source's file stem (which decides whether a bare-name link
    resolves), or leaves a page that now links to the Source or names its path. The access policy and
    the caller's authority are bound separately. Anything this cannot prove, such as lost change
    history, counts as a change.
    """
    try:
        instance, generation, triple = basis["vault"]  # type: ignore[index]
        start = freshness.FreshnessCheckpoint(instance, generation, None if triple is None else tuple(triple))
        if tuple(basis["recall"]) != tuple(recall_policy.recall_policy_identity(root)):  # type: ignore[index]
            return False
    except (KeyError, TypeError, ValueError):
        return False
    current = freshness.preview_reference_generation(root)
    if current is None or current[0] != instance or current[3] is not None:
        return False
    delta = freshness.delta_since(root, "vault", start)
    if not delta.complete or len(delta.changed) + len(delta.deleted) > _MAX_CHECKED_CHANGES:
        return False
    changed = {freshness.registry_rel(root, path) for path in delta.changed}
    touched = changed | {freshness.registry_rel(root, path) for path in delta.deleted}
    if None in touched:
        return False
    if not touched:
        return True
    paired = move_file.paired_move(root, source, source)
    targets = [source, *([paired[0]] if paired else [])]
    stem = PurePosixPath(source).stem.casefold()
    if (set(targets) & touched or any(PurePosixPath(rel).stem.casefold() == stem for rel in touched)
            or in_report(touched) is not False):
        return False
    if not changed:
        return True
    inbound = vault.cached_inbound_wikilinks(root, source)
    if inbound is None or changed & {hit.path for hit in inbound}:
        return False
    mentions = path_mentions.find_pages(root, targets, current_published=True)
    return mentions.complete and not changed & set(mentions.pages)


def retained_report_identity(root: Path, who: RequestPrincipal) -> _RetainedAdmission | None:
    """Recheck exactly the release inputs that a retained summary or page can depend on."""
    try:
        authority = _authority_binding(root, who)
    except OpError:
        return None
    snapshot = _snapshot_key(root)
    if snapshot is None:
        raise request_budget.ReadBudgetExhausted()
    return _RetainedAdmission(authority, snapshot)


def retained_report_still_current(root: Path, who: RequestPrincipal, admission: _RetainedAdmission) -> bool:
    """Recheck the full release evidence after egress and disclosure recording."""
    markers = lifecycle.tombstone_marker_revision(root)
    return (
        markers is not None
        and markers == admission.authority.marker_revision
        and retained_report_identity(root, who) == admission
    )


@contextmanager
def retained_report_admission(identity: _RetainedAdmission) -> Iterator[None]:
    """Bind the authority the transport will recheck before releasing this report."""
    token = _RETAINED_ADMISSION.set(identity)
    try:
        yield
    finally:
        _RETAINED_ADMISSION.reset(token)


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
    surface: capabilities.ActiveSurfaceDescriptor | None, authority: _Authority, snapshot_key: str, stop: Event,
) -> tuple[
    dict[str, Any], Iterator[tuple[str, str, str | None]], dict[str, dict[str, Any]], dict[str, Any],
]:
    from .command_surface import resolve_command
    from .commands import PRODUCT_COMMANDS
    from .writer_lease import invoke_command

    command = resolve_command(
        "manage_memory_file", (surface.contributing_commands if surface is not None else ()) or PRODUCT_COMMANDS,
    )
    if command is None:
        raise _not_found()
    budget = request_budget.RequestBudget(seconds=_BUILD_BUDGET_SECONDS)
    check = request_budget.ReadCheckpoint(budget, bound_disclosure=True)
    original: dict[str, Any] = {}
    token = _JOB_REPORT.set(original)
    try:
        with (
            request_scope(who), invocation_scope(), request_budget.checking(check),
            capabilities.active_surface(surface) if surface is not None else nullcontext(),
            reclassify_source.held_evidence_sink() as held, reserved_paths.folder_listing_scope(),
        ):
            if (auth_sessions.origin_revalidates_in_background(who)
                    and auth_sessions.origin_login_state_sync(who) is not auth_sessions.OriginLogin.VALID):
                raise _not_found()
            if stop.is_set() or _authority_binding(root, who) != authority:
                raise _not_found()
            basis = _build_basis(root)
            if _snapshot_key(root) != snapshot_key:
                raise reclassify_source.preview_warming()
            result = invoke_command(command, root, operation="propose-reclassification", **args)
            if (not original or _digest({key: result.get(key) for key in original["keys"]}) != original["digest"]
                    or stop.is_set() or _authority_binding(root, who) != authority):
                raise _not_found()
            read = held.read_paths | {path for _, path, _ in _entries(result)}
            if _snapshot_key(root) != snapshot_key and not _changes_leave_report(
                    root, basis, args["path"], lambda touched: bool(touched & read)):
                raise reclassify_source.preview_warming()
            basis["reads"] = sorted(read)
            if (auth_sessions.origin_revalidates_in_background(who)
                    and auth_sessions.origin_login_state_sync(who) is not auth_sessions.OriginLogin.VALID):
                raise _not_found()
            check()
    except request_budget.ReadBudgetExhausted:
        raise reclassify_source.preview_warming() from None
    finally:
        _JOB_REPORT.reset(token)
    summary = {key: value for key, value in result.items() if key not in {"referrers", "refusals"}}
    return summary, _entries(result), held, basis


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
    authority = _authority_binding(root, who)
    if not authority.complete:
        raise _not_found()
    try:
        path, _ = reclassify_source._require_source(root, path)
    except reclassify_source.ReclassifyError:
        raise _not_found() from None
    snapshot_key = _snapshot_key(root)
    args = {"path": path, "source_kind": source_kind, "domain": domain}
    request_key = _digest(args)
    follow = {"operation": "propose-reclassification",
              **{key: value for key, value in args.items() if value is not None}}
    if preview_run_id is not None:
        if _RETAINED_ADMISSION.get() != _RetainedAdmission(authority, snapshot_key):
            raise _not_found()
        result = maintenance_jobs.source_preview_status(
            root, binding=authority.key, request_key=request_key, snapshot_key=snapshot_key,
            run_id=preview_run_id, cursor=cursor,
        )
        if (result.get("state") == maintenance_jobs.COMPLETED and result.get("snapshot_current") is not True):
            if snapshot_key is None or not _changes_leave_report(
                root, maintenance_jobs.source_preview_basis(root, preview_run_id), path,
                lambda touched: maintenance_jobs.source_preview_rows_among(root, preview_run_id, touched),
            ):
                raise _stale_report()
            result["snapshot_current"] = True
        if result.get("state") in {maintenance_jobs.CANCELLED, maintenance_jobs.INTERRUPTED}:
            raise _interrupted()
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
            root, binding=authority.key, request_key=request_key, snapshot_key=snapshot_key,
            build=lambda stop: _build(root, args, who, surface, authority, snapshot_key, stop),
        )
    if _authority_binding(root, who) != authority:
        raise _not_found()
    if _snapshot_key(root) != snapshot_key:
        raise _stale_report()
    if preview_run_id is None or result.get("state") == maintenance_jobs.RUNNING or result.get("next_cursor") is not None:
        follow["preview_run_id"] = result["run_id"]
        if result.get("next_cursor") is not None:
            follow["cursor"] = result["next_cursor"]
        result["next"] = capabilities.active_call_route("manage_memory_file", follow)
    if result.get("state") != maintenance_jobs.COMPLETED:
        return result
    held_evidence = maintenance_jobs.source_preview_held_evidence(
        root, result["run_id"],
        {path, *(entry["path"] for entry in result.get("entries", ()))},
    )
    return RetainedReport(result, path, held_evidence=held_evidence)
