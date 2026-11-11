"""`relation.type` request building and Dreamer scheduling (the relation sensor).

The producer takes the plain-link cohort the relation review queue lists, from
published graph rows only: body wikilinks (origin `wikilink`, the same indexed
evidence the queue's `wikilink` method reads) and authored relation bullets
whose relation is the registry's plain link or generic (origin
`markdown_relation`). A pair that already carries a specific authored relation
is skipped. The tick never loads a model and never runs a Markdown parser:
page text is read as raw bytes and cut by line, and relation-statement lines
come from the graph's own recorded anchors, never from a word match.

Each source page is one replaceable scope (owner `relation-sensor`), one owned
request per link, keyed by source path, target path and occurrence anchor.
Submission writes the page's `relation_sensed` rows in the same transaction as
the scope replacement, so a row is pending from the moment it is requested and
keeps its verdict after the job retires.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import (
    epistemic_graph as epistemic_graph_module,
    relation_queue,
    relation_registry,
    semantic_contract as semantic_contract_module,
    sensed_model,
    sensing,
    sensing_llm,
    vault as vault_module,
)

log = logging.getLogger(__name__)

OWNER = "relation-sensor"

#: Bounds per sensed tick; constants of the producer, never settings.
PAGES_PER_TICK = 8
LINKS_PER_PAGE = 8
MENU_LIMIT = 12
#: How many pending or superseded rows one tick re-projects.
INGEST_BATCH = 256

_EXCERPT_CHARS = 900
_LINK_CHARS = 300

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_SPACE = re.compile(r"\s+")

#: The canonical Relations section heading, the same literal the governed
#: write uses (`relation_queue.accept`'s `heading="Relations"`).
_RELATIONS_HEADING = "relations"


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
    """One file node by path — the only page lookup the producer makes."""
    return _page_from_row(
        conn.execute(
            "SELECT path, title, page_type, metadata, source_hash FROM graph_nodes "
            "WHERE kind='file' AND path=?",
            (rel_path,),
        ).fetchone()
    )


def _kind(fit: relation_queue.RelationFit, page: _GraphPage) -> Any:
    """`RelationFit._kind` for a graph-described page (no file read)."""
    resolved = fit._basis.resolve(
        semantic_contract_module.normalized_compiled_type(page.page_type)
    )
    if resolved.note_type is None:
        return None
    from . import relation_endpoints

    return relation_endpoints.page_kind(
        resolved.note_type.role, page.frontmatter.get("entity_type"), fit._types()
    )


def _plain(text: str) -> str:
    rendered = _WIKILINK.sub(
        lambda m: (m.group(2) or m.group(1).rsplit("/", 1)[-1]).strip(), text
    )
    return _SPACE.sub(" ", rendered).strip()


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


def _relation_lines(conn: sqlite3.Connection, rel_path: str) -> set[int]:
    """Line numbers carrying authored relation statements, from graph anchors."""
    lines: set[int] = set()
    for (anchor,) in conn.execute(
        "SELECT source_anchor FROM graph_edges WHERE origin='markdown_relation' "
        "AND src_key=('file:' || ?)",
        (rel_path,),
    ):
        anchor = str(anchor or "")
        if anchor.startswith("line-"):
            try:
                lines.add(int(anchor[5:]))
            except ValueError:
                continue
    return lines


def _section_lines(conn: sqlite3.Connection, rel_path: str) -> set[int]:
    """Line range of the canonical Relations section, from the graph's blocks."""
    out: set[int] = set()
    for title, start, end in conn.execute(
        "SELECT title, line_start, line_end FROM graph_nodes WHERE path=? AND "
        "line_start IS NOT NULL AND line_end IS NOT NULL AND kind='candidate'",
        (rel_path,),
    ):
        if str(title or "").strip().casefold() != _RELATIONS_HEADING:
            continue
        try:
            out.update(range(int(start), int(end) + 1))
        except (TypeError, ValueError):
            continue
    return out


def excerpt(vault_root: Path, conn: sqlite3.Connection, page: _GraphPage,
            lines: list[str] | None = None) -> str:
    """`page-excerpt-v1`: title, then body minus relation statements, links
    rendered as display text, whitespace-collapsed, cut at 900 chars."""
    if lines is None:
        lines = _body_lines(vault_root, page) or []
    drop = _relation_lines(conn, page.rel_path) | _section_lines(conn, page.rel_path)
    body = _plain("\n".join(
        line for index, line in enumerate(lines, start=1) if index not in drop
    ))
    title = page.title
    if body.casefold().startswith(("# " + title).casefold()):
        body = body[len(title) + 2:].strip()
    elif body.casefold().startswith(title.casefold()):
        body = body[len(title):].strip()
    return f"{title}\n{body[:_EXCERPT_CHARS]}"


