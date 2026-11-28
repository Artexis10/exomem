"""Formats for the owner's relationship projection and portable relation files."""

from __future__ import annotations

import hashlib
import html
import json
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from io import BytesIO
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from sqlite3 import Connection, IntegrityError

from .. import relation_registry
from ..cli_ops import OpError
from ..held_fs import Chunks
from ..reserved_paths import RELATIONS_FILE_SUFFIX
from . import relationships as store
from .relationships import BoundRelationship, Relationship, UnitIdentity

FORMAT = "evermatter-relations"
VERSION = 1
_MARKER = b"<!-- evermatter:relations"
_OPEN = _MARKER + b" sha256="
_CLOSE = b"<!-- evermatter:relations-end -->"
_END = b" -->"
# These tokens delimit the locator rows emitted by projection_lines.
_LOCATOR_SEPARATOR = " · "
_LOCATOR_DIRECTIONS = ("to", "from")
MAX_BLOCK_BYTES = 64 * 1024
MAX_RECORD_BYTES = 64 * 1024


def path_for(page: str) -> str:
    return page + RELATIONS_FILE_SUFFIX


def source_path(path: str) -> str | None:
    if not path.casefold().endswith(RELATIONS_FILE_SUFFIX):
        return None
    page = path[:-len(RELATIONS_FILE_SUFFIX)]
    return page if page.casefold().endswith(".md") else None


def splice_block(source: bytes, lines: Iterable[str]) -> bytes:
    """Append a self-verifying projection with a removable leading separator."""
    # An added LF after a bare CR would look like a two-byte separator to the stripper.
    newline = b"\r\n" if b"\r\n" in source or source.endswith(b"\r") else b"\n"
    content = b"".join(line.encode("utf-8") + newline for line in lines)
    if not content:
        return source
    digest = hashlib.sha256(content).hexdigest().encode("ascii")
    block = newline + _OPEN + digest + _END + newline + content + _CLOSE + newline
    if len(block) > MAX_BLOCK_BYTES:
        raise OpError("RELATIONS_BLOCK_TOO_LARGE", "the managed relations block exceeds its bound")
    return source + block


def strip_block(source: bytes, *, keep_mine: bool = False) -> bytes:
    """Remove a valid block, or an edited block with intact markers on keep-mine."""
    return _split_block(source, keep_mine=keep_mine)[0]


def _split_block(source: bytes, *, keep_mine: bool) -> tuple[bytes, bytes, bytes]:
    """Return the authored body, exact projection block and its rendered lines."""
    if _MARKER not in source:
        return source, b"", b""
    opens = []
    closes = []
    offset = 0
    for line in source.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n")
        end = offset + len(line)
        if marker == _CLOSE:
            closes.append((offset, end))
        elif marker.startswith(_MARKER):
            opens.append((offset, end, marker))
        offset = end
    if not opens and not closes:
        return source, b"", b""
    if len(opens) != 1 or len(closes) != 1:
        raise _damaged_markers()
    start, inner_start, marker = opens[0]
    inner_end, end = closes[0]
    if inner_start > inner_end or not marker.startswith(_OPEN) or not marker.endswith(_END):
        raise _damaged_markers()
    separator = b"\r\n" if source.endswith(b"\r\n", 0, start) else b"\n"
    if not start or not source.endswith(separator, 0, start):
        raise _damaged_markers()
    expected = marker[len(_OPEN):-len(_END)]
    if not keep_mine and (
        end - start > MAX_BLOCK_BYTES
        or hashlib.sha256(source[inner_start:inner_end]).hexdigest().encode("ascii") != expected
    ):
        raise OpError("RELATIONS_BLOCK_EDITED", "the managed relations block was edited")
    cut = start - len(separator)
    return source[:cut] + source[end:], source[cut:end], source[inner_start:inner_end]


def _damaged_markers() -> OpError:
    return OpError(
        "RELATIONS_BLOCK_DAMAGED", "the managed relations block markers are damaged",
        "Choose keep-store to regenerate the relations block.",
    )


