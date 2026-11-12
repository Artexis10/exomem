"""`relation.type` request building and Dreamer scheduling (the relation sensor).

The producer takes the plain-link cohort the relation review queue lists, from
published graph rows only: body wikilinks (origin `wikilink`, the same indexed
evidence the queue's `wikilink` method reads) and authored relation bullets
whose relation is the registry's plain link or generic (origin
`markdown_relation`). A pair that already carries a specific authored relation
is skipped. The tick never loads a model. It parses only selected pages for
their excerpts with the shared Markdown grammar. Queue reads
use graph rows and never parse page text.

Each source page is one replaceable scope (owner `relation-sensor`), one owned
request per link, keyed by source path, target path and occurrence anchor.
Submission writes the page's `relation_sensed` rows with its scope replacement.
New inputs land pending; unchanged inputs keep their verdict after a revisit
or job retirement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import epistemic_graph as epistemic_graph_module
from . import (
    markdown_regions,
    relation_queue,
    relation_registry,
    sensed_model,
    sensing,
    sensing_llm,
)
from . import vault as vault_module
from .markdown_relations import (
    bullet_marker_length,
    heading_key,
    iter_headings,
    scan_markdown_relations,
)

log = logging.getLogger(__name__)

OWNER = "relation-sensor"

#: Bounds per sensed tick; constants of the producer, never settings.
PAGES_PER_TICK = 8
LINKS_PER_PAGE = 8
#: How many pending or superseded rows one tick re-projects.
INGEST_BATCH = 256

_EXCERPT_CHARS = 900
_LINK_CHARS = 300

class _GraphPage:
    """A page as the published graph describes it: no Markdown parse."""

    def __init__(self, rel_path: str, title: str, page_type: str | None,
                 frontmatter: Mapping[str, Any], source_hash: str) -> None:
        self.rel_path = rel_path
        self.title = title
        self.page_type = page_type
        self.frontmatter = dict(frontmatter)
        self.source_hash = source_hash


def _structural_frontmatter(metadata: str | None) -> dict[str, Any]:
    try:
        structural = json.loads(str(metadata or "{}")).get(
            epistemic_graph_module.STRUCTURAL_METADATA
        ) or {}
        raw = structural.get("frontmatter_yaml")
        loaded = yaml.safe_load(str(raw)) if raw else {}
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _page_from_row(row: tuple | None) -> _GraphPage | None:
    if row is None:
        return None
    path, title, page_type, metadata, source_hash = row
    return _GraphPage(
        str(path),
        str(title or Path(str(path)).stem),
        str(page_type) if page_type else None,
        _structural_frontmatter(str(metadata)),
        str(source_hash or ""),
    )


def _file_node(conn: sqlite3.Connection, rel_path: str) -> _GraphPage | None:
    """One file node by its graph-owned primary key."""
    return _page_from_row(
        conn.execute(
            "SELECT path, title, page_type, metadata, source_hash FROM graph_nodes "
            "WHERE node_key=?",
            (epistemic_graph_module._file_key(rel_path),),
        ).fetchone()
    )


def _plain(text: str) -> str:
    # Shared vault grammar owns wikilink tokens; CommonMark owns code spans.
    code = markdown_regions.code_spans(text)
    pieces: list[str] = []
    end = 0
    for match in vault_module.iter_body_wikilinks(text):
        if not markdown_regions.outside(text, match.start(), code):
            continue
        name, _bar, alias = match.group(0)[2:-2].partition("|")
        target, _hash, _fragment = name.partition("#")
        pieces.extend((text[end:match.start()], (alias or target.rsplit("/", 1)[-1]).strip()))
        end = match.end()
    pieces.append(text[end:])
    return " ".join("".join(pieces).split())


def _page_raw(vault_root: Path, rel_path: str, expected_hash: str) -> str | None:
    """A page's raw text when it matches the hash the graph published.

    The producer may read page files for its chosen sources and their link
    targets only; the read is honourable only while it describes the same
    bytes the graph hashed (`source_hash` is `content_hash` of the raw text).
    A mismatch means the graph is behind the file: the page is skipped and
    retried once the graph catches up.
    """
    try:
        raw = (Path(vault_root) / rel_path).read_text(encoding="utf-8")
    except OSError:
        return None
    if vault_module.content_hash(raw) != expected_hash:
        return None
    return raw


def _body_lines(vault_root: Path, page: _GraphPage) -> list[str] | None:
    """The page's body lines, or None while the file is ahead of the graph."""
    raw = _page_raw(vault_root, page.rel_path, page.source_hash)
    if raw is None:
        return None
    _frontmatter, body, _span = vault_module.parse_frontmatter(raw)
    return unicodedata.normalize("NFC", str(body)).splitlines()