def link_line(lines: Sequence[str], number: int | None) -> str:
    """`link-sentence-v1`: the carrying line, plain, 300 chars; "" for a bare bullet."""
    if number is None or not (1 <= int(number) <= len(lines)):
        return ""
    return _plain(lines[int(number) - 1]).lstrip("-*0123456789. ").strip()[:_LINK_CHARS]


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


def plain_links(
    conn: sqlite3.Connection,
    registry: relation_registry.RelationRegistry,
    source: _GraphPage,
) -> list[_Link]:
    """A page's plain-link cohort from published rows, queue order."""
    links: list[_Link] = []
    seen_targets: set[str] = set()
    rows = conn.execute(
        "SELECT d.path, e.review_evidence FROM graph_edges e "
        "JOIN graph_nodes d ON d.node_key = e.dst_key AND d.kind = 'file' "
        "WHERE e.origin = 'wikilink' AND e.source_path = ? "
        "ORDER BY COALESCE(CAST(json_extract(e.review_evidence, "
        "'$.internal.occurrence') AS INTEGER), e.rowid), d.path, e.rowid",
        (source.rel_path,),
    ).fetchall()
    specific: set[str] = set()
    bullets: list[tuple[int, str, str, str]] = []  # line, canonical, target, raw
    for dst_page_key, raw_relation, anchor, metadata in conn.execute(
        "SELECT dst_page_key, raw_relation, source_anchor, metadata FROM graph_edges "
        "WHERE origin='markdown_relation' AND src_key=('file:' || ?)",
        (source.rel_path,),
    ):
        target = str(dst_page_key or "").removeprefix("file:")
        resolved = registry.resolve(str(raw_relation or ""))
        canonical = resolved.canonical
        if canonical is None:
            continue
        anchor_text = str(anchor or "")
        number = int(anchor_text[5:]) if anchor_text.startswith("line-") else 0
        raw_line = ""
        try:
            raw_line = str(json.loads(str(metadata or "{}")).get("line") or "")
        except (ValueError, TypeError):
            pass
        if canonical == registry.link_key or registry.is_generic(canonical):
            bullets.append((number, canonical, target, raw_line))
        elif registry.is_specific(canonical):
            specific.add(target)
    for target, raw_evidence in rows:
        try:
            internal = json.loads(str(raw_evidence or "{}")).get("internal") or {}
        except (ValueError, TypeError):
            internal = {}
        target = str(target)
        seen_targets.add(target)
        links.append(
            _Link(
                "wikilink",
                target,
                f"occurrence-{internal.get('occurrence', -1)}",
                int(internal["line"]) if internal.get("line") is not None else None,
                registry.link_key,
            )
        )
    for number, canonical, target, raw_line in sorted(bullets):
        if target in seen_targets:
            continue
        seen_targets.add(target)
        links.append(_Link("retype", target, f"line-{number}", None, canonical,
                           bullet=raw_line))
    return [link for link in links if link.target not in specific]


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
    source_text = excerpt(vault_root, conn, source, lines=body)
    out: list[tuple[str, sensing.SenseRequest, dict[str, Any]]] = []
    for link in links[:LINKS_PER_PAGE]:
        target = _file_node(conn, link.target)
        if target is None:
            continue
        target_lines = _body_lines(vault_root, target)
        if target_lines is None:
            continue  # target missing or ahead of the graph: only this link drops
        target_text = excerpt(vault_root, conn, target, lines=target_lines)
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
        }
        out.append((key, request, row))
    return out


