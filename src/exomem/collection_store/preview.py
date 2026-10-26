"""Explicit per-vault store binding for the facades.

A trusted caller binds an already lease-owned connection: the service's store thread
for a store-routed request (``runtime.StoreServer``), or a caller that owns its writer.
Nothing else, and no environment flag, changes file-mode routing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import memory_refs
from .. import structured_collections as collections
from .connection import CollectionStoreError, WriterConnection

if TYPE_CHECKING:
    from .writer import CollectionWriter

# (vault root, writer, served by a production session): only a production session's
# routes answer to the release's capabilities.
_BOUND: ContextVar[tuple[Path, CollectionWriter, bool] | None] = ContextVar(
    "collection_store_preview", default=None
)


# Records and Planning share this disclosure protocol boundary.
QUERY_RECEIPT_BOUNDARY = "record_query"


@contextmanager
def preview_store(vault_root: Path, handle: WriterConnection) -> Iterator[CollectionWriter]:
    """Bind a dark writer for this call context; ownership remains with the caller."""
    from ..writer_lease import active_manager
    from .runtime import CollectionStoreRuntime, borrowed_writer

    root = Path(vault_root).resolve()
    production = False
    if isinstance(handle, CollectionStoreRuntime):
        if root != handle.root:
            raise CollectionStoreError("COLLECTION_STORE_VAULT_MISMATCH", "foreign preview runtime")
        production = handle._session is not None and handle._session.production
        checkout = handle.checkout()
    else:
        checkout = borrowed_writer(root, handle, active_manager())
    with checkout as writer:
        token = _BOUND.set((root, writer, production))
        try:
            yield writer
        finally:
            _BOUND.reset(token)


@contextmanager
def unbound() -> Iterator[None]:
    """Bind no writer for the rest of this call context, as after a create left the store pending.

    The vault's marker does not route that collection yet, so the call's remaining reads
    take ordinary file authority.
    """
    token = _BOUND.set(None)
    try:
        yield
    finally:
        _BOUND.reset(token)


def bound_writer(vault_root: Path) -> CollectionWriter | None:
    binding = _BOUND.get()
    return binding[1] if binding is not None and binding[0] == Path(vault_root).resolve() else None


def production_bound(vault_root: Path) -> bool:
    """Whether a production session serves ``vault_root`` here, so its routes answer to the release."""
    binding = _BOUND.get()
    return binding is not None and binding[0] == Path(vault_root).resolve() and binding[2]


def selected_writer(vault_root, selector, *, prove=None):
    from . import ownership

    writer = bound_writer(vault_root)
    if writer is None:
        return None
    proof = prove() if prove is not None else ownership.current(
        vault_root, conn=writer.connection, private=not production_bound(vault_root))
    entry = proof.selected(vault_root, selector)
    if entry is None:
        return None
    if entry["collection_id"] not in proof.admitted:
        raise ownership.unavailable()
    return writer


#: What a caller hears about a store collection in a process that does not serve the store.
UNSERVED = "this collection lives in the collection store, which only the running Exomem service serves"
#: What it hears when this process serves the store but the operation runs on files alone.
UNSUPPORTED = "this collection lives in the collection store, and this operation does not run on the store yet"
MARKER_UNREADABLE = ("the collection store's authority marker is unreadable, so no collection here can be "
                     "told apart from one the store owns")


def store_notice(vault_root, authorize_path):
    """Who may learn that the store owns a collection this call cannot reach: a test over manifest paths.

    The vault's owner may, so an unavailable C stays in its inventory, and so may a caller
    whose release covers the path. Anyone else hears that the collection is absent, so a
    withheld collection's existence never leaks. A None path (a selector by identity
    under an unreadable marker) is visible to the owner alone.
    """
    from ..governance import raw_protection
    from ..governance.principal import effective_principal

    who = effective_principal()
    # An unavailable C must remain visible in its owner's inventory; this grants a notice, never view bytes.
    owner = raw_protection.is_owner(who) and raw_protection.has_unrestricted_access(vault_root, who)
    return lambda path: owner or (path is not None and authorize_path(path))


def require_file_collection(vault_root, selector, *, authorize_path, notice_path=None):
    """Refuse a file-mode mutation of a collection that the vault's marker routes to the store.

    Every file-mode writer resolves its collection through the mutation resolver, which
    calls this first, so a write never lands in a routed collection's generated views.
    Reads keep resolving those views off the store thread on purpose, so this check
    belongs to mutation resolution alone. ``notice_path`` is the manifest path a caller
    holds for a selector by identity; an unreadable marker decides the notice on it.
    """
    from ..cli_ops import OpError
    from . import ownership

    root = Path(vault_root)
    writer = bound_writer(root)
    if writer is not None:
        proof = ownership.current(root, conn=writer.connection, private=not production_bound(root))
        if proof.selected(root, selector) is not None:
            # No production caller reaches this: route() sends a routed selector to the store thread, whose dispatch() writes it.
            raise collections.CollectionError(
                "COLLECTION_STORE_ROUTE_REQUIRED", "a store collection is written only by its store writer")
        return
    try:
        proof = ownership.current(root)
    except (CollectionStoreError, RuntimeError, OpError) as error:
        # The mutation boundary's typed busy result drives the existing capture retry.
        if isinstance(error, OpError) and error.code == "MUTATION_BUSY":
            raise
        # Which collections the store owns is unknown, so none can be written as a file.
        path = notice_path if notice_path is not None else _selector_path(root, selector)
        _refuse(root, path, authorize_path, "COLLECTION_STORE_UNAVAILABLE", MARKER_UNREADABLE)
    entry = proof.selected(root, selector)
    if entry is None:
        return
    from .runtime import served

    _refuse(root, entry["manifest_path"], authorize_path, "COLLECTION_STORE_UNAVAILABLE",
            UNSUPPORTED if served(root) else UNSERVED)


def _selector_path(root, selector):
    """The manifest path a selector spells, or None when it names a collection by identity."""
    if isinstance(selector, collections.CollectionManifest):
        return selector.path
    raw = str(selector).strip()
    if memory_refs.parse_memory_ref(raw) or memory_refs.normalize_id(raw):
        return None
    return collections._reference_key(root, raw)


def _refuse(root, path, authorize_path, code, message):
    if store_notice(root, authorize_path)(path):
        raise collections.CollectionError(code, message)
    raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")


def selected_projection_writer(vault_root, path):
    writer = bound_writer(vault_root)
    if writer is None:
        return writer
    with writer.read_snapshot():
        return writer if writer._operation.projection_subjects(path) is not None else None


def canonical_read(function=None, *, projection=False, unavailable=None,
                   receipt_boundary: str | None = None):
    """Keep a structured consumer's canonical reads under one request snapshot."""
    from ..cli_ops import OpError

    if function is None:
        return lambda function: canonical_read(function, projection=projection, unavailable=unavailable,
                                               receipt_boundary=receipt_boundary)

    @wraps(function)
    def read(vault_root, *args, **kwargs):
        writer = bound_writer(vault_root)
        if writer is None:
            if not projection:
                return function(vault_root, *args, **kwargs)
            from .runtime import projection_route

            # These consumers take a canonical path or page as their first argument; bulk callers decide each path below.
            value = args[0] if args else kwargs.get("rel_path", kwargs.get("page"))
            path = value.get("path") if isinstance(value, Mapping) else value
            try:
                if isinstance(path, str) and (server := projection_route(vault_root, path)) is not None:
                    return server.call(lambda: read(vault_root, *args, **kwargs))
            except (CollectionStoreError, OpError):
                if unavailable is None:
                    raise
                # Discovery withholds this C candidate instead of making unrelated A/B unavailable.
                return unavailable()
            return function(vault_root, *args, **kwargs)
        with writer.read_snapshot():
            principal = kwargs.get("principal")
            purpose = kwargs.get("purpose")
            if principal is not None and principal != writer._operation.who:
                writer._operation.refuse()
            if purpose is not None and purpose != writer._operation.purpose:
                writer._operation.refuse()
            return function(vault_root, *args, **kwargs)
    from .ownership import reader

    owned_read = reader(read)
    if receipt_boundary is None:
        return owned_read

    @wraps(function)
    def disclosed(vault_root, *args, **kwargs):
        from ..governance import egress

        # A composite owns its collector until the whole-result ownership proof finishes.
        with egress.disclosure_boundary(vault_root, receipt_boundary, join_existing=True) as collector:
            try:
                return owned_read(vault_root, *args, **kwargs)
            finally:
                egress.emit_boundary_receipt(collector)
    return disclosed


