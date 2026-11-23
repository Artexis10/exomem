"""Bounds of an agent-initiated ("automatic") Records collection creation and field addition.

`change_records` `create` takes `automatic: true` for a collection the agent starts on its own, under
the owner's `proactive_capture` setting. Without the flag a create behaves as it always has. With it
the server refuses `AUTOMATIC_CREATION_REFUSED`, creating nothing, unless every bound below holds.
The bounds hold on a file-only vault and on a store vault alike: each mode keeps its own create record.

`add_record` with `new_collection` is the one-call route to the same create: `minimal_manifest` drafts the
manifest from the row, and the create that follows carries the flag, so these bounds are the only ones.

`new_fields` on `add_record` and `edit_record` is the additive schema change under the same disposition: the fields
the call names are added to the collection and the row is written. `fields_to_add` types them from the values and
`require_field_bounds` is its one check; its field bound holds only for a collection that was created automatically.
Renaming, retyping or removing a field stays the confirmed `revise`.

The flag rests on the agent's word, as every agent-initiated write does. The bounds are not a security
boundary: they keep an eager agent from building sprawl, and a refusal costs one question, because the
way forward is the confirmed create without the flag. The flag never widens what a plain create allows.
"""

from __future__ import annotations

import datetime as dt
import json
import textwrap
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

import yaml

from . import kbdir, memory_refs, vault
from . import structured_collections as collections

#: Most item fields of an automatic collection: a minimal schema, since a speculative field costs every later row.
MAX_FIELDS = 12
#: Most automatic creations per vault in `WINDOW`: stops an agent that loops from scattering collections.
MAX_PER_WINDOW = 3
WINDOW = dt.timedelta(hours=24)

#: SQL for "this create transaction's receipt carries the automatic flag", on a `txns` row aliased `t`.
#: `json_valid` guards it so one unreadable receipt cannot fail the inventory or a create.
RECEIPT_IS_AUTOMATIC = "(CASE WHEN json_valid(t.receipt_json) THEN json_extract(t.receipt_json, '$.automatic') = 1 END)"

#: SQL for the ids of the store collections whose create transaction recorded the automatic flag: one read for all.
AUTOMATIC_COLLECTION_IDS = (
    f"SELECT c.collection_id FROM collections c JOIN txns t ON t.txn_id = c.created_txn WHERE {RECEIPT_IS_AUTOMATIC}"
)

#: A file collection's create writes this line under its audit event in the activity log. It names the
#: collection and the instant, since the log's own heading carries a date only. The audit reader keeps
#: only its own prefix, so an older release skips the line.
CREATE_LINE_PREFIX = "Records automatic-create-v1 "
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
#: Most rotated log archives (about 2 MB each) that one inventory reads: its cost stays flat as history grows,
#: and a file collection it cannot place reports unknown rather than "not automatic".
INVENTORY_ARCHIVES = 4

_CREATE_ACT = "create it without `automatic`"
_FIELD_ACT = "add it with a confirmed `revise` of the collection"
#: The Unicode general categories that can end a line in a title: control characters and the line and paragraph
#: separators. A closed set that the Unicode standard fixes.
_LINE_BREAKING = frozenset({"Cc", "Zl", "Zp"})
#: The one directory under the collection folder where a minimal collection keeps its rows.
_ROWS_SOURCE = "Items"


def _way_forward(disposition: str, noun: str, act: str) -> str:
    """The next step after a refusal. Under `off` the agent starts nothing, so the confirmed call is for what the user asked for."""
    if disposition == "off":
        return f"{act} only if the user asked for it"
    return f"propose the {noun} to the user, and {act} after they confirm"


def _refuse(bound: str, reason: str, **details: Any) -> Never:
    # A bound other than the disposition is checked only after the disposition allowed the call, so it is silent.
    way = _way_forward(details.get("disposition", "silent"), "collection", _CREATE_ACT)
    raise collections.CollectionError("AUTOMATIC_CREATION_REFUSED", f"{reason}; {way}", {"bound": bound, **details})


