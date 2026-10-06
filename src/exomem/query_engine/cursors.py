"""Opaque stateless continuation over freshly admitted typed row queries."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import closing
from dataclasses import asdict, is_dataclass, replace

from cryptography.fernet import Fernet, InvalidToken

from ..collection_store import query_freshness
from . import ir, typed_sql
from .runtime import _MAX_DECODE_BYTES, QueryError
from .scalars import parse_instant
from .typed_rows import AdmittedQuery, execute_rows

_MAX_TOKEN_BYTES = 4096
_MAX_RESULT_BYTES = 64 * 1024
_TOKEN_KEYS = frozenset({"v", "query", "principal", "schema", "authorization", "visible", "lineage", "as_of", "boundary"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(asdict(value) if is_dataclass(value) else value).encode()).hexdigest()


class CursorCodec:
    """An internal issuer, not a public parameter or an authorization grant."""

    def __init__(self, key):
        self._cipher = Fernet(key)

    def mint(self, binding, *, as_of, boundary):
        payload = {"v": 1, **binding, "as_of": as_of, "boundary": boundary}
        token = self._cipher.encrypt(_json(payload).encode()).decode("ascii")
        if len(token) > _MAX_TOKEN_BYTES:
            raise QueryError("QUERY_RESULT_TOO_LARGE")
        return token

    def open(self, token):
        try:
            if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_BYTES:
                raise ValueError
            payload = json.loads(self._cipher.decrypt(token.encode("ascii")))
            if (not isinstance(payload, dict) or set(payload) != _TOKEN_KEYS or type(payload["v"]) is not int or payload["v"] != 1
                    or not isinstance(payload["boundary"], str) or not 0 < len(payload["boundary"]) <= 128
                    or any(not isinstance(payload[name], str) or len(payload[name]) != 64
                           for name in ("query", "principal", "schema", "authorization", "visible", "lineage"))):
                raise ValueError
            parse_instant(payload["as_of"])
            return payload
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise QueryError("QUERY_CURSOR_INVALID") from error

    @staticmethod
    def verify(payload, binding):
        if any(payload[name] != binding[name] for name in ("query", "principal")):
            raise QueryError("QUERY_CURSOR_INVALID")
        if any(payload[name] != binding[name] for name in ("schema", "authorization", "visible", "lineage")):
            raise QueryError("QUERY_CURSOR_STALE")


def _codec(session):
    session.check()
    row = session.connection.execute("SELECT value FROM store_meta WHERE key='query_cursor_key'").fetchone()
    if row is None:
        raise QueryError("QUERY_UNAVAILABLE")
    try:
        return CursorCodec(row[0].encode("ascii"))
    except (ValueError, TypeError, UnicodeError) as error:
        raise QueryError("QUERY_UNAVAILABLE") from error


def _visible(admitted, dependencies):
    if admitted.uniform:
        basis = query_freshness.uniform_basis(admitted.session.connection, admitted.query.source.ref, dependencies)
        if basis is None:
            raise QueryError("QUERY_UNAVAILABLE")
        return _hash(basis)
    session = admitted.session
    count = admitted.released_count
    if count is None or count + session._estimated_visits > session.limits.max_row_visits:
        raise QueryError("QUERY_COST_LIMIT")
    session._estimated_visits += count
    digest = hashlib.sha256(b"exomem.typed-visible-dependencies.v1\0")
    ids = f"query_ids_{admitted.ordinal}"
    cursor = session.connection.execute(
        f"SELECT i.row_id,CASE WHEN {admitted.membership_sql} THEN i.item_key END "
        f"FROM temp.{ids} a CROSS JOIN main.items i WHERE i.row_id=a.row_id ORDER BY a.row_id",
    )
    with closing(session.fetch(cursor)) as batches:
        for batch in batches:
            for row_id, key in batch:
                session.check()
                if key is None:
                    raise QueryError("QUERY_COST_LIMIT")
                try:
                    values = session.selected_values(
                        row_id, admitted.layout, set(dependencies) - {"item_key"},
                        max_bytes=min(_MAX_DECODE_BYTES, session.limits.max_temp_bytes), check=session.check,
                    )
                    selected = [(path, path in values, values.get(path)) for path in sorted(dependencies) if path != "item_key"]
                    digest.update(_json([key, selected]).encode() + b"\n")
                except (ValueError, TypeError, RecursionError) as error:
                    raise QueryError("QUERY_COST_LIMIT") from error
                session.check()
    return digest.hexdigest()


def _binding(admitted: AdmittedQuery):
    admitted.check()
    operation = admitted.session._authorization
    principal = operation.who
    dependencies = set(admitted.compiled.dependency_paths) | set(admitted.fields)
    lineage = admitted.session.connection.execute(
        "SELECT key,value FROM store_meta WHERE key IN ('store_id','instance_id','lineage','forks') ORDER BY key",
    ).fetchall()
    return {"query": _hash(replace(admitted.query, page=replace(admitted.query.page, after=None))),
            "principal": _hash([principal.audience_id, principal.surface, principal.purpose,
                                principal.authorization_session_id, principal.issuer_family]),
            "schema": _hash(admitted.schema_identity),
            "authorization": _hash([operation.policy.fingerprint, operation.access_fingerprint, operation.purpose]),
            "visible": _visible(admitted, dependencies), "lineage": _hash(lineage)}


def _resume(admitted, payload):
    """Recover small boundary identity only after fresh binding verification."""
    session, conn = admitted.session, admitted.session.connection
    columns = ",".join(column for column, _ in admitted.compiled.order_terms)
    membership = admitted.membership_sql.replace("i.row_id", "p.row_id")
    boundary = conn.execute(
        f"SELECT {columns} FROM {admitted.table} p WHERE p.item_key=? AND {membership}",
        (payload["boundary"],),
    ).fetchone()
    if boundary is None:
        raise QueryError("QUERY_CURSOR_STALE")
    compiled = typed_sql.compile_rows(admitted.query, admitted.projection,
                                     as_of=payload["as_of"], after=boundary)
    cost = 0
    if not compiled.page_bound:
        count = admitted.released_count
        if count is None:
            count = conn.execute("SELECT count(*) FROM (SELECT 1 FROM items WHERE collection_id=? LIMIT ?)",
                                 (admitted.query.source.ref, session.limits.max_row_visits + 1)).fetchone()[0]
        cost = count * 2
        if cost + session._estimated_visits > session.limits.max_row_visits:
            raise QueryError("QUERY_COST_LIMIT")
    session._estimated_visits += cost
    resumed = replace(admitted, compiled=compiled, estimated_visits=admitted.estimated_visits + cost)
    session._queries[admitted.ordinal] = resumed
    return resumed


def execute_page(session, query, *, as_of=None):
    """A dark leaf; no tool route, field-classified release or migration enabled."""
    session.check()
    if not isinstance(query, ir.Query):
        raise QueryError("QUERY_UNSUPPORTED")
    codec = _codec(session)
    payload = codec.open(query.page.after) if query.page.after is not None else None
    frozen = parse_instant(payload["as_of"] if payload else as_of or query.as_of or dt.datetime.now(dt.UTC).isoformat()).isoformat()
    if payload is not None and as_of is not None and parse_instant(as_of).isoformat() != frozen:
        raise QueryError("QUERY_CURSOR_INVALID")
    base = replace(query, page=replace(query.page, after=None))
    admitted = session.admit_query(base, as_of=frozen)
    binding = _binding(admitted)
    if payload is not None:
        codec.verify(payload, binding)
        admitted = _resume(admitted, payload)
    page = execute_rows(admitted)
    boundary_position = next(n for n, (column, _) in enumerate(admitted.compiled.order_terms) if column == "p.item_key")
    token = codec.mint(binding, as_of=admitted.compiled.as_of, boundary=page.last_order_key[boundary_position]) if page.has_more else None
    result = {"rows": page.rows, "returned": len(page.rows), "has_more": page.has_more,
              "next_cursor": token, "total": page.total, "as_of": admitted.compiled.as_of,
              "source": asdict(query.source), "query_version": query.version,
              "schema_version": admitted.schema_version,
              "schema_fingerprint": binding["schema"],
              "truncated": page.has_more,
              "truncation_reason": ("bytes" if len(page.rows) < query.page.limit else "limit") if page.has_more else None}
    session.check()
    if len(_json(result).encode()) > _MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    session.check()
    return result