@canonical_read(projection=True, unavailable=lambda: (True, None))
def presentation_label(vault_root, path):
    """Resolve store-owned relationship labels through the existing canonical read route."""
    writer = bound_writer(vault_root)
    if writer is None:
        return False, None
    from . import views

    return views.presentation_label(writer._operation, path)


def presentation_wikilinks(vault_root):
    """Use a bound canonical namespace; offline migration supplies its own verified snapshot."""
    from .. import vault

    writer = bound_writer(vault_root)
    if writer is not None:
        with writer.read_snapshot():
            from . import views

            return views.presentation_wikilinks(writer._operation)
    return vault.WikilinkResolver(Path(vault_root))


def projection_decision(vault_root, path, *, policy, audience, purpose,
                        authorization_context, content=None):
    """Join the bound operation; None alone permits ordinary file authority."""
    writer = bound_writer(vault_root)
    if writer is None:
        return None
    from ..governance.decisions import Decision

    with writer.read_snapshot():
        operation = writer._operation
        if (audience != operation.who.audience_id or purpose != operation.purpose
                or authorization_context != operation.context
                or policy.fingerprint != operation.policy.fingerprint):
            return Decision(0)
        return operation.projection_decision(
            path, content=content,
            manifest_for=lambda cid: writer._collection_manifest(writer._collection_row(cid))[0],
        )