def require_bounds(vault_root: Path, manifest_path: str, manifest_text: str) -> None:
    """Refuse an automatic create whose caller or manifest is outside the bounds that read no create record.

    The daily bound reads them, so `require_daily_room` checks it under the writer's own lock.
    """
    from . import envelope

    disposition = envelope.active()["proactive_capture"]
    if disposition != "silent":
        _refuse(
            "proactive_capture",
            f"automatic creation needs proactive_capture silent, and it is {disposition}",
            disposition=disposition,
        )
    root = Path(vault_root)
    manifest = collections.parse_manifest_bytes(root, root / manifest_path, manifest_text.encode("utf-8"))
    if manifest.semantic_profile != "records":
        _refuse("profile", "automatic creation is for Records collections", semantic_profile=manifest.semantic_profile)
    if not manifest.schema.natural_key:
        _refuse("natural_key", "an automatic collection declares a natural key, so a repeated item updates its row")
    total = field_count(manifest.schema.fields)
    if total > MAX_FIELDS:
        _refuse(
            "fields", f"an automatic collection has at most {MAX_FIELDS} item fields", fields=total, maximum=MAX_FIELDS
        )
    front = vault.parse_frontmatter(manifest_text)[0]
    # An allowlist of the keys `minimal_manifest` emits: the manifest grows keys (views, rollups, claims, capture,
    # templates, presentation) that a denylist would miss, and each one is structure that a confirmed create makes.
    declared = [
        *sorted(set(front) - _MANIFEST_KEYS),
        *(f"item_schema.{key}" for key in sorted(set(front["item_schema"]) - _ITEM_SCHEMA_KEYS)),
        *(["references"] if collections.declared_references(manifest.schema) else []),
    ]
    if declared:
        _refuse("authored_features", f"an automatic collection declares no {', '.join(declared)}", declared=declared)


def field_count(fields: Mapping[str, collections.FieldSpec]) -> int:
    """Every declared item field: each one counts, and so does each property of an object field or of an array's items."""

    def nested(spec: collections.FieldSpec) -> int:
        return field_count(spec.properties) + (nested(spec.items) if spec.items is not None else 0)

    return sum(1 + nested(spec) for spec in fields.values())


@dataclass(frozen=True, slots=True)
class MinimalCollection:
    """The collection that `add_record` with `new_collection` creates from one row."""

    manifest_path: str
    manifest_text: str
    natural_key: tuple[str, ...]
    fields: dict[str, str]


