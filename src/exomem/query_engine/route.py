"""The agent route for typed v1 collection queries and their discovery chapter.

OpenSpec add-collection-query-engine §8 and §11 (S1.4, 5.3, 7.6b).
``record_memory(action="query", collection=..., query={...})`` reaches ``run``
through the store dispatch. It owns no grammar: ``validation`` normalizes the
closed object against the admitted collection's declaration, and the shared
read session admits, bounds and executes it, as a typed row page or a grouped
reduction, in the requested mode and execution profile.

``chapter`` answers ``schema_memory(subject="query-engine", operation="inspect")``
from the same validator, profile and importer constants. It reads no vault
content and writes nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, replace

from .. import capabilities, item_keys
from ..structured_collections import declared_references
from . import (
    cursors,
    ir,
    reductions,
    reference_resolution,
    reference_rows,
    runtime,
    typed_rows,
    validation,
)
from .runtime import QueryError, QueryLimits

VERSION = 1
CHAPTERS = ("collections", "import")
#: Where a refusal points the caller, and whether the same request can succeed later.
_REPAIRS = {
    "QUERY_BUSY": ("query", "Retry shortly; the store's readers are in use.", True),
    "QUERY_TIMEOUT": ("query", "Lower page.limit or narrow the local-day window. "
                      "Aggregates can use execution_profile analytics.", True),
    "QUERY_COST_LIMIT": ("query", "Lower page.limit or narrow the local-day window. "
                         "Aggregates can coarsen the bucket or use analytics.", False),
    "QUERY_GROUP_LIMIT": ("group_by", "Narrow the local-day window or coarsen the bucket.", False),
    "QUERY_RESULT_TOO_LARGE": ("page.limit", "Lower page.limit or select fewer fields.", False),
    "QUERY_CURSOR_INVALID": ("page.after", "Send next_cursor unchanged with the same query.", False),
    "QUERY_CURSOR_STALE": ("page.after", "The collection changed; restart from the first page.", False),
    "QUERY_UNSUPPORTED": ("query", "Use only the operations the query-engine chapter lists.", False),
    "QUERY_CAPABILITY_UNAVAILABLE": ("name", "Use name collections or import.", False),
    "QUERY_PROJECTION_BUILDING": ("collection", "Retry shortly; the service is building this collection's "
                                  "query projection.", True),
    "QUERY_PROJECTION_FAILED": ("collection", "Revise the collection's declaration; its query projection failed "
                                "on the declaration or on a stored value.", False),
    "QUERY_REBUILDING": ("collection", "Retry once the rebuild ends; rollup queries and the summary page answer "
                         "meanwhile. A stopped rebuild names its member and code in progress.stop.", True),
}


class Refusal(QueryError):
    """A validation finding, raised with its own location and repair."""

    def __init__(self, finding: validation.Finding) -> None:
        super().__init__(finding.code, finding.expected)
        self.finding = finding


def details(error: QueryError) -> dict:
    """The public ``{code, at, expected, allowed, repair, retryable}`` of a refusal."""
    finding = getattr(error, "finding", None)
    if finding is None:
        at, repair, retryable = _REPAIRS.get(
            error.code, ("query", "Revise the query; describe the collection for its fields.", False))
        finding = validation.Finding(error.code, at, error.message, (), repair, retryable)
    found = finding.as_dict()
    return found if error.progress is None else {**found, "progress": error.progress}


def _profile(raw) -> str:
    """The session profile, read before validation; an invalid value is reported by validation."""
    profile = raw.get("execution_profile") if isinstance(raw, dict) else None
    return profile if profile in validation.PROFILES else "interactive"


def _names_reference(query) -> bool:
    """Whether the query names a reference: a ``related`` entry or a dotted path through one."""
    return bool(query.related) or any(field.alias is not None for field in ir.fields(query))


def run(writer, selector: str, raw, *, facade_profile: str,
        view: str | None = None, view_identity: str | None = None) -> dict:
    """Validate ``raw`` against the admitted collection and run it in its mode and profile.

    Link values pass the legacy query's own projector, so a link to a page the
    caller cannot read is omitted, whoever the caller is. A link field may be
    selected, aggregated or grouped, which read projected values, but cannot
    filter, sort or join, which would evaluate the stored ones.
    """
    from ..record_governance import _LinkProjector

    with writer.read_collection(selector, facade_profile=facade_profile) as manifest:
        collection_id = manifest.collection_id
    limits = QueryLimits(profile=_profile(raw))
    with runtime.read_session(writer.root, writer.handle.path, limits=limits) as session:
        # The session's own authorization snapshot decides links, as it decides rows.
        operation = session._authorization
        with session._manifest(collection_id) as (manifest, basis, _, uniform):
            links = frozenset(name for name, spec in manifest.schema.fields.items()
                              if _LinkProjector.carries_links(spec))
            withheld = session._field_plans[collection_id].item_key_withheld
            declaration = typed_rows.declaration(manifest, basis, projected=links, item_key_withheld=withheld)
        if links:
            # A bare-title or memory link may build the candidate index inside the query deadline;
            # a cold build that runs past it fails the query with QUERY_TIMEOUT, never drops the link.
            session.project_with(_LinkProjector.create(writer.root, manifest, policy=operation.policy,
                                                       authorize_path=operation.allows_file), fields=links)
        bound_refs: dict = {}

        def references(source_cid, dotted):
            """Bind one declared reference and describe its target to the validator."""
            bound = bound_refs.get((source_cid, dotted))
            if bound is None:
                lookup = session.admit_lookup(source_cid)
                declared = next((item for item in declared_references(lookup.manifest.schema)
                                 if item.dotted == dotted), None)
                if declared is None:
                    head = dotted.split(".", 1)[0]
                    # An admitted field without a declaration is an unknown path; a withheld
                    # one reads exactly like an undeclared one.
                    if head in lookup.manifest.schema.fields:
                        raise QueryError("QUERY_FIELD_UNKNOWN", "no declared reference")
                    raise QueryError("QUERY_FIELD_UNAVAILABLE", "this field is not available to resolve")
                bound = bound_refs[source_cid, dotted] = reference_resolution.bind(
                    session, source_cid, dotted)
            plan = session._field_plans[bound.collection_id]
            target_links = frozenset(
                name for name, spec in bound.manifest.schema.fields.items()
                if _LinkProjector.carries_links(spec))
            return {"ref": bound.collection_id, "many": bound.many, "type": bound.basis.type_name,
                    "declaration": typed_rows.declaration(
                        bound.manifest, bound.basis, projected=target_links,
                        item_key_withheld=plan.item_key_withheld)}

        result = validation.normalize_query(raw, declarations={collection_id: declaration},
                                            collection=collection_id, references=references)
        if result.findings:
            finding = result.findings[0]
            if view is not None and finding.target_side:
                # The view crossed into another collection that no longer offers the
                # field or reference to this caller — it is stale, not malformed.
                # One fixed text, so a removed and a withheld dependency read the same.
                finding = validation.Finding(
                    "SAVED_VIEW_STALE", finding.at, "a reference or field another collection offers you", (),
                    "This view depends on a reference or field that another collection "
                    "no longer offers you; revise the view.", finding.retryable)
            raise Refusal(finding)
        query = result.query
        view_meta = {"view": {"name": view, "identity": view_identity}} if view is not None else {}
        named = _names_reference(query)
        filtered_reduction = query.aggregate is not None and (
            named or query.where is not None
            or any(value.where is not None for value in query.aggregate.values))
        if query.mode == "compose":
            # Compose reads no values, but refuses every shape and profile the other modes refuse.
            if named or filtered_reduction:
                reference_rows.check_shape(session, query, bound_refs, manifest=manifest)
            elif query.aggregate is None:
                typed_rows.check_shape(replace(query, mode="execute"), manifest, basis, item_key_withheld=withheld)
            else:
                reductions.check_shape(query, manifest, limits)
            return {**view_meta, "source": asdict(query.source), "query_version": query.version,
                    "schema_version": manifest.schema.version, "mode": "compose",
                    "kind": "rows" if query.aggregate is None else "groups",
                    "fingerprint": cursors._hash(replace(query, page=replace(query.page, after=None))),
                    "execution_profile": limits.profile, "bounds": limits.bounds()}
        if query.aggregate is not None:
            if filtered_reduction:
                return {**view_meta, **reference_rows.reduce(
                    session, query, bound_refs, basis=basis,
                    uniform=uniform, manifest=manifest, as_of=query.as_of)}
            return {**view_meta, **session.reduce(query, as_of=query.as_of)}
        if named:
            return {**view_meta, **reference_rows.run(
                session, query, bound_refs, basis=basis,
                uniform=uniform, manifest=manifest, as_of=query.as_of)}
        if query.mode == "execute":
            return {**view_meta, **cursors.execute_page(session, query, as_of=query.as_of)}
        return {**view_meta, **_admitted_rows(session, query, limits)}


def _admitted_rows(session, query, limits) -> dict:
    """Explain, preview or dry-run a row query: admission and preparation, no result rows."""
    as_of = query.as_of or dt.datetime.now(dt.UTC).isoformat()
    admitted = session.admit_query(replace(query, mode="execute", page=replace(query.page, after=None)),
                                   as_of=as_of)
    declared = dict(zip(admitted.projection.index_names, (index.name for index in admitted.projection.indexes),
                        strict=True))
    # The implicit item-key index is usable but has no declared field index name.
    seek = admitted.uniform and admitted.compiled.usable_index is not None
    result = {"source": asdict(query.source), "query_version": query.version,
              "schema_version": admitted.schema_version, "mode": query.mode, "as_of": admitted.compiled.as_of,
              "execution_profile": limits.profile, "bounds": limits.bounds(),
              "plan": {"kind": "rows", "release": "uniform" if admitted.uniform else "admitted",
                       "access": "index" if seek else "admitted_scan",
                       "index": declared.get(admitted.compiled.usable_index) if seek else None,
                       "fields": list(admitted.fields)}}
    if query.mode == "explain":
        return result
    # Admission's charge against bounds.max_row_visits: a bound on the work, not a count of rows.
    if query.mode == "preview":
        return {**result, "visit_charge": admitted.estimated_visits}
    return {**result, "admitted": True, "visit_charge": admitted.estimated_visits}


def contract() -> dict:
    """The short query section of store-mode ``describe``: the route and where its grammar is."""
    return {"route": capabilities.active_call_route(
                "record_memory",
                {"action": "query", "collection": "<collection>", "query": {"version": VERSION}}),
            "grammar": {"tool": "schema_memory",
                        "args": {"subject": "query-engine", "operation": "inspect", "name": "collections"}},
            "modes": list(validation.MODES), "execution_profiles": list(validation.PROFILES),
            "item_keys": {
                "default_order": (
                    "A query with no order_by returns rows by item_key ascending. On a collection with "
                    f"{item_keys.ORDERED_V1} keys that is the lowest natural keys first (a number above 2**53 orders by its "
                    f"binary64 value); on {item_keys.HASHED_V1} keys it is a stable order with no meaning."),
                "scheme": (
                    f"A new derived collection takes {item_keys.ORDERED_V1} unless a natural-key field is a "
                    f"location or has no ordered encoding; every other collection keeps {item_keys.HASHED_V1}. "
                    f"An {item_keys.ORDERED_V1} item key is 1 to {item_keys.MAX_KEY_BYTES} bytes, spelled as "
                    f"2 to {item_keys.MAX_KEY_BYTES * 8 // 5} characters; a {item_keys.HASHED_V1} key is a "
                    "16-byte UUID. inspect reports the scheme and the byte lengths as contract.item_keys."),
                "key_range": (
                    f"On an {item_keys.ORDERED_V1} collection, a key range bound must be the exact spelling "
                    "of an item key (lowercase base32hex); an equality that no key spells matches no row.")}}


def chapter(name: str | None) -> dict:
    """One bounded query-engine chapter: capability, version, grammar and unavailable operations."""
    if name == "graph":
        raise QueryError("QUERY_CAPABILITY_UNAVAILABLE",
                         'graph queries ship with connect_memory(operation="query"), which is not available yet')
    if name not in CHAPTERS:
        raise ValueError(f"INVALID_SCHEMA_ARGUMENT: query-engine inspect needs name in {[*CHAPTERS, 'graph']}")
    head = {"subject": "query-engine", "chapter": name, "version": VERSION}
    if name == "import":
        from ..collection_store import importer

        return {**head, "capability": "collection-import",
                "route": capabilities.active_call_route(
                    "record_memory",
                    {"action": "import", "collection": "<collection>", "import_request": {"mode": "preview"}}),
                "contract": importer.contract(),
                "before_start": {
                    "step": "Apply recommended_declarations from the preview with one "
                            f"{capabilities.active_tools_text('record_memory', 'write')} revise, then start.",
                    "revise": "Merge fields into item_schema.fields and rollups into rollups in the manifest. "
                              "Take both hashes from inspect lifecycle_guards.",
                    "result": "The imported rows answer filters and sorts on the time field by index, and daily "
                              "totals by rollup. omitted lists each recommendation a budget stopped.",
                },
                "unavailable": ["import into a file-mode collection: IMPORT_UNAVAILABLE"]}
    limits = validation.LIMITS
    return {
        **head, "capability": "collection-query",
        "route": contract()["route"],
        "query": {
            "version": "required; 1",
            "mode": {"compose": "validate and fingerprint; reads no values",
                     "explain": "release, access path, declared index or rollup, bounds",
                     "preview": "explain plus what the plan reads: visit_charge for rows (admission's charge "
                                "against bounds.max_row_visits, plus an estimated lookup per row for each reference "
                                "named), admitted_rows or rollup_buckets for groups",
                     "dry_run": "admission and preparation without result rows",
                     "execute": "the default; one page of rows or groups"},
            "execution_profile": {name: {**bounds, **({"concurrent_per_store": runtime.MAX_ANALYTICS_SESSIONS}
                                                      if name == "analytics" else {})}
                                  for name, bounds in runtime.PROFILES.items()},
            "select": f"up to {limits['select']} declared fields; default every declared field; "
                      "a dotted path through a single-valued declared reference selects a target field",
            "related": "up to 8 entries naming declared references to expand per row; a string, or "
                       "{field: <dotted declaration>, limit: 1-50 (default 10)} bounding many-valued expansion",
            "where": {"leaf": {"field": "<declared field or reference path>", "op": sorted(validation._OPS),
                               "value": "<typed>"},
                      "combine": ["all", "any", "not"], "max_leaves": limits["predicate_leaves"],
                      "max_depth": limits["boolean_depth"], "max_in_values": limits["membership_values"],
                      "references": "a path through up to 2 single-valued declared references; a leaf whose "
                                    "reference did not resolve is unknown under three-valued logic; "
                                    "where is allowed on a reduction and forces the base-row path"},
            "order_by": f"up to {limits['order_by']} of {{field, direction: asc|desc, nulls: first|last}}",
            "group_by": {"max": limits["group_by"], "key": {"field": "<declared field or reference path>",
                                                             "bucket": list(validation.BUCKETS),
                                                             "from": "inclusive local date, bucketed key only",
                                                             "to": "inclusive local date, bucketed key only"}},
            "aggregates": {"max": limits["aggregates"], "alias": {"op": ["count", "sum", "avg", "min", "max",
                                                                         "latest"], "field": "<declared field>",
                                                                 "where": "optional per-aggregate filter "
                                                                          "over base and path fields"}},
            "page": {"limit": {"default": limits["page_default"], "rows": limits["row_page"],
                               "groups": limits["group_page"]},
                     "after": "next_cursor from the previous page of the same query"},
            "as_of": "optional RFC 3339 instant; a continuation keeps its first page's",
            "max_bytes": limits["query_bytes"],
        },
        "result": {
            "max_bytes": runtime.MAX_RESULT_BYTES,
            "fields": ["rows | groups", "returned", "has_more", "next_cursor", "truncated", "truncation_reason",
                       "as_of", "source", "query_version", "schema_version", "execution_profile", "bounds"],
            "groups": "also plan (strategy, reason, release, rollup, basis with from/to), flagged_rows and "
                      "flagged_scope: rows whose local day is unknown, counted over the collection, never guessed",
            "references": "a row that names references carries references: {<chain>: resolved|unresolved|none} "
                          "and related: {<entry>: expanded rows}; a related row omits fields the caller "
                          "cannot read; a group key that did not resolve is listed in the group's unresolved",
            "combined": "every query that names references or filters a reduction carries combined: count, "
                        "unresolved_rows, excluded_unresolved and unresolved_values (bounded, with truncated "
                        "and total); on a grouped result groups come whole on every page while "
                        "returned/has_more/next_cursor page the combined rows",
            "local_day": "a bucketed datetime uses its declared offset field; without one, its own numeric offset",
            "links": "a link to a page the caller cannot read is omitted from the row",
        },
        "errors": {"shape": ["code", "message", "retry", "candidates"],
                   "message": "names the field it refused (at), what it expected and how to repair it; "
                              "candidates are the values that field accepts; retry is present when the "
                              "same request can succeed later",
                   "codes": sorted({"QUERY_FIELD_UNKNOWN", "QUERY_FIELD_UNAVAILABLE", "QUERY_KEY_UNKNOWN",
                                    "QUERY_VALUE_INVALID",
                                    "QUERY_INPUT_LIMIT", "QUERY_ARGUMENT_CONFLICT", "QUERY_UNAVAILABLE",
                                    *_REPAIRS})},
        "unavailable": [
            "joins, having, text and graph: use dotted reference paths instead of joins",
            "percentile and distinct_count",
            "a path through a many-valued reference: expand it in related",
            "execution_profile analytics on row queries",
            "filter, sort or join on a link field: QUERY_FIELD_UNAVAILABLE",
            "the graph chapter: QUERY_CAPABILITY_UNAVAILABLE",
            "continuation of a group page under mixed release",
            "a query on a file-mode collection: QUERY_UNAVAILABLE",
        ],
        "example": {"version": VERSION, "group_by": [{"field": "date", "bucket": "day", "from": "2026-03-01",
                                                      "to": "2026-03-31"}],
                    "aggregates": {"days": {"op": "count"}, "steps": {"op": "sum", "field": "steps"},
                                   "mean": {"op": "avg", "field": "steps"}}},
    }