def editor_body(
    tx: Connection, page: str, source: bytes, *, keep_mine: bool = False,
) -> tuple[bytes, bool]:
    """Remove a marked block and refuse surviving projection text with no reliable boundary."""
    body, block, rendered_body = _split_block(source, keep_mine=keep_mine)
    locator_rows = Counter(_locator_rows(body))
    prior = tx.execute(
        "SELECT p.body, m.sha256, m.canonical_sha256, p.sha256 "
        "FROM (SELECT ? AS path) AS item "
        "LEFT JOIN store_pages AS p ON p.path = item.path "
        "LEFT JOIN store_manifest AS m ON m.path = item.path "
        "WHERE m.canonical_sha256 IS NOT NULL OR (? AND p.path IS NOT NULL)",
        (page, bool(locator_rows)),
    ).fetchone()
    before = b"" if prior is None or prior[0] is None else prior[0]
    had_block = prior is not None and prior[2] is not None
    if body == before:
        return body, had_block
    if locator_rows - Counter(_locator_rows(before)):
        raise _damaged_markers()
    if not had_block:
        return body, False
    assert prior is not None
    if prior[2] == prior[3]:
        if block and hashlib.sha256(before + block).hexdigest() == prior[1]:
            _check_projection_fragments(before, body, rendered_body.splitlines())
            return body, True
        rendered = tuple(projection_lines(tx, page))
        projected = splice_block(before, rendered)
        if projected != before and hashlib.sha256(projected).hexdigest() == prior[1]:
            _check_projection_fragments(before, body, (row.encode() for row in rendered))
            return body, True
    raise OpError(
        "RELATIONS_BLOCK_STALE", "the last managed relations block cannot be verified",
        "Choose keep-store to save the outside file separately and regenerate the block.",
    )


def projection_witness_available(tx: Connection) -> bool:
    """Standby readers can open a vault before its writer installs the projection witness."""
    return tx.execute(
        "SELECT 1 FROM pragma_table_xinfo('store_manifest') WHERE name = 'canonical_sha256'",
    ).fetchone() is not None


def _locator(line: bytes) -> bytes | None:
    """Read the fixed punctuation and direction of a generated locator row."""
    anchor, separator, detail = line.partition(_LOCATOR_SEPARATOR.encode())
    if not separator or not anchor.startswith(b"- "):
        return None
    kind, _, reference = detail.partition(b" ")
    if kind:
        for direction in _LOCATOR_DIRECTIONS:
            prefix = f"{direction} ".encode()
            if reference.startswith(prefix):
                return reference[len(prefix):]
    return None


def _locator_rows(body: bytes) -> Iterator[bytes]:
    if _LOCATOR_SEPARATOR.encode() in body:
        for line in _body_rows(body):
            if _locator(line):
                yield line


def has_locator_rows(body: bytes) -> bool:
    return next(_locator_rows(body), None) is not None


def body_from_witness(source: bytes, before: bytes, projected_sha256: str) -> bytes:
    """Release the authored bytes of a held file after authenticating its marked block."""
    body, block, rendered = _split_block(source, keep_mine=False)
    if body == before:
        return body
    if not block or hashlib.sha256(before + block).hexdigest() != projected_sha256:
        raise _damaged_markers()
    if Counter(_locator_rows(body)) - Counter(_locator_rows(before)):
        raise _damaged_markers()
    _check_projection_fragments(before, body, rendered.splitlines())
    return body


def _check_projection_fragments(before: bytes, body: bytes, rendered: Iterable[bytes]) -> None:
    """Compare with the previous authored bytes after proving the rendered projection."""
    rendered_rows = set(rendered)
    old_rows = Counter(row for row in _body_rows(before) if row in rendered_rows)
    new_rows = Counter(row for row in _body_rows(body) if row in rendered_rows)
    if any(new_rows[row] > old_rows[row] for row in rendered_rows):
        raise _damaged_markers()
    for row in rendered_rows:
        locator = _locator(row)
        if locator is not None:
            path, _, anchor = locator.partition(b"#")
            if (anchor and body.count(locator) > before.count(locator)) or (
                b"/" in path and body.count(path) > before.count(path)
            ):
                raise _damaged_markers()