def _is_calendar_date(value: str) -> bool:
    """Whether `value` is an ISO calendar date, `YYYY-MM-DD`: a format check on the value, not a reading of it."""
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _field_type(value: Any) -> str | None:
    """The field type of one value's JSON shape, or None when a single value gives it no scalar type."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        # One value cannot tell a whole-number field from a decimal one, and a `number` field takes both.
        return "number"
    if isinstance(value, str):
        return "date" if _is_calendar_date(value) else "string"
    return None


def _minimal_front(
    title: str, collection_id: str, storage: str, natural_key: Sequence[str], fields: Mapping[str, str]
) -> dict[str, Any]:
    """The frontmatter of a minimal collection: the only structure an automatic create may declare."""
    return {
        "type": "collection",
        "exomem_id": collection_id,
        "title": title,
        "semantic_profile": "records",
        "collection_version": collections.COLLECTION_VERSION,
        "schema_version": 1,
        "lifecycle": "active",
        "storage": {"strategy": storage, "source": _ROWS_SOURCE, "format_version": collections.STORAGE_FORMAT_VERSION},
        "item_schema": {
            "natural_key": list(natural_key),
            "fields": {
                name: {"type": field_type, **({"required": True} if name in natural_key else {})}
                for name, field_type in fields.items()
            },
        },
    }


#: The manifest keys, and the `item_schema` keys, that an automatic create may declare: read from the draft itself,
#: so the allowlist and what `minimal_manifest` writes cannot drift apart.
_DRAFT_SHAPE = _minimal_front("", "", "", (), {})
_MANIFEST_KEYS = frozenset(_DRAFT_SHAPE)
_ITEM_SCHEMA_KEYS = frozenset(_DRAFT_SHAPE["item_schema"])


def creates_from_row(action: Any, collection: Any, new_collection: Any) -> bool:
    """Whether this call starts the collection that `collection` names from its row: an append that carries
    `new_collection` and names a bare title, not a manifest path. The call's dispatch and the store route decide
    on this alone, so a call that creates is routed to the thread that creates."""
    from . import collection_refs  # imports record_governance, which imports this module

    return (
        action == "append" and new_collection is not None and isinstance(collection, str)
        and not collection_refs.names_a_manifest(collection.strip())
    )


def minimal_manifest(
    vault_root: Path, title: str, item: Mapping[str, Any], natural_key: Sequence[str]
) -> MinimalCollection:
    """The minimal Records collection that holds `item` as its first row.

    One field per key of `item`, typed from its value; the declared natural key is required, so a row
    without a complete key is refused by the ordinary validation and a repeated row updates its own. A key
    that `item` does not carry is declared as a string, so that same validation names it. No views,
    references, rollups or derived rows. Storage is what this vault creates: sqlite beside a collection
    store, Markdown items without one (`records.require_create_storage` is the authority). The bounds
    stay `require_bounds` and the daily count's: this only drafts.
    """
    from .collection_store import ownership

    folder = vault.sanitize_title_filename(title)
    # The title is kept verbatim in the manifest: a control character or a line or paragraph separator would start a
    # second line. Other spaces and format characters (a no-break space, a right-to-left mark) are ordinary text.
    if not folder or any(unicodedata.category(character) in _LINE_BREAKING for character in title):
        raise collections.CollectionError(
            "INVALID_COLLECTION_REFERENCE", "`collection` is not a usable collection title: name it in plain words"
        )
    fields: dict[str, str] = {}
    for name, value in item.items():
        field_type = _field_type(value)
        if field_type is None and name not in natural_key:
            raise collections.CollectionError(
                "INVALID_ITEM",
                f"field {name!r} holds null, a list or an object, which new_collection cannot type from one row: "
                "give it a string, number or boolean, or create the collection with records.create",
            )
        fields[name] = field_type or "string"
    for name in natural_key:
        fields.setdefault(name, "string")
    storage = "markdown-items" if ownership.file_only(vault_root) else collections.SQLITE_STORAGE
    manifest = _minimal_front(title, memory_refs.new_id(), storage, natural_key, fields)
    text = "---\n" + yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True) + "---\n"
    return MinimalCollection(
        kbdir.managed_join(f"Records/{folder}/_collection.md"), text, tuple(natural_key), fields
    )


def create_line(collection_id: str) -> str:
    """The activity-log line that marks a file collection's create as automatic."""
    at = dt.datetime.now(dt.UTC).strftime(_STAMP)
    return CREATE_LINE_PREFIX + json.dumps({"collection_id": collection_id, "at": at}, separators=(",", ":"))


def _file_creates(
    vault_root: Path, *, since: dt.datetime | None = None, max_archives: int | None = None
) -> tuple[list[dict[str, str]], bool]:
    """The automatic creates that the activity log records, and whether the read covered the whole log.

    `since` skips the archives that predate it, which never loses a create inside the window.
    A line that does not parse raises: a create it recorded would go uncounted.
    """
    lines, complete = vault.operation_log_lines(
        vault_root, CREATE_LINE_PREFIX, rotated_since=None if since is None else since.timestamp(),
        max_archives=max_archives,
    )
    creates = []
    for line in lines:
        event = json.loads(line.removeprefix(CREATE_LINE_PREFIX))
        if not (isinstance(event, dict) and isinstance(event.get("collection_id"), str) and isinstance(event.get("at"), str)):
            raise ValueError("an automatic-create line carries no collection and instant")
        creates.append(event)
    return creates, complete


