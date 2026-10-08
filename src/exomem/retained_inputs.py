"""Current snapshot, release and exact selection for retained input references."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import lifecycle_statuses, memory_refs, reserved_paths, semantic_unit_read, semantic_units
from .get_page import GetError, GetResult, get_page, prepare_page_read
from .governance import authorization_custody, authorization_session_lifecycle, egress, store
from .governance.principal import RequestPrincipal, effective_principal
from .vault import PathGuard


@dataclass
class RetainedInputError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


@dataclass(frozen=True)
class RetainedInput:
    """Private read evidence, not permission to disclose or commit later."""

    canonical: str
    unit_ref: str | None
    page: GetResult
    guard: PathGuard | None
    unit: semantic_units.SemanticUnit | None = None
    released: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None


def _unavailable() -> RetainedInputError:
    return RetainedInputError("RETAINED_INPUT_UNAVAILABLE", "input is unavailable")


def _principal() -> RequestPrincipal:
    principal = effective_principal()
    audience = principal.audience_id
    if (
        not principal.resolved
        or not isinstance(audience, str)
        or not audience.strip()
        or "\x00" in audience
    ):
        raise RetainedInputError("RETAINED_OWNER_UNRESOLVED", "input owner is unavailable")
    return principal


def parse_reference(value: object) -> tuple[str, str | None]:
    """Validate the existing bounded canonical page/optional exact unit spelling."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise RetainedInputError("RETAINED_INPUT_INVALID", "input reference is invalid")
    parent, marker, fragment = value.partition("#")
    canonical_id = memory_refs.parse_memory_ref(parent)
    canonical = memory_refs.memory_ref(canonical_id) if canonical_id is not None else None
    if canonical is None or parent != canonical or (marker and (not fragment or "#" in fragment)):
        raise RetainedInputError("RETAINED_INPUT_INVALID", "input reference is invalid")
    return canonical, f"{canonical}#{fragment}" if marker else None


def _read_snapshot(
    vault_root: Path, reference: object, *, committed_path: str | None = None
) -> RetainedInput:
    """Read once; the internal committed-path route never inventories references."""
    principal = _principal()
    canonical, unit_ref = parse_reference(reference)
    try:
        path = (
            committed_path
            if committed_path is not None
            else egress.resolve_visible_identifier(vault_root, canonical, principal=principal)
        )
        prepared = prepare_page_read(vault_root, path=path)
        page = get_page(vault_root, path=path, _prepared=prepared)
    except (
        memory_refs.ReferenceError,
        GetError,
        OSError,
        ValueError,
        reserved_paths.ReservedPathLeafError,
    ) as error:
        raise _unavailable() from error
    if memory_refs.ref_from_markdown(page.content) != canonical:
        if committed_path is not None:
            raise RetainedInputError(
                "RETAINED_INPUT_INVALID", "the page at that path holds another ref"
            )
        raise _unavailable()
    try:
        physical = reserved_paths.resolve_physical_relative(vault_root, prepared.resolved_relative)
        guard = PathGuard.capture(
            vault_root,
            physical,
            leaf_policy="content",
            expected_content_hash=hashlib.sha256(prepared.raw).hexdigest(),
            expected_content_size=len(prepared.raw),
        )
    except (OSError, ValueError, reserved_paths.ReservedPathLeafError):
        # Only the committed receipt adapter can use an unguarded snapshot,
        # and only for its historical opaque digest. Release always refuses.
        guard = None
    return RetainedInput(canonical, unit_ref, page, guard)