def _body_rows(body: bytes) -> Iterator[bytes]:
    # Normalize line delimiters for comparison without materializing all the rows.
    for line in BytesIO(body.replace(b"\r", b"\n")):
        yield line.strip()


@dataclass(frozen=True, slots=True)
class PortableRelations:
    source: str
    revision: int
    units: tuple[UnitIdentity, ...]
    relationships: tuple[Relationship, ...]
    target_units: tuple[UnitIdentity, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedFile:
    data: Chunks
    sha256: str


@contextmanager
def prepare_file(tx: Connection, page: str, revision: int) -> Iterator[PreparedFile | None]:
    """Spool the paged SQLite result for a held, repeatable filesystem publication."""
    from itertools import chain

    units = store.units_on(tx, page)
    first = next(units, None)
    if first is None:
        yield None
        return
    with tempfile.SpooledTemporaryFile(max_size=256 * 1024, mode="w+b") as stream:
        digest = hashlib.sha256()
        for line in lines(page, revision, chain((first,), units), store.outgoing_on(tx, page)):
            digest.update(line)
            stream.write(line)
        yield PreparedFile(Chunks(lambda: _parts(stream)), digest.hexdigest())


def _parts(stream: tempfile.SpooledTemporaryFile[bytes]) -> Iterator[bytes]:
    stream.seek(0)
    while part := stream.read(256 * 1024):
        yield part


def lines(
    source: str, revision: int, units: Iterable[UnitIdentity], relationships: Iterable[BoundRelationship],
) -> Iterator[bytes]:
    """Stream one versioned relations file with all the source's durable identities."""
    yield _line({"format": FORMAT, "version": VERSION, "source": source, "revision": revision})
    for unit in units:
        yield _line({"unit": asdict(unit)})
    for bound in relationships:
        yield _line({
            "relationship": asdict(bound.relationship),
            "target_unit": asdict(bound.target) if bound.target is not None else None,
        })


def projection_lines(tx: Connection, page: str) -> Iterator[str]:
    """Describe the highest counts and a small, deterministic sample from one snapshot."""
    row = tx.execute("SELECT frontmatter_json FROM store_pages WHERE path=?", (page,)).fetchone()
    if row is None:
        return
    parent_ref = store.page_reference(row[0])
    groups = store.projection_groups(tx, page, parent_ref, 41)
    if not groups:
        return
    yield "### Stored relationships"
    for anchor, kind, incoming, count in groups[:40]:
        direction = "Incoming" if incoming else "Outgoing"
        yield f"- {direction} {_label(kind)} for {_label(anchor or 'page')}: {count}"
    if len(groups) > 40:
        yield "- The relations file carries more relationship groups."
    for anchor, kind, incoming, target, fragment in store.projection_samples(tx, page, parent_ref, 12):
        far = f"{target}#{fragment}" if fragment is not None else target
        direction = _LOCATOR_DIRECTIONS[incoming]
        yield f"- {_label(anchor or 'page')}{_LOCATOR_SEPARATOR}{_label(kind)} {direction} {_label(far)}"


def _label(value: str) -> str:
    # JSON quotes preserve control characters and HTML entities keep the display literal.
    text = json.dumps(value if len(value) <= 64 else value[:63] + "…", ensure_ascii=False)[1:-1]
    return html.escape(text).replace("[", "&#91;").replace("]", "&#93;").replace(chr(96), "&#96;")


def _line(value: object) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) >= MAX_RECORD_BYTES:
        raise _invalid("a relationship record exceeds its size limit")
    return encoded + b"\n"