def file_created_ids(vault_root: Path) -> tuple[frozenset[str], bool] | None:
    """The collections whose file create was automatic, and whether the read covered the whole log.

    None is a log that cannot be read. The inventory runs on every discovery, so its read stops after
    `INVENTORY_ARCHIVES` archives rather than growing with the vault's whole history.
    """
    try:
        creates, complete = _file_creates(vault_root, max_archives=INVENTORY_ARCHIVES)
    except (vault.PathGuardError, OSError, ValueError):
        return None
    return frozenset(event["collection_id"] for event in creates), complete


def require_daily_room(vault_root: Path, connection: Any = None) -> None:
    """Refuse the creation that would be the `MAX_PER_WINDOW`-th plus one in `WINDOW`.

    Each storage mode counts its own create record. A store vault counts the collections' create
    transactions: `collections` holds tens of rows, so that is a small join by primary key, never a scan
    of the transaction log. A file vault counts the activity log's create lines: the live log, plus an
    archive only when it was rotated inside the window. `committed_at` and the line's instant share one
    form, so the text comparison orders by time. A vault whose store came after file collections counts
    both, so `connection` is the store's when it has one. The caller holds the mutation lock.
    """
    since = dt.datetime.now(dt.UTC) - WINDOW
    cutoff = since.strftime(_STAMP)
    created = 0
    if connection is not None:
        (created,) = connection.execute(
            "SELECT COUNT(*) FROM collections c JOIN txns t ON t.txn_id = c.created_txn "
            f"WHERE t.committed_at >= ? AND {RECEIPT_IS_AUTOMATIC}",  # noqa: S608 - a module constant, no input
            (cutoff,),
        ).fetchone()
    try:
        created += sum(1 for event in _file_creates(vault_root, since=since)[0] if event["at"] >= cutoff)
    except (vault.PathGuardError, OSError, ValueError):
        _refuse("daily", "the activity log could not be read to count the automatic creations in the last 24 hours")
    if created >= MAX_PER_WINDOW:
        _refuse(
            "daily",
            f"this vault created {created} collections automatically in the last 24 hours, the most allowed",
            created=created,
            maximum=MAX_PER_WINDOW,
        )


def _refuse_fields(bound: str, reason: str, **details: Any) -> Never:
    way = _way_forward(details.get("disposition", "silent"), "field", _FIELD_ACT)
    raise collections.CollectionError("AUTOMATIC_FIELDS_REFUSED", f"{reason}; {way}", {"bound": bound, **details})


def fields_to_add(manifest: collections.CollectionManifest, values: Mapping[str, Any]) -> dict[str, str]:
    """The fields `values` names that `manifest` does not declare yet, each typed from its value.

    A name the collection already declares adds nothing, so a replayed call is the same call, and the row's own
    validation judges its value against the declared type: `new_fields` never retypes a field. A new field whose
    value gives no scalar type (null, a list, an object) cannot be typed from one row.
    """
    added: dict[str, str] = {}
    for name, value in values.items():
        if name in manifest.schema.fields:
            continue
        field_type = _field_type(value)
        if field_type is None:
            _refuse_fields(
                "field_type", f"field {name!r} holds null, a list or an object, which new_fields cannot type from one row",
                field=name,
            )
        added[name] = field_type
    return added


def created_automatically(vault_root: Path, collection_id: str, *, store_created: bool | None) -> bool:
    """Whether the collection's own create record says that its create was automatic.

    A store collection's record is its create transaction's receipt, which the caller read on the store thread
    (`store_created`); None means a file collection, whose record is the activity log's create line.
    """
    if store_created is not None:
        return store_created
    found = file_created_ids(vault_root)
    # An unreadable log, or a bounded read that ends before the id, answers "not automatic": that wrong call
    # costs one collection that grows past `MAX_FIELDS`, and a refused write would cost the owner more.
    return found is not None and collection_id in found[0]