def page_scope(
    vault_root: Path, conn: sqlite3.Connection, fit: relation_queue.RelationFit,
    source: _GraphPage,
) -> sensed_model.Submission:
    """Submit one replaceable scope of relation.type requests for a page.

    The page's `relation_sensed` rows are replaced in the same transaction as
    its jobs: every request lands as a pending row, so a verdict survives the
    job's retirement.
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
                request_key=key, request=request, order_key=f"{source.rel_path}\n{key}"
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
) -> dict[str, Any]:
    """One bounded pass: this tick's changed pages first, then the cursor.

    `graph` is a graph read snapshot. Submissions go through the producer
    connection, so this must run OUTSIDE the projection's write transaction;
    the caller applies `processed`/`removed`/`cursor` to the tables inside it.
    """
    chosen: list[str] = []
    remaining = PAGES_PER_TICK
    chosen.extend(str(path) for path in changed[:remaining])
    remaining -= len(chosen)
    if remaining > 0:
        rows = graph.execute(
            "SELECT path FROM graph_nodes WHERE kind='file' AND path > ? "
            "ORDER BY path LIMIT ?",
            (cursor, remaining),
        ).fetchall()
        wrapped = len(rows) < remaining
        for (path,) in rows:
            chosen.append(str(path))
        remaining -= len(rows)
        if wrapped and remaining > 0:
            rows = graph.execute(
                "SELECT path FROM graph_nodes WHERE kind='file' AND path <= ? "
                "ORDER BY path LIMIT ?",
                (cursor, remaining),
            ).fetchall()
            for (path,) in rows:
                if str(path) not in set(chosen):
                    chosen.append(str(path))
    chosen = chosen[:PAGES_PER_TICK]
    if not chosen:
        return {"submitted": 0, "pages": [], "cursor": cursor}

    fit = relation_queue.RelationFit(Path(vault_root))
    submitted = 0
    processed: list[tuple[str, str]] = []
    for path in chosen:
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

    # The cursor advances past the deepest path this pass reached; a pass that
    # covered the whole table wraps so the next pass starts at the first path.
    last = chosen[-1]
    tail = graph.execute(
        "SELECT 1 FROM graph_nodes WHERE kind='file' AND path > ? LIMIT 1", (last,)
    ).fetchone()
    return {
        "submitted": submitted,
        "pages": processed,
        "cursor": "" if tail is None else last,
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
        "WHERE (verdict IS NULL OR instrument_id NOT IN (%s)) "
        "AND request_key > ? ORDER BY request_key LIMIT ?" % inactive,
        (*active, cursor, INGEST_BATCH),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT request_key, input_key, instrument_id FROM relation_sensed "
            "WHERE (verdict IS NULL OR instrument_id NOT IN (%s)) "
            "ORDER BY request_key LIMIT ?" % inactive,
            (*active, INGEST_BATCH),
        ).fetchall()
    if rows:
        sensed_model._set_meta(conn, "relation_ingest_cursor", str(rows[-1][0]))
    filled = 0
    for request_key, input_key, old_instrument in rows:
        verdict = reading = instrument = None
        if ledger is not None and active:
            stored = sensing_ledger.by_input_key(
                ledger, sensing.RELATION_TYPE, str(input_key)
            )
            for instrument_id in active:
                reading = next(
                    (r for r in stored if r.instrument_id == instrument_id),
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
    vault_root: Path, sources: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """`request_key -> row` of current relation verdicts for the given sources."""
    if not sources:
        return {}
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return {}
    try:
        try:
            rows = conn.execute(
                "SELECT request_key, source_path, target_path, anchor, kind, "
                "reading_id, instrument_id, verdict, p, menu_digest "
                "FROM relation_sensed WHERE source_path IN (%s)"
                % ",".join("?" for _ in sources),
                tuple(str(s) for s in sources),
            ).fetchall()
        except sqlite3.Error:
            return {}
    finally:
        conn.close()
    return {
        str(key): {
            "source_path": src,
            "target_path": tgt,
            "anchor": anchor,
            "kind": kind,
            "reading_id": rid,
            "instrument_id": iid,
            "verdict": verdict,
            "p": p,
            "menu_digest": digest,
        }
        for key, src, tgt, anchor, kind, rid, iid, verdict, p, digest in rows
    }


def sensed_for_candidate(
    vault_root: Path, source: str, target: str, kind: str | None = None
) -> dict[str, Any] | None:
    """One source+target's current verdict row, without reading the table."""
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                "SELECT request_key, source_path, target_path, anchor, kind, "
                "reading_id, instrument_id, verdict, p, menu_digest "
                "FROM relation_sensed WHERE source_path=? AND target_path=?",
                (str(source), str(target)),
            ).fetchall()
        except sqlite3.Error:
            return None
    finally:
        conn.close()
    if kind is not None:
        rows = [row for row in rows if str(row[4]) == kind]
    if len(rows) != 1:
        return None
    key, src, tgt, anchor, k, rid, iid, verdict, p, digest = rows[0]
    return {
        "request_key": key,
        "source_path": src,
        "target_path": tgt,
        "anchor": anchor,
        "kind": k,
        "reading_id": rid,
        "instrument_id": iid,
        "verdict": verdict,
        "p": p,
        "menu_digest": digest,
    }


def proposal(row: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The queue's `sensed` block for a verdict row, gated on the live menu."""
    verdict = str(row.get("verdict") or "")
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
    """Authored plain-link/generic bullets with a current relation verdict.

    New candidates, method `retype`, only while their reading is a relation
    verdict; evidence names the exact bullet line. `rows` is the caller's
    `sensed_rows_for` result for this source.
    """
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for dst_page_key, raw_relation, anchor, metadata in conn.execute(
        "SELECT dst_page_key, raw_relation, source_anchor, metadata FROM graph_edges "
        "WHERE origin='markdown_relation' AND src_key=('file:' || ?)",
        (source_path,),
    ):
        resolved = registry.resolve(str(raw_relation or ""))
        canonical = resolved.canonical
        if canonical is None or not (
            canonical == registry.link_key or registry.is_generic(canonical)
        ):
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
        out.append(candidate)
    return out