def excerpt(vault_root: Path, page: _GraphPage, registry: relation_registry.RelationRegistry,
            lines: list[str] | None = None) -> str:
    """`page-excerpt-v1`: title, then body minus relation statements, links
    rendered as display text, whitespace-collapsed, cut at 900 chars."""
    if lines is None:
        lines = _body_lines(vault_root, page) or []
    drop = {
        candidate.line - 1
        for candidate in scan_markdown_relations("\n".join(lines), include_legacy=True).candidates
        if candidate.canonical or candidate.has_colon
        or registry.resolve(candidate.raw_kind or "").canonical is not None
    }
    headings = {index: (level, title) for index, level, title in iter_headings(lines)}
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    first_heading = headings.get(first)
    title_line = (
        first if first_heading and first_heading[0] == 1
        and heading_key(first_heading[1]) == heading_key(page.title) else None
    )
    relation_level = None
    kept = []
    for index, line in enumerate(lines):
        heading = headings.get(index)
        if heading is not None:
            level, title = heading
            if relation_level is not None and level <= relation_level:
                relation_level = None
            if relation_level is None and heading_key(title) == heading_key("Relations"):
                relation_level = level
        if relation_level is None and index not in drop and index != title_line:
            kept.append(line)
    body = _plain("\n".join(kept))
    return f"{page.title}\n{body[:_EXCERPT_CHARS]}"


def link_line(lines: Sequence[str], number: int | None) -> str:
    """`link-sentence-v1`: the carrying line, plain, 300 chars; "" for a bare bullet."""
    if number is None or not (1 <= int(number) <= len(lines)):
        return ""
    line = lines[int(number) - 1]
    return _plain(line[bullet_marker_length(line):])[:_LINK_CHARS]