def require_field_bounds(
    vault_root: Path, manifest: collections.CollectionManifest, added: Mapping[str, str], *, store_created: bool | None
) -> None:
    """Refuse an automatic addition of `added` to `manifest` unless the disposition allows it and the field bound holds.

    The field bound stops an agent's own collections from sprawling, so it applies only to a collection that was
    itself created automatically: it holds at most `MAX_FIELDS` item fields after an addition. A collection the owner
    made, or one a confirmed create made, takes any number of added fields, since its width is the owner's choice.
    The create record is read only when the count is over the bound; `store_created` is the store's answer, if any.
    """
    from . import envelope

    disposition = envelope.active()["proactive_capture"]
    if disposition != "silent":
        _refuse_fields(
            "proactive_capture",
            f"adding a field automatically needs proactive_capture silent, and it is {disposition}",
            disposition=disposition,
        )
    total = field_count(manifest.schema.fields) + len(added)
    if total > MAX_FIELDS and created_automatically(vault_root, manifest.collection_id, store_created=store_created):
        _refuse_fields(
            "fields",
            f"an automatic collection holds at most {MAX_FIELDS} item fields, and this addition would leave {total}",
            fields=total,
            maximum=MAX_FIELDS,
        )


def with_added_fields(manifest_text: str, added: Mapping[str, str]) -> str:
    """`manifest_text` with one declaration per `added` field at the end of `item_schema.fields`, every other byte kept.

    The manifest is YAML frontmatter and a collection may carry comments and saved views, so this edits
    at the parsed node's position as `record_formats.render_manifest_audit_head` does, rather than dumping
    the document again. A `fields` mapping written in flow style has no line to extend and is refused,
    which leaves the confirmed `revise`.
    """
    _front, _body, front_text = vault.parse_frontmatter(manifest_text, strict=True)
    document = yaml.compose(front_text or "")

    def child(node: Any, key: str) -> Any:
        if not isinstance(node, yaml.MappingNode):
            return None
        return next((value for name, value in node.value if getattr(name, "value", None) == key), None)

    declared = child(child(document, "item_schema"), "fields")
    if not isinstance(declared, yaml.MappingNode) or declared.flow_style or not declared.value:
        _refuse_fields("layout", "this collection's fields are not written one per line, so a field cannot be added in place")
    newline = "\r\n" if "\r\n" in front_text else "\n"
    indent = " " * declared.value[0][0].start_mark.column
    entries = "".join(
        textwrap.indent(yaml.safe_dump({name: {"type": field_type}}, sort_keys=False), indent)
        for name, field_type in added.items()
    ).replace("\n", newline)
    # The `fields` mapping ends where the next token starts (a sibling key, a later top-level key), or at the document
    # end. A last field that is a block sequence or a block scalar ends on that same mark, so its own end is no
    # insertion line.
    end = declared.end_mark
    if end.index >= len(front_text):
        revised = front_text + entries if front_text.endswith("\n") else front_text + newline + entries.removesuffix(newline)
    else:
        line_start = end.index - end.column
        revised = front_text[:line_start] + entries + front_text[line_start:]
    # The edit is checked against the document it edits, so a layout this does not foresee is refused, never written.
    expected = yaml.safe_load(front_text)
    expected["item_schema"]["fields"].update({name: {"type": field_type} for name, field_type in added.items()})
    try:
        intact = yaml.safe_load(revised) == expected
    except yaml.YAMLError:
        intact = False
    if not intact:
        _refuse_fields("layout", "a field cannot be added in place to this collection's manifest without moving other content")
    return manifest_text.replace(front_text, revised, 1)
