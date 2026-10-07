"""Explicit per-vault store binding for the facades.

A trusted caller binds an already lease-owned connection: the service's store thread
for a store-routed request (``runtime.StoreServer``), or a caller that owns its writer.
Nothing else, and no environment flag, changes file-mode routing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import structured_collections as collections
from .connection import CollectionStoreError, WriterConnection

if TYPE_CHECKING:
    from .writer import CollectionWriter

# (vault root, writer, served by a production session): only a production session's
# routes answer to the release's capabilities.
_BOUND: ContextVar[tuple[Path, CollectionWriter, bool] | None] = ContextVar(
    "collection_store_preview", default=None
)


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


def selected_writer(vault_root, selector):
    from . import authority

    writer = bound_writer(vault_root)
    if writer is None:
        return None
    marker = authority.routing_marker(writer)
    if marker is None:
        return writer
    entry = authority.selected_entry(vault_root, marker, selector)
    if entry is None:
        return None
    authority.require_selected(writer.connection, marker, entry)
    return writer


def selected_projection_writer(vault_root, path):
    from . import authority

    writer = bound_writer(vault_root)
    if writer is None or authority.routing_marker(writer) is None:
        return writer
    with writer.read_snapshot():
        return writer if writer._operation.projection_subjects(path) is not None else None


def canonical_read(function):
    """Keep a structured consumer's canonical reads under one request snapshot."""
    @wraps(function)
    def read(vault_root, *args, **kwargs):
        writer = bound_writer(vault_root)
        if writer is None:
            return function(vault_root, *args, **kwargs)
        with writer.read_snapshot():
            principal = kwargs.get("principal")
            purpose = kwargs.get("purpose")
            if principal is not None and principal != writer._operation.who:
                writer._operation.refuse()
            if purpose is not None and purpose != writer._operation.purpose:
                writer._operation.refuse()
            return function(vault_root, *args, **kwargs)
    return read


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


def _mutate(vault_root, method, *args, **kwargs):
    """Own the leaf boundary when the dispatcher holds only its writer fence."""
    from ..writer_lease import active_manager

    with active_manager().mutation_guard(vault_root, operation=method.__name__):
        return method(*args, **kwargs)


def dispatch(
    vault_root: Path, profile: str, action: str, values: Mapping[str, Any]
) -> tuple[bool, Any]:
    """Use a bound preview only after the facade's unchanged argument validation."""
    binding = _BOUND.get()
    if binding is None or binding[0] != Path(vault_root).resolve():
        return False, None
    if binding[2]:
        from .capability import require_records_summary_route

        require_records_summary_route(vault_root, binding[1], action, values)
    writer = binding[1]
    args = {name: value for name, value in values.items() if value is not None}
    from . import authority

    marker = authority.routing_marker(writer)
    if marker is not None:
        selector = args.get("collection", args.get("manifest_path"))
        if action == "create" and binding[2]:
            from .runtime import served_create

            created = served_create(vault_root, args)
            if created is not None:
                return True, created
        if selector is None or selected_writer(vault_root, selector) is None:
            return False, None
        if action == "create":
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "store creation requires admission")
    elif action == "create" and binding[2]:
        # Only admission writes a production store's marker; a create cannot stand in for it.
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the store's authority marker is missing")
    if action == "describe":
        if profile == "records":
            from ..record_memory import parse_manifest_contract

            writer._require_operation_context()
            from ..query_engine import route
            from .importer import contract

            return True, {**parse_manifest_contract(store_mode=True), "import": contract(), "query": route.contract()}
        return False, None
    writer._require_operation_context()
    writer._facade_profile = profile
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
        if args.pop("include_agent_history", False):
            raise collections.CollectionError(
                "COLLECTION_STORE_PREVIEW_UNSUPPORTED", "store audit history belongs to a later slice"
            )
        with writer.read_collection(collection, facade_profile=profile) as manifest:
            if profile == "planning":
                return True, planning.query(vault_root, manifest, **args)
            result = record_governance.query_collection(vault_root, manifest, **args)
            return True, record_governance.project_query_result(
                result, manifest, output_format=args.get("output_format", "json"),
            )
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
    if action == "discard":
        return True, _mutate(vault_root, writer.discard_held, collection, **args)
    raise collections.CollectionError(
        "COLLECTION_STORE_PREVIEW_UNSUPPORTED", "this operation belongs to a later store slice"
    )