def released_summary(vault_root, path, principal, *, include_raw=False):
    """Resolve a generated recipient overview from canonical admission, including held publication."""
    writer = bound_writer(vault_root)
    if writer is None:
        return False, None
    with writer.read_snapshot():
        operation = writer._operation
        if operation.who != principal:
            operation.refuse()
        row = writer.connection.execute(
            "SELECT collection_id FROM projection_state WHERE path=? AND kind='summary'", (path,)).fetchone()
        if row is None:
            return False, None
        if selected_writer(vault_root, row[0]) is None or selected_projection_writer(vault_root, path) is None:
            return False, None
        manifest = operation.field_manifest(row[0])
        plan = operation.field_plan(manifest)
        if plan.owner:
            return False, None
        from .summary import released_page

        return True, released_page(operation, plan.manifest, path, include_raw=include_raw)


def _mutate(vault_root, method, *args, **kwargs):
    """Own the leaf boundary when the dispatcher holds only its writer fence."""
    from ..writer_lease import active_manager

    if kwargs.get("_prepare") is not None:
        # A prepare writes no row and rolls back. The curation command that asks already holds the
        # boundary, so a second one would only advance the commit generation a second time.
        return method(*args, **kwargs)

    # The store write revalidates its token with the coordinator before it commits, so this guard may keep
    # the operation's grant instead of asking for a second one.
    with active_manager().mutation_guard(vault_root, operation=method.__name__, reuse_operation_grant=True):
        return method(*args, **kwargs)


