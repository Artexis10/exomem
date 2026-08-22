"""Load one request-pinned active authorization-projection namespace."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..find_types import Hit
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    projected_retrieval,
    projection_authorization,
    projection_store,
    schema_v4,
    store,
)
from .principal import RequestPrincipal


class ProjectionRuntimeUnavailable(RuntimeError):
    """The enrolled projected corpus cannot be verified for serving."""


@dataclass(frozen=True, slots=True)
class _PreactivatedProjectionRuntime:
    root_key: str
    cell_id: str
    logical_vault_id: str
    activation_store_id: str
    activation_epoch: int
    activation_state_digest: str
    runtime: ActiveProjectionRuntime


_PREACTIVATED_RUNTIMES: dict[str, _PreactivatedProjectionRuntime] = {}
_PREACTIVATED_RUNTIME_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class ActiveProjectionRuntime:
    snapshot: schema_v4.ActivePolicySnapshot
    namespace: projection_store.VerifiedProjectionNamespace
    catalog: projected_retrieval.ProjectionCatalog = field(
        init=False,
        repr=False,
    )
    lexical_index: projected_retrieval.ProjectedLexicalIndex = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, schema_v4.ActivePolicySnapshot)
            or not isinstance(
                self.namespace,
                projection_store.VerifiedProjectionNamespace,
            )
            or self.namespace.active_state_digest
            != self.snapshot.active.activation_state_digest
            or self.namespace.namespace_key.policy_fingerprint
            != self.snapshot.active.policy_fingerprint
            or self.namespace.namespace_key.projector_schema_version
            != self.snapshot.active.projector_schema_version
            or self.namespace.namespace_key.catalog_generation
            != self.snapshot.active.catalog_generation
        ):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        catalog = projected_retrieval.ProjectionCatalog(self.namespace)
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(
            self,
            "lexical_index",
            projected_retrieval.ProjectedLexicalIndex(catalog),
        )


@dataclass(frozen=True, slots=True)
class ProjectedFindResult:
    """One public acquisition result derived only from the pinned namespace."""

    hits: tuple[Hit, ...]
    withheld_paths: frozenset[str]
    warming_components: tuple[str, ...] = ()
    declared_purpose: str | None = None


def _root_key(vault_root: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(Path(vault_root))))


def _control_activation(
    custody: authorization_custody.AuthorizationCustody,
) -> tuple[str, str, str, int, str] | None:
    control = custody.control
    if not control.governance_enrolled:
        return None
    if (
        control.activation_store_id is None
        or control.activation_epoch is None
        or control.activation_state_digest is None
    ):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    return (
        control.cell_id,
        control.logical_vault_id,
        control.activation_store_id,
        control.activation_epoch,
        control.activation_state_digest,
    )


def _configured_external_custody() -> bool:
    return any(
        variable in os.environ
        for variable in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
        )
    )


def _clear_preactivated_runtimes_for_tests() -> None:
    """Clear process-local serving state between isolated unit tests."""

    with _PREACTIVATED_RUNTIME_LOCK:
        _PREACTIVATED_RUNTIMES.clear()


def has_preactivated_projection_runtime(vault_root: Path) -> bool:
    """Return process-local readiness without opening custody or catalog state."""

    with _PREACTIVATED_RUNTIME_LOCK:
        return _root_key(Path(vault_root)) in _PREACTIVATED_RUNTIMES


def preactivate_projection_runtime(
    vault_root: Path,
) -> ActiveProjectionRuntime | None:
    """Build one exact v4 runtime before a transport accepts requests.

    An unconfigured or authenticated never-enrolled vault has no projected
    runtime.  Configured but unverifiable custody and every incomplete v4
    namespace refuse content-free; startup never relabels them as legacy.
    """

    root = Path(vault_root)
    if not _configured_external_custody():
        return None
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
        activation = _control_activation(custody)
        if activation is None:
            return None
        cell_id, logical_vault_id, store_id, epoch, digest = activation
        connection = store.open_active_governance_read_connection(root)
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=logical_vault_id,
            expected_activation_store_id=store_id,
            expected_activation_epoch=epoch,
            expected_activation_state_digest=digest,
        )
        expected_manifest = projection_store.manifest_from_namespace_evidence(
            snapshot
        )
        manifest, items = projection_store.load_projection_catalog(
            root,
            key=expected_manifest.namespace_key,
            expected_rows_digest=expected_manifest.rows_digest,
        )
        if manifest != expected_manifest:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        namespace = projection_store.bind_active_projection_namespace(
            snapshot,
            manifest=manifest,
            items=items,
        )
        runtime = ActiveProjectionRuntime(snapshot, namespace)
        record = _PreactivatedProjectionRuntime(
            root_key=_root_key(root),
            cell_id=cell_id,
            logical_vault_id=logical_vault_id,
            activation_store_id=store_id,
            activation_epoch=epoch,
            activation_state_digest=digest,
            runtime=runtime,
        )
        with _PREACTIVATED_RUNTIME_LOCK:
            _PREACTIVATED_RUNTIMES[record.root_key] = record
        return runtime
    except ProjectionRuntimeUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        projection_store.ProjectionStoreError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _preactivated_runtime(vault_root: Path) -> ActiveProjectionRuntime | None:
    root = Path(vault_root)
    key = _root_key(root)
    with _PREACTIVATED_RUNTIME_LOCK:
        record = _PREACTIVATED_RUNTIMES.get(key)
    if record is None:
        return None
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
        activation = _control_activation(custody)
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        ProjectionRuntimeUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    expected = (
        record.cell_id,
        record.logical_vault_id,
        record.activation_store_id,
        record.activation_epoch,
        record.activation_state_digest,
    )
    if activation != expected:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    return record.runtime


def _external_custody_requires_projected_boundary(root: Path) -> bool:
    configured = any(
        variable in os.environ
        for variable in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
        )
    )
    if not configured:
        return False
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        # A configured but unreadable external trust root is not proof that the
        # vault was never enrolled.  Refuse instead of falling into legacy raw
        # acquisition.
        return True
    return custody.control.governance_enrolled


def _unsupported_schema_requires_projected_boundary(root: Path) -> bool:
    if _external_custody_requires_projected_boundary(root):
        return True
    legacy: sqlite3.Connection | None = None
    try:
        legacy = store.open_readonly_connection(root)
        if legacy is not None:
            return False
    except (OSError, RuntimeError, sqlite3.Error):
        return True
    finally:
        if legacy is not None:
            legacy.close()
    # The exact v3 opener intentionally maps corrupt and future schemas to
    # ``None``.  Their physical presence therefore means "unknown authority",
    # not "ungoverned".
    return os.path.lexists(store.sidecar_path(root))


def requires_projected_read_boundary(vault_root: Path) -> bool:
    """Conservatively identify reads that must never reopen a legacy raw lane."""

    root = Path(vault_root)
    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_active_governance_read_connection(root)
    except store.UnsupportedGovernanceSchema:
        return _unsupported_schema_requires_projected_boundary(root)
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error):
        return True
    else:
        return True
    finally:
        if connection is not None:
            connection.close()


def load_active_projection_runtime(
    vault_root: Path,
) -> ActiveProjectionRuntime | None:
    """Return ``None`` only for proven legacy/unenrolled reads.

    Exact-v4 serving remains closed until activation can install a prebuilt
    runtime and the repository's actual-wire timing gate accepts it.  Public
    requests must never rebuild or tokenize the full namespace themselves.
    """

    root = Path(vault_root)
    preactivated = _preactivated_runtime(root)
    if preactivated is not None:
        return preactivated
    try:
        connection = store.open_active_governance_read_connection(root)
    except store.UnsupportedGovernanceSchema:
        if _unsupported_schema_requires_projected_boundary(root):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            ) from None
        return None
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    connection.close()
    # This packet intentionally has no serving-runtime activation seam.  The
    # exact-v4 schema fence above is sufficient to choose the closed result;
    # sampling policy/catalog/namespace blobs here would turn the refusal into
    # a corpus-size timing oracle.
    raise ProjectionRuntimeUnavailable(
        "governed projected retrieval is unavailable"
    )


def _verified_session_grants(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    principal: RequestPrincipal,
    purpose: str | None,
) -> tuple[str | None, tuple[projection_authorization.VerifiedProjectionGrant, ...]]:
    context = principal.verified_authorization_session
    if context is None:
        return purpose, ()
    if not isinstance(
        context,
        authorization_session_lifecycle.AuthorizationSessionContext,
    ):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    connection: sqlite3.Connection | None = None
    now = int(time.time())
    declared_purpose = purpose
    try:
        connection = store.open_authorization_session_connection(vault_root)
        connection.execute("BEGIN")
        if declared_purpose is None:
            declared_purpose = (
                authorization_session_authority.active_session_purpose(
                    connection,
                    context=context,
                    audience=principal.audience_id,
                    now=now,
                )
            )
        pairs = (
            authorization_session_authority.active_session_grants_for_projection_catalog(
                connection=connection,
                context=context,
                audience=principal.audience_id,
                purpose=declared_purpose,
                catalog=tuple(
                    authorization_session_authority.SessionMembership(
                        path=item.item_identity,
                        fingerprint=item.content_hash,
                        scope_ids=item.scope_ids,
                    )
                    for item in runtime.namespace.items
                ),
                policy_fingerprint=runtime.snapshot.policy.fingerprint,
                now=now,
            )
        )
        connection.commit()
    except (
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
        ValueError,
    ) as error:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    catalog = {item.item_identity: item for item in runtime.namespace.items}
    verified: list[projection_authorization.VerifiedProjectionGrant] = []
    for path, grant in pairs:
        item = catalog.get(path)
        if item is None:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        item_scopes = frozenset(item.scope_ids)
        contributing_scopes = tuple(
            scope_id for scope_id in grant.scope_ids if scope_id in item_scopes
        )
        if not contributing_scopes:
            continue
        verified.append(
            projection_authorization.VerifiedProjectionGrant(
                grant_id=grant.grant_id,
                item_identity=path,
                content_hash=item.content_hash,
                policy_fingerprint=grant.policy_fingerprint,
                scope_ids=contributing_scopes,
                audience=grant.audience,
                purpose=grant.purpose,
                ceiling=grant.ceiling,
            )
        )
    return declared_purpose, tuple(verified)


def _wire_hit(
    hit: projected_retrieval.ProjectedLexicalHit,
    *,
    selection: projected_retrieval.ProjectionSelection,
    rank: int,
    knowledge_base_name: str,
    keyword: bool,
) -> Hit:
    decision = selection.decision
    if decision is None or decision.level != hit.decision_level:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    fields = dict(hit.search_fields)
    path = hit.item_identity
    path_parts = PurePosixPath(path).parts
    inside_kb = bool(path_parts and path_parts[0] == knowledge_base_name)
    return Hit(
        path=path,
        type=fields.get("type"),
        scope="kb" if inside_kb else "vault",
        title=fields.get("title") or PurePosixPath(path).stem,
        updated=fields.get("updated", ""),
        excerpt=hit.snippet,
        bm25_rank=None if keyword else rank,
        keyword_rank=rank if keyword else None,
        outside_kb=not inside_kb,
        snapshot_hash=selection.content_hash,
        decision=decision,
    )


def find_projected_hits(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    query: str,
    limit: int,
    mode: str,
    graph: bool,
    rerank: bool | None,
    principal: RequestPrincipal,
    purpose: str | None,
) -> ProjectedFindResult:
    """Acquire public candidates without opening a raw corpus/index lane."""

    if not isinstance(runtime, ActiveProjectionRuntime):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("find: limit must be an integer from 1 through 100")
    if not isinstance(query, str) or len(query) > 1_048_576:
        raise ValueError("find: query must be bounded text")
    if not query or mode != "keyword" or graph or rerank is not False:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    if not isinstance(principal, RequestPrincipal) or not principal.resolved:
        return ProjectedFindResult(
            hits=(),
            withheld_paths=frozenset(
                item.item_identity for item in runtime.namespace.items
            ),
        )

    declared_purpose, verified_grants = _verified_session_grants(
        Path(vault_root),
        runtime,
        principal=principal,
        purpose=purpose if purpose is not None else principal.purpose,
    )
    authorization = projection_authorization.build_authorization_map(
        runtime.namespace,
        policy=runtime.snapshot.policy,
        audience=principal.audience_id,
        purpose=declared_purpose,
        verified_session_grants=verified_grants,
        catalog=runtime.catalog,
    )
    withheld = frozenset(
        selection.item_identity
        for selection in authorization.selections
        if selection.projection_variant_id is None
    )
    projected_hits = runtime.lexical_index.search_keyword(
        authorization,
        query,
        k=limit,
    )

    by_identity = {
        selection.item_identity: selection
        for selection in authorization.selections
    }
    knowledge_base_name = kb_dirname()
    hits = tuple(
        _wire_hit(
            hit,
            selection=by_identity[hit.item_identity],
            rank=rank,
            knowledge_base_name=knowledge_base_name,
            keyword=True,
        )
        for rank, hit in enumerate(projected_hits, 1)
    )
    return ProjectedFindResult(
        hits=hits,
        withheld_paths=withheld,
        declared_purpose=declared_purpose,
    )


__all__ = [
    "ActiveProjectionRuntime",
    "ProjectedFindResult",
    "ProjectionRuntimeUnavailable",
    "find_projected_hits",
    "has_preactivated_projection_runtime",
    "load_active_projection_runtime",
    "preactivate_projection_runtime",
    "requires_projected_read_boundary",
]