def _unique(entries: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in entries:
        if key in result:
            raise _invalid("a relationship record repeats a field")
        result[key] = value
    return result


def _object(raw: bytes) -> dict:
    if len(raw) > MAX_RECORD_BYTES:
        raise _invalid("a relationship record exceeds its size limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _invalid("a relationship record is invalid JSON") from error
    if not isinstance(value, dict):
        raise _invalid("a relationship record is not an object")
    return value


def parse(encoded: Iterable[bytes]) -> PortableRelations:
    """Validate the exact supported format before a caller installs any row."""
    records = iter(encoded)
    header = _object(next(records, b""))
    if (set(header) != {"format", "version", "source", "revision"}
            or header["format"] != FORMAT or type(header["version"]) is not int
            or header["version"] != VERSION or not _path(header["source"])
            or type(header["revision"]) is not int or not 0 <= header["revision"] < 2**63):
        raise _invalid("the relations file header or version is invalid")
    source, revision = header["source"], header["revision"]
    units: dict[str, UnitIdentity] = {}
    targets: dict[str, UnitIdentity] = {}
    links: dict[str, Relationship] = {}
    for raw in records:
        entry = _object(raw)
        if set(entry) == {"unit"}:
            unit = _unit(entry["unit"])
            if unit.id in units or unit.parent_path != source:
                raise _invalid("the relations file repeats a unit or names the wrong source")
            units[unit.id] = unit
        elif set(entry) == {"relationship", "target_unit"}:
            relationship = _relationship(entry["relationship"])
            if relationship.id in links:
                raise _invalid("the relations file repeats a relationship ID")
            target = entry["target_unit"]
            if target is not None:
                unit = _unit(target)
                if unit.id != relationship.target_unit_id or targets.setdefault(unit.id, unit) != unit:
                    raise _invalid("a target locator does not match its relationship")
            elif relationship.target_unit_id is not None:
                raise _invalid("a unit relationship is missing its target locator")
            links[relationship.id] = relationship
        else:
            raise _invalid("the relations file contains an unknown record")
    if any(link.source_unit_id not in units for link in links.values()):
        raise _invalid("a relationship's source unit is missing from the relations file")
    return PortableRelations(
        source, revision, tuple(units.values()), tuple(links.values()), tuple(targets.values()),
    )


def plan_import(
    tx: Connection, path: str, decoded: PortableRelations, *,
    actor: str, allowed_type: Callable[[str], bool],
) -> store.Change:
    """Plan an outside edit to an existing projection without reassigning any unit ID."""
    if source_path(path) != decoded.source:
        raise _invalid("the relations file names a different source page")
    units = (*decoded.units, *decoded.target_units)
    known = store.identities_at(tx, {unit.locator for unit in units})
    if any(known.get(unit.locator) != unit for unit in units):
        raise _invalid("a unit locator does not match its stored identity")
    current = {bound.relationship.id: bound.relationship for bound in store.outgoing_on(tx, decoded.source)}
    wanted = {link.id: link for link in decoded.relationships}
    added = []
    retired = []
    for link_id, old in current.items():
        proposal = wanted.pop(link_id, None)
        if proposal == old:
            continue
        successor = None
        if proposal is not None:
            successor = uuid.uuid4().hex
            added.append(replace(proposal, id=successor))
        retired.append(store.Retirement(link_id, successor))
    added.extend(wanted.values())
    if any(not allowed_type(edge.relation_type) for edge in added):
        raise _invalid("an edited relationship uses an unavailable type")
    return store.Change(
        actor, added=tuple(added), retired=tuple(retired),
        projected_base=store.ProjectedBase(path, decoded.source, decoded.revision),
    )


def type_admission(root: Path, tx: Connection, source: str) -> Callable[[str], bool]:
    """Use the registry the canonical source page selects for an editor's change."""
    row = tx.execute("SELECT frontmatter_json FROM store_pages WHERE path=?", (source,)).fetchone()
    registry = _source_registry(root, source, row[0] if row is not None else None)
    return lambda key: registry.definition(key) is not None


def validate_restored_types(root: Path, tx: Connection) -> None:
    """Refuse unknown types for live sources. An absent source has no registry to select."""
    rows = tx.execute(
        "SELECT u.parent_path,p.frontmatter_json,u.parent_ref,r.relation_type FROM store_relationships r"
        " JOIN store_unit_ids u ON u.id=r.source_unit_id JOIN store_pages p ON p.path=u.parent_path"
        " GROUP BY u.parent_path,p.frontmatter_json,u.parent_ref,r.relation_type"
        " ORDER BY u.parent_path,u.parent_ref",
    )
    for (source, stored_fields, parent_ref), group in groupby(rows, key=itemgetter(0, 1, 2)):
        if parent_ref is not None and store.page_reference(stored_fields) != parent_ref:
            continue  # A replacement at the same path cannot supply the orphan's registry.
        registry = _source_registry(root, source, stored_fields)
        for _, _, _, kind in group:
            if registry.definition(kind) is None:
                raise OpError(
                    "INVALID_RELATIONS_FILE",
                    f"{json.dumps(path_for(source))} uses unregistered type {json.dumps(kind)}",
                    "Restore the registry definition or correct the relations file, then migrate again.",
                )


def _source_registry(root: Path, source: str, frontmatter: str | None) -> relation_registry.RelationRegistry:
    return relation_registry.load_for_page(root, source, json.loads(frontmatter) if frontmatter else {})


def restore(tx: Connection, decoded: PortableRelations, *, revision: int) -> None:
    """Restore stable locators and edges inside offline staging, including unresolved ends."""
    locations: dict[store.UnitLocator, UnitIdentity] = {}
    for unit in (*decoded.units, *decoded.target_units):
        if locations.setdefault(unit.locator, unit) != unit:
            raise _invalid("the file assigns two IDs to one unit anchor")
    known = store.identities_at(tx, set(locations))
    if any(locations[locator] != unit for locator, unit in known.items()):
        raise _invalid("the same unit has inconsistent identities in the relations files")
    identities = tuple(unit for locator, unit in locations.items() if locator not in known)
    try:
        store.apply(
            tx, store.Change("store-mode migrate", identities=identities, added=decoded.relationships),
            revision=revision, reason="restore portable relationships",
            at=tx.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0],
        )
    except IntegrityError as error:
        raise _invalid("the relations files reuse a relationship or unit ID") from error


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(mark in value for mark in ("\0", "\r", "\n"))


def _path(value: object) -> bool:
    from .. import reserved_paths

    return isinstance(value, str) and _text(value) and "\\" not in value and not (
        reserved_paths.classify_logical(value).blocked
    ) and value.casefold().endswith(".md")


def _unit(value: object) -> UnitIdentity:
    if (not isinstance(value, dict) or set(value) != {"id", "parent_path", "anchor", "parent_ref"}
            or not _text(value["id"]) or not _path(value["parent_path"]) or not _text(value["anchor"])
            or (value["parent_ref"] is not None and not _text(value["parent_ref"]))):
        raise _invalid("the unit identity has invalid fields")
    return UnitIdentity(**value)


def _relationship(value: object) -> Relationship:
    if (not isinstance(value, dict) or set(value) != {
        "id", "source_unit_id", "relation_type", "target_unit_id", "target_page_path", "target_page_ref",
    } or not all(_text(value[key]) for key in ("id", "source_unit_id", "relation_type"))):
        raise _invalid("the relationship has invalid fields")
    target_unit, target_page, target_ref = value["target_unit_id"], value["target_page_path"], value["target_page_ref"]
    if target_unit is not None:
        if not _text(target_unit) or target_page is not None or target_ref is not None:
            raise _invalid("a unit relationship must name one target unit")
    elif not _path(target_page) or (target_ref is not None and not _text(target_ref)):
        raise _invalid("a page relationship must name one target page")
    return Relationship(**value)


def _invalid(reason: str) -> OpError:
    return OpError("INVALID_RELATIONS_FILE", reason)