def dispatch(
    vault_root: Path, profile: str, action: str, values: Mapping[str, Any]
) -> tuple[bool, Any]:
    """Use a bound preview only after the facade's unchanged argument validation."""
    binding = _BOUND.get()
    if binding is None or binding[0] != Path(vault_root).resolve():
        return False, None
    writer = binding[1]
    from . import ownership

    @cache
    def prove():
        # One ownership proof serves this dispatch's checks; the next dispatch proves again.
        return ownership.current(vault_root, conn=writer.connection, private=not binding[2])

    if binding[2]:
        from .capability import require_records_summary_route

        require_records_summary_route(vault_root, writer, action, values, prove=prove)
    args = {name: value for name, value in values.items() if value is not None}

    if action == "describe" and profile == "records":
        from ..query_engine import route
        from ..record_memory import parse_manifest_contract
        from .importer import contract

        writer._require_operation_context()
        return True, {**parse_manifest_contract(store_mode=True), "import": contract(), "query": route.contract()}

    proof = prove()
    if proof.marker is not None:
        selector = args.get("collection", args.get("manifest_path"))
        if action == "create" and binding[2]:
            from .runtime import served_create

            created = served_create(vault_root, args)
            if created is not None:
                return True, created
        if selector is None or selected_writer(vault_root, selector, prove=prove) is None:
            return False, None
        if action == "create":
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "store creation requires admission")
    if action == "describe":
        return False, None
    writer._require_operation_context()
    writer._facade_profile = profile
    if action == "validate":
        if args.get("collection") is None:
            return False, None
        return True, writer.validate_revision(args["collection"], manifest_text=args["manifest_text"])
    # Records inspection is generic in file mode; only its mutations require
    # the Records profile. Planning inspection retains its profile guard.
    if action == "inspect" and profile == "records":
        return True, writer.inspect_collection(args["collection"], facade_profile=profile)
    if action == "inspect":
        return True, writer.inspect_collection(args["collection"])
    if action == "query" and "query" in args:
        from ..query_engine import route

        return True, route.run(writer, args["collection"], args["query"], facade_profile=profile)
    if action == "query":
        from .. import planning, record_governance

        collection = args.pop("collection")
        with writer.read_collection(collection, facade_profile=profile) as manifest:
            if profile == "planning":
                return True, planning.query(vault_root, manifest, **args)
            include_history = args.pop("include_agent_history", False)
            from .history import agent_history

            result = record_governance.query_collection(vault_root, manifest, **args)
            projected = record_governance.project_query_result(
                result, manifest, output_format=args.get("output_format", "json"),
                agent_history=agent_history(writer, manifest) if include_history else None,
            )
            projected = writer._operation.query_metadata_evidence(projected, writer, manifest, result)
            return True, projected
    if action == "create":
        return True, _mutate(vault_root, writer.create_collection, **args)
    if action == "import" and profile == "records":
        from . import importer

        return True, importer.dispatch(
            vault_root, writer, args["collection"], args["import_request"]
        )
    collection = args.pop("collection")
    if profile == "planning" and "plan_id" in args:
        args["item_key"] = args.pop("plan_id")
    if action in {"append", "add"}:
        return True, _mutate(vault_root, writer.append_record, collection, **args)
    if action == "bulk_upsert" and profile == "records":
        return True, _mutate(vault_root, writer.bulk_upsert_records, collection, **args)
    if action == "triage":
        args["changes"] = args.pop("transition")
        args["operation"] = "triage"
    if action in {"update", "triage"}:
        args.setdefault("changes", {})
        args["refresh_presentation"] = args.get("refresh_presentation") is True
        return True, _mutate(vault_root, writer.update_record, collection, **args)
    if action == "revise":
        if binding[2] and isinstance(args.get("manifest_text"), str):
            from .admission import require_summary_manifest
            from .summary import SUMMARY

            # Under the caller's own authority first, so a hidden collection still refuses as not found.
            # A summary collection stays one; an items-mode store collection from before S1 revises as before.
            with writer.read_collection(collection, facade_profile=profile) as current:
                if current.view_mode == SUMMARY:
                    require_summary_manifest(vault_root, current.path, args["manifest_text"])
        return True, _mutate(vault_root, writer.revise_collection, collection, **args)
    if action == "rebaseline":
        return True, _mutate(vault_root, writer.rebaseline_collection, collection, **args)
    if action == "discard":
        return True, _mutate(vault_root, writer.discard_held, collection, **args)
    if action == "history":
        with writer.read_collection(collection, facade_profile=profile) as manifest:
            if manifest.derived:
                from .derived_rows import refused

                raise refused("row history")
            from .history import row_history

            return True, row_history(writer, manifest, **args)
    raise collections.CollectionError(
        "COLLECTION_STORE_PREVIEW_UNSUPPORTED", "this operation belongs to a later store slice"
    )
