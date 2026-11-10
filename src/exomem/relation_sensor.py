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
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from dataclasses import dataclass

from . import (
    epistemic_graph as epistemic_graph_module,
    relation_options,
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

_EXCERPT_CHARS = 900
_LINK_CHARS = 300

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
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


def _file_nodes(conn) -> dict[str, _GraphPage]:
    out: dict[str, _GraphPage] = {}
    for path, title, page_type, metadata, source_hash in conn.execute(
        "SELECT path, title, page_type, metadata, source_hash FROM graph_nodes WHERE kind='file'"
    ):
        out[str(path)] = _GraphPage(
            str(path),
            str(title or Path(str(path)).stem),
            str(page_type) if page_type else None,
            _structural_frontmatter(str(metadata)),
            str(source_hash or ""),
        )
    return out


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


def _body_lines(vault_root: Path, rel_path: str) -> list[str]:
    """The page's body lines from its raw bytes (frontmatter split, no parse)."""
    try:
        raw = (Path(vault_root) / rel_path).read_text(encoding="utf-8")
    except OSError:
        return []
    _frontmatter, body, _span = vault_module.parse_frontmatter(raw)
    return unicodedata.normalize("NFC", str(body)).splitlines()


def _relation_lines(conn, rel_path: str) -> set[int]:
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


def _section_lines(conn, rel_path: str) -> set[int]:
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


def excerpt(vault_root: Path, conn, page: _GraphPage, lines: list[str] | None = None) -> str:
    """`page-excerpt-v1`: title, then body minus relation statements, links
    rendered as display text, whitespace-collapsed, cut at 900 chars."""
    if lines is None:
        lines = _body_lines(vault_root, page.rel_path)
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
) -> list[dict[str, Any]]:
    """The `relation-menu-v1` entries: exactly the queue's options for the pair."""
    return relation_options.ranked_options(
        registry,
        _kind(fit, source),
        _kind(fit, target),
        fit._types(),
        accepts=lambda key: relation_queue._usable_relation(registry, key, source, target)
        is not None,
        limit=MENU_LIMIT,
    )


@dataclass
class _Link:
    kind: str          # "wikilink" | "retype"
    target: str        # target page rel path
    anchor: str        # occurrence anchor (wikilink occurrence / bullet line)
    line: int | None   # body line carrying the link (for link-sentence)
    bullet: str | None = None  # exact bullet line for retype evidence


def plain_links(
    conn,
    registry: relation_registry.RelationRegistry,
    source: _GraphPage,
) -> list[_Link]:
    """A page's plain-link cohort from published rows, queue order, queue bound."""
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
    bullets: list[tuple[int, str, str, str]] = []  # line, kind-key, target, raw
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
        if registry.is_specific(canonical):
            specific.add(target)
        elif canonical == registry.link_key or registry.is_generic(canonical):
            bullets.append((number, canonical, target, raw_line))
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
            )
        )
    for number, _canonical, target, raw_line in sorted(bullets):
        if target in seen_targets:
            continue
        seen_targets.add(target)
        links.append(_Link("retype", target, f"line-{number}", None, bullet=raw_line))
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
    conn,
    fit: relation_queue.RelationFit,
    source: _GraphPage,
    links: Sequence[_Link],
) -> list[tuple[str, sensing.SenseRequest]]:
    """`(request_key, request)` per link. Raises nothing; skips unknown targets."""
    registry = fit.registry(source)
    pages = _file_nodes(conn)
    body = _body_lines(vault_root, source.rel_path)
    source_text = excerpt(vault_root, conn, source, lines=body)
    out: list[tuple[str, sensing.SenseRequest]] = []
    for link in links[:LINKS_PER_PAGE]:
        target = pages.get(link.target)
        if target is None:
            continue
        target_body = _body_lines(vault_root, target.rel_path)
        target_text = excerpt(vault_root, conn, target, lines=target_body)
        link_text = link_line(body, link.line) if link.kind == "wikilink" else ""
        menu = menu_for(fit, registry, source, target)
        menu_text = sensing_llm.menu_slot_text(
            {"key": o["key"], "label": o["label"], "definition": o["description"]}
            for o in menu
        )
        request_key = hashlib.sha256(
            json.dumps(
                [source.rel_path, link.target, link.anchor],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        inputs = (
            _slot(source.rel_path, source.rel_path, "source", "page-excerpt-v1", source_text),
            _slot(target.rel_path, target.rel_path, "target", "page-excerpt-v1", target_text),
            _slot(source.rel_path, source.rel_path, "link", "link-sentence-v1", link_text,
                  anchor=link.anchor),
            _slot(source.rel_path, source.rel_path, "menu", "relation-menu-v1", menu_text),
        )
        texts = (source_text, target_text, link_text, menu_text)
        try:
            request = sensing.SenseRequest(
                sensing.RELATION_TYPE, 2, inputs, texts
            )
        except ValueError:
            log.debug("relation sensor: malformed request skipped", exc_info=True)
            continue
        out.append((request_key, request))
    return out


def page_scope(
    vault_root: Path, conn, fit: relation_queue.RelationFit, source: _GraphPage
) -> sensed_model.Submission:
    """Submit one replaceable scope of relation.type requests for a page."""
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
            sensed_model.OwnedRequest(request_key=key, request=request, order_key=key)
            for key, request in requests
        ),
    )


# ----------------------------------------------------------------------
# the tick producer
# ----------------------------------------------------------------------