def menu_for(
    fit: relation_queue.RelationFit,
    registry: relation_registry.RelationRegistry,
    source: _GraphPage,
    target: _GraphPage,
    candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The `relation-menu-v1` entries, from the same call the queue makes.

    The candidate is judged as the generic relation — the menu a `retype` or
    generic item offers and a wikilink item's proposal menu.
    """
    return fit.options(
        dict(candidate),
        registry=registry,
        source_page=source,
        target_page=target,
        relation=relation_registry.generic_relation_key(),
    )


@dataclass
class _Link:
    kind: str          # "wikilink" | "retype"
    target: str        # target page rel path
    anchor: str        # occurrence anchor (wikilink occurrence / bullet line)
    line: int | None   # body line carrying the link (for link-sentence)
    relation: str      # authored relation key (retype) or plain link (wikilink)
    bullet: str | None = None  # exact bullet line for retype evidence


def request_key(source: str, target: str, anchor: str) -> str:
    return hashlib.sha256(
        json.dumps([source, target, anchor], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def _is_plain(registry: relation_registry.RelationRegistry, key: str) -> bool:
    definition = registry.definition(key)
    # A refinement of the core plain link still carries a specific authored meaning.
    return registry.is_generic(key) or (
        definition is not None and definition.core and registry.has(key, "plain_link")
    )


def plain_links(
    conn: sqlite3.Connection,
    registry: relation_registry.RelationRegistry,
    source: _GraphPage,
) -> list[_Link]:
    """At most LINKS_PER_PAGE candidates, suppressed in SQL before the limit."""
    links: list[_Link] = []
    plain = sorted(
        key for key in registry.keys
        if _is_plain(registry, key)
    )
    marks = ",".join("?" for _ in plain)
    # Resolve authored labels once per selected page, including registry aliases.
    authored = (
        "WITH authored AS MATERIALIZED (SELECT dst_page_key, raw_relation, "
        "source_anchor, metadata, exomem_sensor_relation(raw_relation) AS canonical "
        "FROM graph_edges WHERE source_path=? AND src_key=('file:' || ?) "
        "AND origin='markdown_relation') "
    )
    conn.create_function(
        "exomem_sensor_relation", 1,
        lambda raw: registry.resolve(str(raw or "")).canonical,
    )
    try:
        rows = conn.execute(
            authored + "SELECT d.path, e.review_evidence FROM graph_edges e "
            "JOIN graph_nodes d ON d.node_key=e.dst_key AND d.kind='file' "
            "WHERE e.origin='wikilink' AND e.source_path=? "
            "AND e.dst_key NOT IN (SELECT dst_page_key FROM authored "
            f"WHERE canonical IS NOT NULL AND canonical NOT IN ({marks})) "
            "ORDER BY COALESCE(CAST(json_extract(e.review_evidence, "
            "'$.internal.occurrence') AS INTEGER), e.rowid), d.path, e.rowid LIMIT ?",
            (source.rel_path, source.rel_path, source.rel_path, *plain, LINKS_PER_PAGE),
        ).fetchall()
        bullets = []
        if len(rows) < LINKS_PER_PAGE:
            bullets = conn.execute(
                authored + ", ranked AS (SELECT *, "
                "CAST(substr(source_anchor, 6) AS INTEGER) AS line, "
                "ROW_NUMBER() OVER (PARTITION BY dst_page_key ORDER BY "
                "CAST(substr(source_anchor, 6) AS INTEGER), canonical, "
                "COALESCE(json_extract(metadata, '$.line'), '')) AS rank "
                f"FROM authored WHERE canonical IN ({marks}) "
                "AND dst_page_key NOT IN (SELECT dst_page_key FROM authored "
                f"WHERE canonical IS NOT NULL AND canonical NOT IN ({marks})) "
                "AND dst_page_key NOT IN (SELECT dst_key FROM graph_edges "
                "WHERE source_path=? AND origin='wikilink')) "
                "SELECT line, canonical, dst_page_key, metadata FROM ranked "
                "WHERE rank=1 ORDER BY line, canonical, dst_page_key, "
                "COALESCE(json_extract(metadata, '$.line'), '') LIMIT ?",
                (source.rel_path, source.rel_path, *plain, *plain, source.rel_path,
                 LINKS_PER_PAGE - len(rows)),
            ).fetchall()
    finally:
        conn.create_function("exomem_sensor_relation", 1, None)
    for target, raw_evidence in rows:
        try:
            internal = json.loads(str(raw_evidence or "{}")).get("internal") or {}
        except (ValueError, TypeError):
            internal = {}
        target = str(target)
        links.append(
            _Link(
                "wikilink",
                target,
                f"occurrence-{internal.get('occurrence', -1)}",
                int(internal["line"]) if internal.get("line") is not None else None,
                registry.link_key,
            )
        )
    for number, canonical, target_key, metadata in bullets:
        target = str(target_key).removeprefix("file:")
        raw_line = str(json.loads(str(metadata or "{}")).get("line") or "")
        links.append(_Link("retype", target, f"line-{number}", None, canonical,
                           bullet=raw_line))
    return links


def _slot(unit_ref: str, path: str, slot: str, extractor: str, text: str,
          anchor: str | None = None) -> sensing.InputSlot:
    return sensing.InputSlot(
        unit_ref=unit_ref,
        path=path,
        slot=slot,
        extractor=extractor,
        text_sha256=sensing.text_sha256(text),
        anchor=anchor,
    )


def build_requests(
    vault_root: Path,
    conn: sqlite3.Connection,
    fit: relation_queue.RelationFit,
    source: _GraphPage,
    links: Sequence[_Link],
) -> list[tuple[str, sensing.SenseRequest, dict[str, Any]]]:
    """`(request_key, request, row)` per link. `row` is the relation_sensed row."""
    registry = fit.registry(source)
    body = _body_lines(vault_root, source)
    if body is None:
        return []  # file ahead of the graph: skipped, retried on a later tick
    source_text = excerpt(vault_root, source, registry, lines=body)
    out: list[tuple[str, sensing.SenseRequest, dict[str, Any]]] = []
    for link in links[:LINKS_PER_PAGE]:
        target = _file_node(conn, link.target)
        if target is None:
            continue
        target_lines = _body_lines(vault_root, target)
        if target_lines is None:
            continue  # target missing or ahead of the graph: only this link drops
        target_text = excerpt(vault_root, target, fit.registry(target), lines=target_lines)
        link_text = link_line(body, link.line) if link.kind == "wikilink" else ""
        candidate = {
            "from": source.rel_path,
            "to": link.target,
            "relation_type": link.relation,
            "method": link.kind,
        }
        menu = menu_for(fit, registry, source, target, candidate)
        menu_text = sensing_llm.menu_slot_text(
            {"key": o["key"], "label": o["label"], "definition": o["description"]}
            for o in menu
        )
        key = request_key(source.rel_path, link.target, link.anchor)
        inputs = (
            _slot(source.rel_path, source.rel_path, "source", "page-excerpt-v1", source_text),
            _slot(target.rel_path, target.rel_path, "target", "page-excerpt-v1", target_text),
            _slot(source.rel_path, source.rel_path, "link", "link-sentence-v1", link_text,
                  anchor=link.anchor),
            _slot(source.rel_path, source.rel_path, "menu", "relation-menu-v1", menu_text),
        )
        texts = (source_text, target_text, link_text, menu_text)
        try:
            request = sensing.SenseRequest(sensing.RELATION_TYPE, 2, inputs, texts)
        except ValueError:
            log.debug("relation sensor: malformed request skipped", exc_info=True)
            continue
        row = {
            "request_key": key,
            "source_path": source.rel_path,
            "target_path": link.target,
            "anchor": link.anchor,
            "kind": link.kind,
            "menu_digest": sensing.text_sha256(menu_text),
            "input_key": request.input_key,
            "source_hash": source.source_hash,
            "target_hash": target.source_hash,
        }
        out.append((key, request, row))
    return out


def page_scope(
    vault_root: Path, conn: sqlite3.Connection, fit: relation_queue.RelationFit,
    source: _GraphPage,
) -> sensed_model.Submission:
    """Submit one replaceable scope of relation.type requests for a page.

    The page's `relation_sensed` rows are replaced in the same transaction as
    its jobs: new inputs land pending, and unchanged inputs keep their verdicts.
    """
    registry = fit.registry(source)
    links = plain_links(conn, registry, source)
    requests = build_requests(vault_root, conn, fit, source, links)
    token = sensed_model.capture_scope(vault_root, OWNER, source.rel_path)
    if not isinstance(token, sensed_model.ScopeToken):
        return token
    return sensed_model.submit_scope(
        vault_root,
        token,
        (
            # The worker drains jobs in order-key order. Keying by source path
            # senses pages in the order the relation queue presents them,
            # rather than in request-hash order.
            sensed_model.OwnedRequest(
                request_key=key, request=request, priority=2,
                order_key=f"{source.rel_path}\n{key}"
            )
            for key, request, _row in requests
        ),
        relation_rows=(row for _key, _request, row in requests),
    )


# ----------------------------------------------------------------------
# the tick producer
# ----------------------------------------------------------------------


def produce(
    vault_root: Path,
    graph: sqlite3.Connection,
    *,
    cursor: str,
    changed: Sequence[str],
    halt: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """One bounded pass: this tick's changed pages first, then the cursor.

    `graph` is a graph read snapshot. Submissions go through the producer
    connection, so this must run OUTSIDE the projection's write transaction;
    the caller applies `processed`/`removed`/`cursor` to the tables inside it.
    """
    stop = halt() if halt is not None else None
    if stop is not None:
        return {"submitted": 0, "pages": [], "cursor": cursor, "stop": stop}
    chosen = list(dict.fromkeys(str(path) for path in changed))[:PAGES_PER_TICK]
    cursor_paths: list[str] = []
    remaining = PAGES_PER_TICK
    remaining -= len(chosen)
    if remaining > 0:
        marks = ",".join("?" for _ in chosen)
        excluded = f"AND node_key NOT IN ({marks}) " if chosen else ""
        # The graph schema's closed file: namespace excludes every non-file node from this range.
        rows = graph.execute(
            "SELECT path FROM graph_nodes WHERE node_key GLOB 'file:*' AND node_key > ? " + excluded +
            "ORDER BY node_key LIMIT ?",
            (epistemic_graph_module._file_key(cursor),
             *(epistemic_graph_module._file_key(path) for path in chosen), remaining),
        ).fetchall()
        wrapped = len(rows) < remaining
        for (path,) in rows:
            cursor_paths.append(str(path))
        remaining -= len(rows)
        if wrapped and remaining > 0:
            seen = [*chosen, *cursor_paths]
            marks = ",".join("?" for _ in seen)
            excluded = f"AND node_key NOT IN ({marks}) " if seen else ""
            rows = graph.execute(
                "SELECT path FROM graph_nodes WHERE node_key GLOB 'file:*' AND node_key <= ? " + excluded +
                "ORDER BY node_key LIMIT ?",
                (epistemic_graph_module._file_key(cursor),
                 *(epistemic_graph_module._file_key(path) for path in seen), remaining),
            ).fetchall()
            for (path,) in rows:
                cursor_paths.append(str(path))
    chosen.extend(cursor_paths)
    if not chosen:
        return {"submitted": 0, "pages": [], "cursor": cursor}

    fit = relation_queue.RelationFit(Path(vault_root))
    submitted = 0
    processed: list[tuple[str, str]] = []
    last_cursor = None
    for path in chosen:
        stop = halt() if halt is not None else None
        if stop is not None:
            break
        page = _file_node(graph, path)
        if page is None:
            continue
        result = page_scope(vault_root, graph, fit, page)
        if isinstance(result, sensed_model.Submission) and result.state in (
            "submitted", "partial"
        ):
            submitted += result.count
            processed.append((path, page.source_hash))
        elif isinstance(result, sensed_model.Submission) and result.state == "seeding":
            break
        if path in cursor_paths:
            last_cursor = path

    # The cursor advances past the deepest path this pass reached; a pass that
    # covered the whole table wraps so the next pass starts at the first path.
    if last_cursor is not None:
        tail = graph.execute(
            "SELECT 1 FROM graph_nodes WHERE node_key GLOB 'file:*' AND node_key > ? "
            "ORDER BY node_key LIMIT 1", (epistemic_graph_module._file_key(last_cursor),)
        ).fetchone()
        cursor = "" if tail is None else last_cursor
    return {
        "submitted": submitted,
        "pages": processed,
        "cursor": cursor,
        "stop": stop,
    }


def ingest(conn: sqlite3.Connection, ledger: sqlite3.Connection | None) -> int:
    """Fill pending or superseded `relation_sensed` verdicts, incrementally.

    Rows are written at submission as pending (verdict NULL). One tick fills at
    most INGEST_BATCH of them from the ledger, and clears verdicts whose
    instrument left the active set. Rows are never wiped wholesale.
    """
    from . import sensing_ledger

    active = sensing_llm.active_instruments()
    entry = sensing.QUESTIONS[sensing.RELATION_TYPE]
    inactive = ",".join("?" for _ in active) or "''"
    # A persisted keyset cursor walks the pending rows over ticks, so rows
    # beyond the first INGEST_BATCH fill too and no row starves the rest.
    cursor = str(sensed_model._get_meta(conn, "relation_ingest_cursor") or "")
    rows = conn.execute(
        "SELECT request_key, input_key, instrument_id FROM relation_sensed "
        f"WHERE (verdict IS NULL OR instrument_id NOT IN ({inactive})) "
        "AND request_key > ? ORDER BY request_key LIMIT ?",
        (*active, cursor, INGEST_BATCH),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT request_key, input_key, instrument_id FROM relation_sensed "
            f"WHERE (verdict IS NULL OR instrument_id NOT IN ({inactive})) "
            "ORDER BY request_key LIMIT ?",
            (*active, INGEST_BATCH),
        ).fetchall()
    if rows:
        sensed_model._set_meta(conn, "relation_ingest_cursor", str(rows[-1][0]))
    stored = (
        sensing_ledger.by_input_keys(
            ledger, sensing.RELATION_TYPE, (str(row[1]) for row in rows), instrument_ids=active,
        )
        if rows and ledger is not None and active else {}
    )
    filled = 0
    for request_key, input_key, old_instrument in rows:
        verdict = reading = instrument = None
        if ledger is not None and active:
            for instrument_id in active:
                reading = next(
                    (r for r in stored.get(str(input_key), ()) if r.instrument_id == instrument_id),
                    None,
                )
                if reading is not None:
                    break
            if reading is not None:
                verdict = sensing.rederive(
                    reading.vectors,
                    reading.verdict,
                    entry.label_map_version,
                    columns=reading.columns,
                    vector_names=entry.vector_names,
                )
                instrument = str(reading.instrument_id)
        if verdict is not None:
            conn.execute(
                "UPDATE relation_sensed SET verdict=?, p=?, reading_id=?, instrument_id=? "
                "WHERE request_key=?",
                (verdict.label, verdict.p, reading.reading_id, instrument, request_key),
            )
        elif old_instrument is not None:
            conn.execute(
                "UPDATE relation_sensed SET verdict=NULL, p=NULL, reading_id=NULL, "
                "instrument_id=NULL WHERE request_key=?",
                (request_key,),
            )
        else:
            continue
        filled += 1
    return filled


# ----------------------------------------------------------------------
# queue reads: sensed verdicts by request key, indexed by source path
# ----------------------------------------------------------------------


def sensed_rows_for(
    vault_root: Path, sources: Sequence[str], *, graph: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    """Current rows: each replaceable source scope holds at most LINKS_PER_PAGE.

    One graph lookup binds every returned row to its source and target hashes.
    """
    active = sensing_llm.active_instruments()
    if not sources or not active:
        return {}
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return {}
    source_marks = ",".join("?" for _ in sources)
    instrument_marks = ",".join("?" for _ in active)
    try:
        try:
            rows = conn.execute(
                "SELECT request_key, source_path, target_path, anchor, kind, "
                "reading_id, instrument_id, verdict, p, menu_digest, source_hash, target_hash "
                f"FROM relation_sensed WHERE source_path IN ({source_marks}) "
                f"AND instrument_id IN ({instrument_marks})",
                (*map(str, sources), *active),
            ).fetchall()
        except sqlite3.Error:
            return {}
    finally:
        conn.close()
    if not rows:
        return {}
    owned_graph = graph is None
    if owned_graph:
        graph = epistemic_graph_module.EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if graph is None:
        return {}
    try:
        paths = sorted({str(row[index]) for row in rows for index in (1, 2)})
        path_marks = ",".join("?" for _ in paths)
        hashes = dict(graph.execute(
            "SELECT path, source_hash FROM graph_nodes INDEXED BY idx_graph_nodes_path "
            f"WHERE kind='file' AND path IN ({path_marks})", paths,
        ).fetchall())
    finally:
        if owned_graph:
            graph.close()
    return {
        str(key): {
            "request_key": key,
            "source_path": src,
            "target_path": tgt,
            "anchor": anchor,
            "kind": kind,
            "reading_id": rid,
            "instrument_id": iid,
            "verdict": verdict,
            "p": p,
            "menu_digest": digest,
            "source_hash": source_hash,
            "target_hash": target_hash,
        }
        for key, src, tgt, anchor, kind, rid, iid, verdict, p, digest, source_hash, target_hash in rows
        if hashes.get(src) == source_hash and hashes.get(tgt) == target_hash
    }


def sensed_for_candidate(
    vault_root: Path, source: str, target: str, kind: str | None = None,
    *, graph: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """One current verdict from the source's bounded, hash-checked scope."""
    rows = [
        row for row in sensed_rows_for(vault_root, [source], graph=graph).values()
        if row["target_path"] == target and (kind is None or row["kind"] == kind)
    ]
    if len(rows) != 1:
        return None
    return rows[0]


def proposal(row: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The queue's `sensed` block for a verdict row, gated on the live menu."""
    verdict = str(row.get("verdict") or "")
    if row.get("instrument_id") not in sensing_llm.active_instruments():
        return None
    if verdict in ("none", "abstain"):
        return None
    digest = hashlib.sha256(
        sensing_llm.menu_slot_text(
            {"key": o["key"], "label": o["label"], "definition": o["description"]}
            for o in options
        ).encode("utf-8")
    ).hexdigest()
    if digest != str(row.get("menu_digest") or ""):
        return None
    if verdict not in {str(o["key"]) for o in options}:
        return None
    return {
        "relation": verdict,
        "p": row.get("p"),
        "reading_id": row.get("reading_id"),
        "instrument_id": row.get("instrument_id"),
        "proposed": True,
    }


def retype_candidates(
    conn: sqlite3.Connection,
    source_path: str,
    registry: relation_registry.RelationRegistry,
    rows: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Authored plain-link/generic bullets with a current relation verdict."""
    return retype_candidates_many(conn, {source_path: registry}, rows).get(source_path, [])


def retype_candidates_many(
    conn: sqlite3.Connection,
    registries: Mapping[str, relation_registry.RelationRegistry],
    rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """One bounded query for fresh retype rows, retaining exact bullet evidence.

    Each producer scope contributes at most LINKS_PER_PAGE requested anchors.
    """
    out: dict[str, list[dict[str, Any]]] = {source: [] for source in registries}
    requested = [
        (str(row["source_path"]), f"file:{row['target_path']}", str(row.get("anchor") or ""))
        for row in rows.values()
        if row.get("source_path") in registries and row.get("kind") == "retype"
        and row.get("verdict") and row["verdict"] not in ("none", "abstain")
    ]
    if not requested:
        return out
    requested_marks = ",".join("(?,?,?)" for _ in requested)
    for source_path, dst_page_key, raw_relation, anchor, metadata in conn.execute(
        f"WITH requested(source_path, target_key, anchor) AS (VALUES {requested_marks}) "
        "SELECT e.source_path, e.dst_page_key, e.raw_relation, e.source_anchor, e.metadata "
        "FROM requested r JOIN graph_edges e ON e.source_path=r.source_path "
        "AND e.dst_page_key=r.target_key AND e.source_anchor=r.anchor "
        "WHERE e.origin='markdown_relation' AND e.src_key=('file:' || r.source_path)",
        [value for pair in requested for value in pair],
    ):
        registry = registries[source_path]
        resolved = registry.resolve(str(raw_relation or ""))
        canonical = resolved.canonical
        if canonical is None or not _is_plain(registry, canonical):
            continue
        anchor_text = str(anchor or "")
        target = str(dst_page_key or "").removeprefix("file:")
        key = request_key(source_path, target, anchor_text)
        row = rows.get(key)
        if row is None or str(row.get("verdict") or "") in ("none", "abstain"):
            continue
        raw_line = ""
        try:
            raw_line = str(json.loads(str(metadata or "{}")).get("line") or "")
        except (ValueError, TypeError):
            pass
        candidate = {
            "from": source_path,
            "to": target,
            "relation_type": canonical,
            "method": "retype",
            "evidence": {"line": raw_line, "anchor": anchor_text},
            "_sensed": row,
        }
        out[source_path].append(candidate)
    return out
