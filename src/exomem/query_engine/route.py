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

from . import cursors, reductions, runtime, typed_rows, validation
from .runtime import QueryError, QueryLimits

VERSION = 1
CHAPTERS = ("collections", "import")
#: Where a refusal points the caller, and whether the same request can succeed later.
_REPAIRS = {
    "QUERY_BUSY": ("query", "Retry shortly; the store's readers are in use.", True),
    "QUERY_TIMEOUT": ("query", "Narrow the local-day window or use execution_profile analytics.", True),
    "QUERY_COST_LIMIT": ("query", "Narrow the local-day window, coarsen the bucket or use analytics.", False),
    "QUERY_GROUP_LIMIT": ("group_by", "Narrow the local-day window or coarsen the bucket.", False),
    "QUERY_RESULT_TOO_LARGE": ("page.limit", "Lower page.limit or select fewer fields.", False),
    "QUERY_CURSOR_INVALID": ("page.after", "Send next_cursor unchanged with the same query.", False),
    "QUERY_CURSOR_STALE": ("page.after", "The collection changed; restart from the first page.", False),
    "QUERY_UNSUPPORTED": ("query", "Use only the operations the query-engine chapter lists.", False),
    "QUERY_CAPABILITY_UNAVAILABLE": ("name", "Use name collections or import.", False),
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
    return finding.as_dict()


def _profile(raw) -> str:
    """The session profile, read before validation; an invalid value is reported by validation."""
    profile = raw.get("execution_profile") if isinstance(raw, dict) else None
    return profile if profile in validation.PROFILES else "interactive"


def run(writer, selector: str, raw, *, facade_profile: str) -> dict:
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
        with session._manifest(collection_id) as (manifest, basis, _, _):
            links = frozenset(name for name, spec in manifest.schema.fields.items()
                              if _LinkProjector.carries_links(spec))
            declaration = typed_rows.declaration(manifest, basis, projected=links)
        if links:
            # No cold vault walk inside the query deadline: a bare-title or memory link that needs the
            # candidate index is omitted, as on the legacy bounded-latency path.
            session.project_with(_LinkProjector.create(writer.root, manifest, policy=operation.policy,
                                                       allow_cold_index=False, authorize_path=operation.allows_file),
                                 fields=links)
        result = validation.normalize_query(raw, declarations={collection_id: declaration}, collection=collection_id)
        if result.findings:
            raise Refusal(result.findings[0])
        query = result.query
        if query.mode == "compose":
            # Compose reads no values, but refuses every shape and profile the other modes refuse.
            if query.aggregate is None:
                typed_rows.check_shape(replace(query, mode="execute"), manifest, basis)
            else:
                reductions.check_shape(query, manifest, limits)
            return {"source": asdict(query.source), "query_version": query.version,
                    "schema_version": manifest.schema.version, "mode": "compose",
                    "kind": "rows" if query.aggregate is None else "groups",
                    "fingerprint": cursors._hash(replace(query, page=replace(query.page, after=None))),
                    "execution_profile": limits.profile, "bounds": limits.bounds()}
        if query.aggregate is not None:
            return session.reduce(query, as_of=query.as_of)
        if query.mode == "execute":
            return cursors.execute_page(session, query, as_of=query.as_of)
        return _admitted_rows(session, query, limits)


def _admitted_rows(session, query, limits) -> dict:
    """Explain, preview or dry-run a row query: admission and preparation, no result rows."""
    as_of = query.as_of or dt.datetime.now(dt.UTC).isoformat()
    admitted = session.admit_query(replace(query, mode="execute", page=replace(query.page, after=None)),
                                   as_of=as_of)
    declared = dict(zip(admitted.projection.index_names, (index.name for index in admitted.projection.indexes),
                        strict=True))
    seek = admitted.uniform and admitted.compiled.usable_index in declared
    result = {"source": asdict(query.source), "query_version": query.version,
              "schema_version": admitted.schema_version, "mode": query.mode, "as_of": admitted.compiled.as_of,
              "execution_profile": limits.profile, "bounds": limits.bounds(),
              "plan": {"kind": "rows", "release": "uniform" if admitted.uniform else "admitted",
                       "access": "index" if seek else "admitted_scan",
                       "index": declared[admitted.compiled.usable_index] if seek else None,
                       "fields": list(admitted.fields)}}
    if query.mode == "explain":
        return result
    # Admission's charge against bounds.max_row_visits: a bound on the work, not a count of rows.
    if query.mode == "preview":
        return {**result, "visit_charge": admitted.estimated_visits}
    return {**result, "admitted": True, "visit_charge": admitted.estimated_visits}


def contract() -> dict:
    """The short query section of store-mode ``describe``: the route and where its grammar is."""
    return {"route": {"tool": "record_memory", "args": {"action": "query", "collection": "<collection>",
                                                         "query": {"version": VERSION}}},
            "grammar": {"tool": "schema_memory",
                        "args": {"subject": "query-engine", "operation": "inspect", "name": "collections"}},
            "modes": list(validation.MODES), "execution_profiles": list(validation.PROFILES)}


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
                "route": {"tool": "record_memory", "args": {"action": "import", "collection": "<collection>",
                                                             "import_request": {"mode": "preview"}}},
                "contract": importer.contract(),
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
                                "against bounds.max_row_visits), admitted_rows or rollup_buckets for groups",
                     "dry_run": "admission and preparation without result rows",
                     "execute": "the default; one page of rows or groups"},
            "execution_profile": {name: {**bounds, **({"concurrent_per_store": runtime.MAX_ANALYTICS_SESSIONS}
                                                      if name == "analytics" else {})}
                                  for name, bounds in runtime.PROFILES.items()},
            "select": f"up to {limits['select']} declared fields; default every declared field",
            "where": {"leaf": {"field": "<declared field>", "op": sorted(validation._OPS), "value": "<typed>"},
                      "combine": ["all", "any", "not"], "max_leaves": limits["predicate_leaves"],
                      "max_depth": limits["boolean_depth"], "max_in_values": limits["membership_values"]},
            "order_by": f"up to {limits['order_by']} of {{field, direction: asc|desc, nulls: first|last}}",
            "group_by": {"max": limits["group_by"], "key": {"field": "<declared field>",
                                                             "bucket": list(validation.BUCKETS),
                                                             "from": "inclusive local date, bucketed key only",
                                                             "to": "inclusive local date, bucketed key only"}},
            "aggregates": {"max": limits["aggregates"], "alias": {"op": ["count", "sum", "avg", "min", "max",
                                                                         "latest"], "field": "<declared field>"}},
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
            "local_day": "a bucketed datetime uses its declared offset field; without one, its own numeric offset",
            "links": "a link to a page the caller cannot read is omitted from the row",
        },
        "errors": {"shape": ["code", "at", "expected", "allowed", "repair", "retryable"],
                   "codes": sorted({"QUERY_FIELD_UNKNOWN", "QUERY_FIELD_UNAVAILABLE", "QUERY_KEY_UNKNOWN",
                                    "QUERY_VALUE_INVALID",
                                    "QUERY_INPUT_LIMIT", "QUERY_ARGUMENT_CONFLICT", "QUERY_UNAVAILABLE",
                                    *_REPAIRS})},
        "unavailable": [
            "joins, having, text and graph",
            "percentile and distinct_count",
            "where on a grouped query: use group_by[n].from/to",
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