def produce(
    vault_root: Path,
    conn,
    seen: Mapping[str, str],
) -> dict[str, int]:
    """One bounded pass: changed pages first, then the persisted cursor.

    `conn` is a graph read snapshot; `seen` is the dreamer's path -> signature
    map. Costs at most PAGES_PER_TICK pages and LINKS_PER_PAGE links per page.
    """
    store_conn = sensed_model.open_readonly(vault_root)
    if store_conn is None:
        return {"submitted": 0, "pages": 0}
    try:
        if sensed_model._get_meta(store_conn, "seeding") is not None:
            return {"submitted": 0, "pages": 0}
        cursor = sensed_model._get_meta(store_conn, "relation_cursor") or ""
        hashes = sensed_model._get_meta(store_conn, "relation_seen_hashes") or {}
    finally:
        store_conn.close()

    pages = _file_nodes(conn)
    changed = [
        path
        for path, page in pages.items()
        if seen.get(path) is not None and page.source_hash != hashes.get(path)
    ]
    ordered_paths = sorted(pages)
    behind = [p for p in ordered_paths if p > str(cursor)]
    front = [p for p in ordered_paths if p <= str(cursor)]
    order = sorted(changed) + behind + front
    chosen: list[str] = []
    seen_order = set(changed)
    for path in order:
        if len(chosen) >= PAGES_PER_TICK:
            break
        chosen.append(path)
        seen_order.add(path)
    if not chosen:
        return {"submitted": 0, "pages": 0}

    fit = relation_queue.RelationFit(Path(vault_root))
    submitted = 0
    new_hashes = dict(hashes)
    for path in chosen:
        page = pages[path]
        result = page_scope(vault_root, conn, fit, page)
        if isinstance(result, sensed_model.Submission) and result.state in (
            "submitted", "partial"
        ):
            submitted += result.count
            new_hashes[path] = page.source_hash
        elif isinstance(result, sensed_model.Submission) and result.state == "seeding":
            break

    # A drained pass wraps the cursor so the next pass starts at the first path.
    last = chosen[-1]
    wrapped = len(chosen) >= len(ordered_paths) or last == ordered_paths[-1]
    conn2 = sensed_model._producer_connection(vault_root)
    try:
        conn2.execute("BEGIN IMMEDIATE")
        sensed_model._set_meta(conn2, "relation_cursor", "" if wrapped else last)
        sensed_model._set_meta(conn2, "relation_seen_hashes", new_hashes)
        conn2.execute("COMMIT")
    except Exception:  # noqa: BLE001 - cursor loss re-reads, never corrupts
        conn2.execute("ROLLBACK")
        log.debug("relation sensor: cursor not persisted", exc_info=True)
    finally:
        conn2.close()
    return {"submitted": submitted, "pages": len(chosen)}


def ingest(conn, ledger) -> int:
    """Project current readings into `relation_sensed`; drop stale rows.

    `conn` is the projection write connection, `ledger` a ledger read. A row
    stands only while its request is still queued and its reading answers the
    active instrument set for `relation.type`.
    """
    from . import sensing_ledger

    active = sensed_model._question_active(sensing.RELATION_TYPE)
    entry = sensing.QUESTIONS[sensing.RELATION_TYPE]
    rows = 0
    conn.execute("DELETE FROM relation_sensed")
    if ledger is None:
        return rows
    for job in sensed_model.iter_jobs(conn, question_type=sensing.RELATION_TYPE):
        request = job.request
        stored = sensing_ledger.by_input_key(
            ledger, sensing.RELATION_TYPE, request.input_key
        )
        for instrument_id in active:
            reading = next(
                (r for r in stored if r.reading_id == request.reading_id(instrument_id)),
                None,
            )
            if reading is None:
                continue
            verdict = sensing.rederive(
                reading.vectors,
                reading.verdict,
                entry.label_map_version,
                columns=reading.columns,
                vector_names=entry.vector_names,
            )
            if verdict is None:
                continue
            slots = {item.slot: item for item in request.inputs}
            source = slots.get("source")
            target = slots.get("target")
            link = slots.get("link")
            menu = slots.get("menu")
            anchor = link.anchor if link is not None else None
            kind = "retype" if str(anchor or "").startswith("line-") else "wikilink"
            conn.execute(
                "INSERT OR REPLACE INTO relation_sensed VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.request_key,
                    source.path if source is not None else "",
                    target.path if target is not None else "",
                    anchor,
                    kind,
                    reading.reading_id,
                    instrument_id,
                    verdict.label,
                    verdict.p,
                    menu.text_sha256 if menu is not None else "",
                    request.input_key,
                ),
            )
            rows += 1
            break
    return rows


# ----------------------------------------------------------------------
# queue reads: sensed verdicts by request key
# ----------------------------------------------------------------------


def sensed_rows(vault_root: Path) -> dict[str, dict[str, Any]]:
    """`request_key -> row` of current relation verdicts, or {} when absent."""
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return {}
    try:
        try:
            rows = conn.execute(
                "SELECT request_key, source_path, target_path, anchor, kind, "
                "reading_id, instrument_id, verdict, p, menu_digest "
                "FROM relation_sensed"
            ).fetchall()
        except Exception:  # noqa: BLE001 - an older projection has no table
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


def request_key(source: str, target: str, anchor: str) -> str:
    return hashlib.sha256(
        json.dumps([source, target, anchor], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def retype_candidates(
    vault_root: Path,
    conn,
    source_path: str,
    registry: relation_registry.RelationRegistry,
) -> list[dict[str, Any]]:
    """Authored plain-link/generic bullets with a current relation verdict.

    New candidates, method `retype`, only while their reading is a relation
    verdict; evidence names the exact bullet line.
    """
    rows = sensed_rows(vault_root)
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
        if row is None:
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
            "_sensed_row": row,
        }
        out.append(candidate)
    return out