def _release_snapshot(
    vault_root: Path,
    snapshot: RetainedInput,
    *,
    disclosure: bool = False,
    status_basis: lifecycle_statuses.Basis | None = None,
) -> RetainedInput:
    """Release and select only this snapshot, retaining its nested decision context.

    A superseded input cannot support a new binding, but disclosing an existing
    one depends only on release: supersession is accounting state.
    """
    principal = _principal()
    page = snapshot.page
    if snapshot.guard is None:
        raise _unavailable()
    with egress.disclosure_boundary(vault_root, "episode-input-authorization") as collector:
        released = egress.annotate_page(
            vault_root,
            page.as_dict(include_raw=True),
            principal=principal,
            snapshot_content=page.content,
            stable_ref=snapshot.canonical,
        )
        if (
            released is None
            or released.get("content_hash") != page.content_hash
            or released.get("body") != page.body
        ):
            raise _unavailable()
        basis = status_basis or lifecycle_statuses.Basis(vault_root)
        if not disclosure:
            try:
                if basis.classify(page.frontmatter.get("status")).require() == "superseded":
                    raise _unavailable()
            except lifecycle_statuses.OpError as error:
                raise _unavailable() from error
        unit = None
        if snapshot.unit_ref is not None:
            selected = semantic_unit_read.read_semantic_unit(
                vault_root,
                page=page,
                unit_ref=snapshot.unit_ref,
                frontmatter=released.get("frontmatter"),
                lifecycle_disposition=not disclosure,
                status_basis=basis,
            )
            if (
                selected.status != "found"
                or selected.unit is None
                or selected.parent.ref != snapshot.canonical
            ):
                raise _unavailable()
            unit = selected.unit
        try:
            snapshot.guard.recheck(vault_root)
        except (OSError, ValueError) as error:
            raise _unavailable() from error
    authorization = [
        outcome.value
        for outcome in collector.outcomes
        if outcome.value.get("decision") == "released"
        and outcome.value.get("ref") == snapshot.canonical
    ]
    if len(authorization) > 1:
        raise _unavailable()
    return replace(
        snapshot,
        unit=unit,
        released=released,
        authorization=authorization[0] if authorization else None,
    )


def resolve_retained_input(
    vault_root: Path,
    reference: object,
    *,
    committed_path: str | None = None,
    disclosure: bool = False,
) -> RetainedInput:
    """Resolve one currently released input; committed_path is trusted receipt plumbing."""
    snapshot = _read_snapshot(vault_root, reference, committed_path=committed_path)
    return _release_snapshot(vault_root, snapshot, disclosure=disclosure)


def exact_text_visible(vault_root: Path, value: str) -> bool:
    """Check an exact selected representation without recording unreturned targets."""
    principal = _principal()
    if not egress.direct_text_references_visible(vault_root, value, principal=principal):
        return False
    with egress.disclosure_boundary(vault_root, "retained-input-reference-validation") as collector:
        redacted = egress.redact_withheld_references(vault_root, value, principal=principal)
        scrubbed = egress.postfilter("get", redacted, vault_root)
    if collector.credential_redactions:
        egress._record_credential_block(collector.credential_redactions)  # noqa: SLF001
    return redacted == value and scrubbed == value


def recheck_retained_inputs(
    vault_root: Path,
    selections: tuple[tuple[RetainedInput, str], ...],
    *,
    disclosure: bool = False,
) -> None:
    """Refresh a bounded set's release checks, not an atomic cross-system snapshot."""
    status_basis = lifecycle_statuses.Basis(vault_root)
    for snapshot, text in selections:
        released = _release_snapshot(vault_root, snapshot, disclosure=disclosure, status_basis=status_basis)
        if (
            released.released is None
            or released.released.get("frontmatter") != snapshot.page.frontmatter
            or not exact_text_visible(vault_root, text)
        ):
            raise _unavailable()
    context = _principal().verified_authorization_session
    if context is not None:
        try:
            now = int(time.time())
            custody = authorization_custody.load_authorization_custody(vault_root, now=now)
            with closing(store.open_active_governance_read_connection(vault_root)) as connection:
                connection.execute("BEGIN")
                authorization_session_lifecycle.status_verified_session(
                    connection, custody=custody, context=context, now=now
                )
        except (
            authorization_custody.AuthorizationCustodyUnavailable,
            authorization_session_lifecycle.AuthorizationSessionUnavailable,
            OSError,
            sqlite3.Error,
            store.UnsupportedGovernanceSchema,
        ) as error:
            raise _unavailable() from error
