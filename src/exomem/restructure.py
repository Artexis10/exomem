"""Restructure: the closed step set, applied and inverted on a manifest and its rows, and proved lossless.

One module holds each step and its inverse for both storage modes. A substrate loads the collection's manifest text
and rows, calls `plan`, and commits what it returns: the proposed manifest, the changed rows and one shape-history
entry. Nothing here touches storage, so a later batched job can apply the same plan in pieces.

`plan` applies the steps, applies their inverse to the result, and compares with the starting manifest and every
starting row. It returns only a plan that round-trips, and otherwise refuses naming the step and the rows.

An identity move (`rekey`, `merge_into`, `split_out` and their inverses) changes which key or which collection holds
a row. The plan names, per collection, the rows written, the rows retracted and where each written row came from; a
substrate commits a retracted row as a retraction and a moved one as an insert or a revival. A merge copies: the
source keeps its rows, retired with the target as its successor. Only a substrate that passes a `Reach` applies a
move; the file-only substrate passes none, so `resolve` refuses every move there in one place (`_refuse_file_moves`).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import manifest_edit
from . import structured_collections as collections
from .manifest_edit import ManifestEdit

# The step kinds are the closed set that the `restructure` operation schema and the structured-collections spec fix:
# each maps to the parameters it takes. Code branches on the kind, never on a field or collection name.
STEP_PARAMETERS: Mapping[str, tuple[str, ...]] = {
    "add_field": ("field", "type"),
    "rename_field": ("field", "to"),
    "retype_field": ("field", "type"),
    "retire_field": ("field",),
    "restore_field": ("field",),
    "rekey": ("key",),
    "merge_into": ("target", "mapping"),
    "split_out": ("where", "title"),
    "retitle": ("title",),
    "retire": (),
    "restore": (),
}
# Only an inverse uses these kinds. Undoing an addition drops the field, or retires it where anything still holds it.
# `unmerge` undoes a merge and `rejoin` a split; `resplit` undoes a rejoin. `folded` lists each source row that folded
# into an existing target row as [source key, target key, fields it filled], and `added` the fields the target gained.
_INVERSE_ONLY: Mapping[str, tuple[str, ...]] = {
    "remove_field": ("field",),
    "unmerge": ("target", "mapping", "folded", "added"),
    "rejoin": ("target",),
    "resplit": ("target", "folded", "added"),
}
# What each step kind tells the person who owns the collection, in their words. `{title}` is the collection's title,
# `{field}` a field name read as words, `{new}` the title a retitle or split gives, `{target}` the title of the
# collection a merge folds into. The phrases name neither the layer nor the structure. A step kind absent here changes
# nothing the person sees (`INTERNAL_STEPS`), so a call made only of those says nothing.
SUMMARY_PHRASES: Mapping[str, str] = {
    "add_field": "{title} now keeps the {field}",
    "retire_field": "{title} no longer uses the {field}; what it held is kept",
    "restore_field": "{title} uses the {field} again",
    "remove_field": "{title} no longer uses the {field}",
    "retitle": "Now called {title}",
    "split_out": "Part of {title} moved into {new}",
    "merge_into": "{title} now lives in {target}",
    "unmerge": "{title} is back on its own, apart from {target}",
    "rejoin": "{target} is back in {title}",
    "resplit": "Part of {title} is in {target} again",
    "retire": "{title} is put away and can come back",
    "restore": "{title} is back in use",
}
#: What the Records inventory tells an agent about reshaping, beside the route that runs it. The inventory is
#: what a session reads before it writes a row, and a session that never loads bootstrap learns it nowhere else.
RESHAPE_CUE = (
    "A list that stops fitting is reshaped in one lossless, undoable call: merge_into joins two lists of one kind, "
    "split_out moves part of one into a list of its own, add_field keeps a new detail, and rekey lets the key hold a "
    "further occurrence. Under silent capture pass automatic true and make a change whose fit is plain without "
    "asking; ask one question only when it is not. Under advisory propose the change first."
)
# nosemgrep: ep-word-set -- the step kinds that change nothing the person sees: a subset of this module's closed step set, which the step schema fixes.
INTERNAL_STEPS = frozenset({"rename_field", "retype_field", "rekey"})
# The step kinds that change which key or which collection holds a row. The handlers below close the set.
# nosemgrep: ep-word-set -- a subset of this module's closed step kinds, each implemented by a handler below.
_MOVES = frozenset({"rekey", "merge_into", "split_out", "unmerge", "rejoin", "resplit"})
# The origin set is closed by the spec's shape-history requirement. `declined` records a refused proposal.
ORIGINS = ("automatic", "user_choice", "user_design", "declined")
# A call names one of these. `automatic` is set by the server and never named: a call carries `automatic: true`, which
# the bounds of `automatic_collection` govern, and the server's own `new_fields` addition records it directly.
CALLER_ORIGINS = ("user_choice", "user_design", "declined")
# The closed origin set of the structured-collections spec: a create records who designed the shape, a caller names
# one of these, and the server's own create is `automatic`.
CREATE_ORIGINS = ("user_choice", "user_design")
DEFAULT_ORIGIN = "user_choice"
MAX_STEPS = 16
# The rows one call writes and retracts together: a file collection's lifecycle receipt names the manifest and every
# file the call rewrote and holds at most 513 paths, and a store collection writes and retracts them in the call's one
# transaction. A store retraction costs about as much as a write: at 512 of either, that transaction held the store's
# writer for 0.3-0.45 s on a quiet laptop and up to 0.66 s on a loaded one, past the 0.5 s budget. At 384 the direct
# path stayed inside it, but through the service a merge held up to 0.58 s on a laptop at load 13-14, nearly all of
# it CPU: the same merge took 0.31-0.54 s of CPU across three runs. 256 keeps that worst case inside the budget.
MAX_REWRITTEN_ROWS = 256
# The scalar types that carry no extra declaration (enum values, items, properties), so a field of one can be
# added or retyped by name and type alone. The manifest schema fixes the type names.
_SCALAR_TYPES = ("string", "integer", "number", "boolean", "date", "datetime")
_ROWS_NAMED = 10
_FIELDS = ("item_schema", "fields")


@dataclass(frozen=True, slots=True)
class Holding:
    """One collection as a restructure reads it: its id, manifest path, manifest text and rows by key."""

    collection_id: str
    path: str
    text: str
    rows: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Reach:
    """How a move reaches past the called collection: the substrate's lookups, each refusing what it cannot give.

    `load` returns another collection named by id or manifest path. `parse` reads a manifest from its path and text,
    for the keys and the filter a move derives. `create` returns the id and manifest path that a split's new
    collection takes, from the source's manifest path and the new title.
    """

    load: Callable[[str], Holding]
    parse: Callable[[str, str], collections.CollectionManifest]
    create: Callable[[str, str], tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Change:
    """What a move commits to one collection besides the called one.

    `created` marks a split's new collection. `written` rows are inserted, revived or updated; `retracted` rows leave
    current state. `origins` names, for a written row that came from another key or collection, that `(collection id,
    key)`: its body moves with it, and a key's own view file passes to the row that takes its place.
    """

    collection_id: str
    path: str
    text: str
    created: bool
    rows: Mapping[str, Mapping[str, Any]]
    written: tuple[str, ...]
    retracted: tuple[str, ...]
    origins: Mapping[str, tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Plan:
    """What a substrate commits: the proposed manifest, the rows it rewrites, and the steps that undo it.

    A move also names the called collection's `retracted` rows and their `origins` (see `Change`), and every other
    collection it writes (`others`). `starts` holds the manifest text of each collection the plan read, as it was.
    """

    text: str
    rows: Mapping[str, Mapping[str, Any]]
    changed_rows: tuple[str, ...]
    steps: tuple[Mapping[str, Any], ...]
    inverse: tuple[Mapping[str, Any], ...]
    retracted: tuple[str, ...] = ()
    origins: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    others: tuple[Change, ...] = ()
    starts: Mapping[str, str] = field(default_factory=dict)

    @property
    def moves(self) -> bool:
        """Whether committing the plan retracts a row or writes another collection."""
        return bool(self.retracted or self.others)


def summary(
    steps: Sequence[Mapping[str, Any]], title: str, titled: Callable[[str], str | None] | None = None
) -> str | None:
    """One plain sentence on what `steps` change for the person, or None when none of them changes what they get.

    `title` is the collection's title and `titled` names a merge target by its title. A merge whose target has
    no title to say is left out: no sentence is better than one that names an id or a path.
    """
    said = []
    for step in steps:
        phrase = SUMMARY_PHRASES.get(step["step"])
        target = titled(step["target"]) if titled is not None and "target" in step else None
        if phrase is None or ("{target}" in phrase and not target):
            continue
        words = str(step.get("field", "")).replace("_", " ").replace("-", " ")
        said.append(phrase.format(title=title, field=words, new=step.get("title", ""), target=target))
    return "; ".join(said) or None


def refuse(code: str, reason: str, **details: Any) -> collections.CollectionError:
    return collections.CollectionError(code, reason, details)


def parse_steps(raw: Any, *, allow_inverse: bool = False) -> tuple[dict[str, Any], ...]:
    """The steps of a call, each checked against the closed set and its parameters."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not 0 < len(raw) <= MAX_STEPS:
        raise refuse("INVALID_RESTRUCTURE", f"steps is a list of 1 to {MAX_STEPS} steps")
    allowed = {**STEP_PARAMETERS, **(_INVERSE_ONLY if allow_inverse else {})}
    steps = []
    for index, step in enumerate(raw):
        kind = step.get("step") if isinstance(step, Mapping) else None
        if not isinstance(kind, str) or kind not in allowed:
            raise refuse(
                "INVALID_RESTRUCTURE", f"step {index} names no step kind", step=index, allowed=sorted(STEP_PARAMETERS)
            )
        parameters = allowed[kind]
        surplus = sorted(set(step) - {"step", *parameters})
        missing = [name for name in parameters if name not in step and name != "mapping"]
        if surplus or missing:
            raise refuse(
                "INVALID_RESTRUCTURE",
                f"step {index} ({kind}) takes {', '.join(parameters) or 'no parameters'}"
                + (f"; missing {', '.join(missing)}" if missing else f"; unexpected {', '.join(surplus)}"),
                step=index, kind=kind,
            )
        steps.append(dict(step))
    return tuple(steps)


def resolve_targets(raw: Any, resolve: Callable[[str], str | None]) -> Any:
    """`raw` steps with each step's `target`, which a caller names by title, replaced by what `resolve` answers.

    The facade resolves a target as it resolves the called collection, under the caller's authority. Anything that is
    not a step taking a target passes unchanged, so `parse_steps` still refuses a malformed list.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return raw
    return [
        {**step, "target": resolve(step["target"])}
        if isinstance(step, Mapping) and "target" in STEP_PARAMETERS.get(step.get("step"), ())
        and isinstance(step.get("target"), str) else step
        for step in raw
    ]


def _refused(index: int, step: Mapping[str, Any], reason: str, **details: Any) -> collections.CollectionError:
    return refuse(
        "RESTRUCTURE_STEP_REFUSED", f"step {index} ({step['step']}) refused: {reason}",
        step=index, kind=step["step"], **details,
    )


def _name(value: Any, label: str) -> str:
    if type(value) is not str or not value or len(value.encode()) > 128:
        raise refuse("INVALID_RESTRUCTURE", f"{label} is a name of 1 to 128 bytes")
    return value


def _fields(edit: ManifestEdit) -> dict[str, Any]:
    return edit.document["item_schema"]["fields"]


def _key(edit: ManifestEdit) -> list[str]:
    return list(edit.document["item_schema"]["natural_key"])


def _refuse_core(edit: ManifestEdit, index: int, step: Mapping[str, Any], field: str) -> None:
    """Refuse a step on a field that the Planning profile defines: its meaning belongs to Planning, not to a collection."""
    # Planning imports this module, so the profile's field set is read when a step runs.
    from . import planning

    if edit.document.get("semantic_profile") == "planning" and field in planning.CORE_FIELDS:
        raise _refused(
            index, step, f"{field!r} is a Planning core field, so a restructure leaves it as the profile defines it",
            field=field,
        )


def _field(edit: ManifestEdit, index: int, step: Mapping[str, Any], name: str = "field") -> tuple[str, Any]:
    field = _name(step[name], name)
    _refuse_core(edit, index, step, field)
    spec = _fields(edit).get(field)
    if spec is None:
        raise _refused(index, step, f"the collection has no field {field!r}", field=field)
    return field, spec


def _held(rows: Mapping[str, Mapping[str, Any]], field: str) -> list[str]:
    return [key for key, values in rows.items() if values.get(field) is not None]


# Each handler applies one step to the manifest under edit and the rows, and returns the steps that undo it.
def _add_field(edit, rows, index, step, titles):
    field, kind = _name(step["field"], "field"), step["type"]
    _refuse_core(edit, index, step, field)
    if kind not in _SCALAR_TYPES:
        raise _refused(index, step, f"a field is added with one of {', '.join(_SCALAR_TYPES)}", type=kind)
    if field in _fields(edit):
        raise _refused(index, step, f"the collection already has a field {field!r}", field=field)
    edit.add_entry(_FIELDS, field, {"type": kind})
    return [{"step": "remove_field", "field": field}]


def _remove_field(edit, rows, index, step, titles):
    field, spec = _field(edit, index, step)
    holders = _held(rows, field)
    if holders or mentions(edit.document, field):
        # A value written since, or a view or recipe that now names the field, keeps it: the field is set aside.
        if spec.get("retired") is True:
            return []
        if spec.get("required") is True or field in _key(edit):
            raise _refused(
                index, step, f"{field!r} is required or part of the key, so it cannot be set aside while rows hold it",
                field=field,
            )
        edit.set_value((*_FIELDS, field), {**spec, "retired": True})
        return [{"step": "restore_field", "field": field}]
    edit.remove_entry(_FIELDS, field)
    return [{"step": "add_field", "field": field, "type": spec["type"]}]


def mentions(document: Mapping[str, Any], name: str) -> list[str]:
    """The places outside the field's own declaration where the manifest names it, as dotted paths.

    An exact match of a parsed key or value against a declared field name: identity, not the meaning of a word.
    """
    found: list[str] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if path[:3] == (*_FIELDS, name) or path == ("item_schema", "natural_key"):
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == name and path != _FIELDS:
                    found.append(".".join((*path, str(key))))
                walk(value, (*path, str(key)))
        elif isinstance(node, list):
            for position, value in enumerate(node):
                walk(value, (*path, str(position)))
        elif node == name and len(path) > 1:
            found.append(".".join(path))

    walk(document, ())
    return found


def _in_key(edit, index, step, field, verb):
    if field in _key(edit):
        raise _refused(
            index, step, f"{field!r} is part of the natural key, so {verb} would move every row's identity",
            field=field,
        )


def _rename_field(edit, rows, index, step, titles):
    field, _spec = _field(edit, index, step)
    target = _name(step["to"], "to")
    _refuse_core(edit, index, step, target)
    _in_key(edit, index, step, field, "renaming it")
    if target in _fields(edit):
        raise _refused(index, step, f"the collection already has a field {target!r}", field=target)
    edit.rename_key(_FIELDS, field, target)
    for values in rows.values():
        if field in values:
            values[target] = values.pop(field)
    return [{"step": "rename_field", "field": target, "to": field}]


def _cast(value: Any, kind: str) -> Any:
    """`value` as the nearest value of scalar type `kind`; the round trip, not this, decides whether nothing was lost."""
    if value is None:
        return None
    if kind == "string":
        # The JSON spelling of a number or a boolean: it is what the cast to those types reads back.
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, (bool, int, float)) else str(value)
    if isinstance(value, bool):
        if kind == "boolean":
            return value
        raise ValueError("a boolean is not a number")
    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return int(value) if isinstance(value, (str, int)) else _no(value)
    if kind == "number":
        if isinstance(value, (int, float)):
            return value
        text = value if isinstance(value, str) else _no(value)
        try:
            parsed = int(text)
        except ValueError:
            return float(text)
        return parsed if str(parsed) == text else float(text)
    if kind == "boolean":
        return {"true": True, "false": False}[value] if isinstance(value, str) and value in {"true", "false"} else _no(value)
    return value  # a date or datetime keeps its value; the schema's own check judges it


def same(left: Any, right: Any) -> bool:
    """Whether two values are the same stored value: equal canonical JSON, so 1, 1.0 and true stay three values."""
    from .records import _canonical_json  # records imports this module

    return _canonical_json(left) == _canonical_json(right)


def _no(value: Any) -> Any:
    raise ValueError(f"{value!r} has no value of that type")


def _retype_rows(rows, field, old, new, index, step) -> None:
    """Carry every value of `field` to `new`; refuse naming the rows whose value cannot go there and come back."""
    lossy: list[dict[str, Any]] = []
    converted: dict[str, Any] = {}
    for key, values in rows.items():
        value = values.get(field)
        if value is None:
            continue
        try:
            candidate = _cast(value, new)
            collections.validate_field_value(field, candidate, collections.FieldSpec(new))
            kept = same(_cast(candidate, old), value)
        except (ValueError, TypeError, OverflowError, collections.CollectionError):
            kept = False
        if kept:
            converted[key] = candidate
        else:
            lossy.append({"row": key, "value": value})
    if lossy:
        raise refuse(
            "RESTRUCTURE_NOT_LOSSLESS",
            f"step {index} (retype_field) would lose the value of {len(lossy)} row(s) in {field!r}: "
            + ", ".join(f"{item['row']} holds {item['value']!r}" for item in lossy[:_ROWS_NAMED]),
            step=index, kind=step["step"], field=field, rows=lossy[:_ROWS_NAMED], rows_total=len(lossy),
        )
    for key, value in converted.items():
        rows[key][field] = value


def _retype_field(edit, rows, index, step, titles):
    field, spec = _field(edit, index, step)
    kind = step["type"]
    if kind not in _SCALAR_TYPES or spec["type"] not in _SCALAR_TYPES:
        raise _refused(index, step, f"a retype runs between {', '.join(_SCALAR_TYPES)}", field=field)
    if kind == spec["type"]:
        raise _refused(index, step, f"{field!r} already has type {kind}", field=field)
    if set(spec) - {"type", "required"}:
        raise _refused(index, step, f"{field!r} carries more than a type and `required`", field=field)
    _in_key(edit, index, step, field, "retyping it")
    _retype_rows(rows, field, spec["type"], kind, index, step)
    edit.set_value((*_FIELDS, field), {**spec, "type": kind})
    return [{"step": "retype_field", "field": field, "type": spec["type"]}]


def _retire_field(edit, rows, index, step, titles):
    field, spec = _field(edit, index, step)
    if spec.get("retired") is True:
        raise _refused(index, step, f"{field!r} is already set aside", field=field)
    if spec.get("required") is True:
        raise _refused(index, step, f"{field!r} is required, so a new row could not be written without it", field=field)
    _in_key(edit, index, step, field, "setting it aside")
    edit.set_value((*_FIELDS, field), {**spec, "retired": True})
    return [{"step": "restore_field", "field": field}]


def _restore_field(edit, rows, index, step, titles):
    field, spec = _field(edit, index, step)
    if spec.get("retired") is not True:
        raise _refused(index, step, f"{field!r} is not set aside", field=field)
    edit.set_value((*_FIELDS, field), {name: value for name, value in spec.items() if name != "retired"})
    return [{"step": "retire_field", "field": field}]


def _retitle(edit, rows, index, step, titles):
    title = _name(step["title"], "title")
    before = edit.document["title"]
    if title == before:
        raise _refused(index, step, f"the collection is already titled {title!r}")
    if title in titles:
        raise _refused(index, step, f"another collection is titled {title!r}", title=title)
    edit.set_value(("title",), title)
    return [{"step": "retitle", "title": before}]


def _retire(edit, rows, index, step, titles):
    if edit.document["lifecycle"] != collections.LIFECYCLE_ACTIVE:
        raise _refused(
            index, step, f"the collection is {edit.document['lifecycle']!r}, not {collections.LIFECYCLE_ACTIVE!r}"
        )
    edit.set_value(("lifecycle",), collections.LIFECYCLE_RETIRED)
    return [{"step": "restore"}]


def _restore(edit, rows, index, step, titles):
    if edit.document["lifecycle"] != collections.LIFECYCLE_RETIRED:
        raise _refused(
            index, step, f"the collection is {edit.document['lifecycle']!r}, not {collections.LIFECYCLE_RETIRED!r}"
        )
    edit.set_value(("lifecycle",), collections.LIFECYCLE_ACTIVE)
    return [{"step": "retire"}]


class _World:
    """The collections one run of steps reads and writes: the called one (`home`) and any a move reaches.

    `start` keeps each collection as first read, for the round trip. `origins` maps a row a move wrote, as
    `(collection id, key)`, to the row it came from, so a chain of moves still names the first one.
    """

    def __init__(self, home: Holding, reach: Reach | None) -> None:
        self.home, self.reach = home.collection_id, reach
        self.start: dict[str, Holding] = {home.collection_id: home}
        self.edits: dict[str, ManifestEdit] = {}
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}
        self.paths: dict[str, str] = {}
        self.origins: dict[tuple[str, str], tuple[str, str]] = {}
        self.created: list[str] = []
        self._hold(home)

    def _hold(self, held: Holding) -> None:
        self.edits[held.collection_id] = ManifestEdit(held.text)
        self.rows[held.collection_id] = {key: copy.deepcopy(dict(values)) for key, values in held.rows.items()}
        self.paths[held.collection_id] = held.path

    def fork(self) -> _World:
        """A world that starts where this one ends, with the same first reads: the run of an inverse."""
        forked = copy.copy(self)
        forked.edits = {cid: ManifestEdit(edit.text) for cid, edit in self.edits.items()}
        forked.rows = {cid: copy.deepcopy(rows) for cid, rows in self.rows.items()}
        forked.paths, forked.origins, forked.created = dict(self.paths), {}, []
        return forked

    def holding(self, name: Any, index: int, step: Mapping[str, Any]) -> str:
        """The id of the collection `name` names, read into the world on first use."""
        name = _name(name, "target")
        if name in self.edits:
            return name
        held = self.reach.load(name)
        if held.collection_id not in self.edits:
            self.start.setdefault(held.collection_id, held)
            self._hold(held)
        return held.collection_id

    def manifest(self, cid: str) -> collections.CollectionManifest:
        return self.reach.parse(self.paths[cid], self.edits[cid].text)

    def create(self, source: str, title: str) -> str:
        """A new collection with `source`'s manifest under a new id and `title`."""
        cid, path = self.reach.create(self.paths[source], title)
        edit = ManifestEdit(self.edits[source].text)
        edit.set_value(("exomem_id",), cid)
        edit.set_value(("title",), title)
        self._hold(Holding(cid, path, edit.text, {}))
        self.created.append(cid)
        return cid

    def move(self, cid: str, key: str, from_cid: str, from_key: str) -> None:
        self.origins[(cid, key)] = self.origins.get((from_cid, from_key), (from_cid, from_key))


def _refuse_file_moves(step: Mapping[str, Any]) -> collections.CollectionError:
    """The one refusal of a move on a collection kept only in files: nothing changes, and the shape stays as it is.

    File-only moves are not built: Markdown-only collections are deprecated, and a later change enrols every
    collection in the store.
    """
    return refuse(
        "RESTRUCTURE_MOVE_UNAVAILABLE",
        f"{step['step']} moves rows between keys or collections, which a collection kept only in files does not do "
        "yet; nothing changed, and the collection keeps its current shape",
        kind=step["step"],
        remediation="keep the collection as it is, or change its fields with the other restructure steps",
    )


def creation_unavailable() -> collections.CollectionError:
    """The refusal of a split on an enrolled store that no running service serves, so its admitted create cannot run.

    Nothing changes.
    """
    return refuse(
        "RESTRUCTURE_MOVE_UNAVAILABLE",
        "split_out makes a new collection, which this vault's collection store adds only through the create of the "
        "service that serves it; nothing changed, and the collection keeps its current shape",
        kind="split_out",
        remediation="run the split through the running service, or merge or rekey the collection instead",
    )


def _key_fields(edit: ManifestEdit, index: int, step: Mapping[str, Any]) -> list[str]:
    raw = step["key"]
    if not isinstance(raw, list) or not raw or len(set(map(str, raw))) != len(raw):
        raise _refused(index, step, "key is a list of distinct field names")
    names = [_name(name, "key") for name in raw]
    fields = _fields(edit)
    for name in names:
        if name not in fields or fields[name].get("retired") is True:
            raise _refused(index, step, f"the collection has no field {name!r} in use", field=name)
    return names


def _rekey(world: _World, index, step, titles):
    cid = world.home
    edit, rows = world.edits[cid], world.rows[cid]
    old, new = _key(edit), _key_fields(edit, index, step)
    if not old:
        raise _refused(index, step, "the collection has no natural key, so each row keeps the identity it was given")
    if new == old:
        raise _refused(index, step, f"the collection is already keyed by {', '.join(new)}")
    for name in dict.fromkeys([*old, *new]):
        _refuse_core(edit, index, step, name)
    edit.set_value(("item_schema", "natural_key"), new)
    manifest = world.manifest(cid)
    keyed: dict[str | None, list[str]] = {}
    for key, values in rows.items():
        keyed.setdefault(collections.derived_item_key(manifest, values), []).append(key)
    if missing := keyed.pop(None, []):
        raise _refused(
            index, step, f"{len(missing)} row(s) hold no value under the new key: {', '.join(missing[:_ROWS_NAMED])}",
            rows=missing[:_ROWS_NAMED], rows_total=len(missing),
        )
    if shared := [key for keys in keyed.values() if len(keys) > 1 for key in keys]:
        raise _refused(
            index, step, f"{len(shared)} row(s) share a value under the new key: {', '.join(shared[:_ROWS_NAMED])}",
            rows=shared[:_ROWS_NAMED], rows_total=len(shared),
        )
    moved = {new_key: rows[old_key] for new_key, (old_key,) in keyed.items()}
    for new_key, (old_key,) in keyed.items():
        world.move(cid, new_key, cid, old_key)
    rows.clear()
    rows.update(moved)
    return [{"step": "rekey", "key": old}]


def _mapping(world: _World, source: str, step: Mapping[str, Any], index: int) -> dict[str, str]:
    """A merge's renames, source field to target field: each names a source field and no two meet in one target."""
    raw = step.get("mapping") or {}
    if not isinstance(raw, Mapping):
        raise _refused(index, step, "mapping maps a source field to the target field that takes its values")
    mapping = {_name(name, "mapping"): _name(to, "mapping") for name, to in raw.items()}
    fields = _fields(world.edits[source])
    if unknown := sorted(set(mapping) - set(fields)):
        raise _refused(index, step, f"the collection has no field {unknown[0]!r}", field=unknown[0])
    landed = [mapping.get(name, name) for name in fields]
    if clashes := sorted({name for name in landed if landed.count(name) > 1}):
        raise _refused(index, step, f"two fields would land in {clashes[0]!r}", field=clashes[0])
    for name, to in mapping.items():
        if name != to:
            _refuse_core(world.edits[source], index, step, name)
            _refuse_core(world.edits[source], index, step, to)
    return {name: to for name, to in mapping.items() if name != to}


def _carried(world: _World, values, mapping, source: str, target: str, keys: Mapping[str, str]) -> dict[str, Any]:
    """A source row's values as the target holds them: renamed by `mapping`, and, in a Planning collection, with each
    `parent` or `area` that names a source item naming that item in the target (`keys` maps source key to target key)."""
    from . import planning

    carried = {mapping.get(name, name): copy.deepcopy(value) for name, value in values.items()}
    if world.edits[source].document.get("semantic_profile") == "planning":
        for name in planning.HIERARCHY_FIELDS:
            named = collections.plan_ref_target(source, carried.get(name))
            if named is not None and named in keys:
                carried[name] = collections.plan_ref(target, keys[named])
    return carried


def _held_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _natural(manifest: collections.CollectionManifest, values: Mapping[str, Any]) -> str | None:
    """A row's natural key as the manifest serializes it, or None where the manifest or the row holds no full key."""
    if not manifest.schema.natural_key or collections.derived_item_key(manifest, values) is None:
        return None
    return collections.manifest_natural_key(manifest, values)


def _fold(world: _World, source: str, target: str, mapping, index, step) -> tuple[list[list[Any]], list[str]]:
    """Copy every row of `source` into `target` and retire `source` with `target` as its successor.

    A source row folds into the target row with its key, or with its natural key, only when every value both hold is
    equal; it then fills the fields the target row lacks. Any other equal-key pair refuses, naming the rows and values.
    A source field the target lacks is added to it, never required. Returns the folds and the added fields.
    """
    s_edit, t_edit = world.edits[source], world.edits[target]
    for edit, role in ((s_edit, "the collection"), (t_edit, "the target")):
        if edit.document["lifecycle"] != collections.LIFECYCLE_ACTIVE:
            raise _refused(index, step, f"{role} is {edit.document['lifecycle']!r}, not {collections.LIFECYCLE_ACTIVE!r}")
    if s_edit.document.get("semantic_profile") != t_edit.document.get("semantic_profile"):
        raise _refused(index, step, "the target keeps another kind of collection, so its rows cannot move there")
    added: list[str] = []
    for name, spec in _fields(s_edit).items():
        to = mapping.get(name, name)
        held = _fields(t_edit).get(to)
        if held is None:
            t_edit.add_entry(_FIELDS, to, {key: value for key, value in spec.items() if key != "required"})
            added.append(to)
        elif held.get("type") != spec.get("type"):
            raise _refused(
                index, step, f"the target's {to!r} is a {held.get('type')}, and the collection's {name!r} a {spec.get('type')}",
                field=to,
            )
    manifest = world.manifest(target)
    s_rows, t_rows = world.rows[source], world.rows[target]
    # A source row keeps its key, so a stored reference to it still names it. It meets the target row with that key,
    # or else the one with its natural key, which the target holds once.
    holders = {_natural(manifest, values): key for key, values in t_rows.items()}
    keys: dict[str, str] = {}
    for key, values in s_rows.items():
        natural = _natural(manifest, {mapping.get(name, name): value for name, value in values.items()})
        keys[key] = key if key in t_rows or natural is None else holders.setdefault(natural, key)
    conflicts: list[dict[str, Any]] = []
    folded: list[list[Any]] = []
    for key, values in s_rows.items():
        carried = _carried(world, values, mapping, source, target, keys)
        into = keys[key]
        held = t_rows.get(into)
        if held is None:
            t_rows[into] = carried
            world.move(target, into, source, key)
            continue
        mine, theirs = _held_values(carried), _held_values(held)
        differ = {name: [mine[name], theirs[name]] for name in mine.keys() & theirs.keys() if not same(mine[name], theirs[name])}
        if differ:
            conflicts.append({"row": key, "target_row": into, "values": differ})
            continue
        filled = sorted(mine.keys() - theirs.keys())
        held.update({name: mine[name] for name in filled})
        folded.append([key, into, filled])
    if conflicts:
        raise refuse(
            "RESTRUCTURE_MERGE_CONFLICT",
            f"step {index} ({step['step']}) refused: {len(conflicts)} row(s) hold other values than the target row "
            "with their key, so nothing moved: " + "; ".join(
                f"{item['row']} and {item['target_row']} differ in " + ", ".join(
                    f"{name} ({mine!r} against {theirs!r})" for name, (mine, theirs) in sorted(item["values"].items()))
                for item in conflicts[:_ROWS_NAMED]),
            step=index, kind=step["step"], rows=conflicts[:_ROWS_NAMED], rows_total=len(conflicts),
        )
    s_edit.set_value(("lifecycle",), collections.LIFECYCLE_RETIRED)
    if "successor" in s_edit.document:
        s_edit.set_value(("successor",), target)
    else:
        s_edit.add_entry((), "successor", target)
    return folded, added


def _unfold(world: _World, source: str, target: str, mapping, folded, added, index, step) -> None:
    """Undo `_fold`: take the copied rows and filled fields back out of `target`, drop its added fields, and restore
    `source`. A copied row or filled field that changed since refuses, naming the rows: the undo would lose it."""
    s_edit = world.edits[source]
    if s_edit.document["lifecycle"] != collections.LIFECYCLE_RETIRED or s_edit.document.get("successor") != target:
        raise _refused(index, step, "the collection is no longer retired into the target, so the move changed since")
    if not isinstance(folded, list) or not isinstance(added, list):
        raise refuse("INVALID_RESTRUCTURE", "folded and added are lists")
    fills = {source_key: (into, filled) for source_key, into, filled in folded}
    keys = {key: fills[key][0] if key in fills else key for key in world.rows[source]}
    t_rows = world.rows[target]
    changed: list[str] = []
    taken: list[str] = []
    for key, values in world.rows[source].items():
        carried = _held_values(_carried(world, values, mapping, source, target, keys))
        if key in fills:
            into, filled = fills[key]
            held = t_rows.get(into)
            if held is None or any(not same(held.get(name), carried.get(name)) for name in filled):
                changed.append(key)
            else:
                for name in filled:
                    held.pop(name)
        elif key not in t_rows or not same(_held_values(t_rows[key]), carried):
            changed.append(key)
        else:
            taken.append(key)
    if changed:
        raise refuse(
            "RESTRUCTURE_UNDO_REFUSED",
            f"step {index} ({step['step']}) refused: {len(changed)} moved row(s) changed in the target since, and the "
            f"undo would lose that: {', '.join(changed[:_ROWS_NAMED])}",
            step=index, kind=step["step"], rows=changed[:_ROWS_NAMED], rows_total=len(changed),
        )
    _refuse_orphans(world.edits[target], target, t_rows, set(taken), index, step)
    for key in taken:
        del t_rows[key]
    for name in reversed(added):
        _remove_field(world.edits[target], t_rows, index, {"step": "remove_field", "field": name}, ())
    s_edit.set_value(("lifecycle",), collections.LIFECYCLE_ACTIVE)
    s_edit.remove_entry((), "successor")


def _merge_into(world: _World, index, step, titles):
    target = world.holding(step["target"], index, step)
    if target == world.home:
        raise _refused(index, step, "a collection merges into another collection")
    mapping = _mapping(world, world.home, step, index)
    folded, added = _fold(world, world.home, target, mapping, index, step)
    return [{"step": "unmerge", "target": target, "mapping": mapping, "folded": folded, "added": added}]


def _unmerge(world: _World, index, step, titles):
    target = world.holding(step["target"], index, step)
    _unfold(world, world.home, target, step["mapping"], step["folded"], step["added"], index, step)
    return [{"step": "merge_into", "target": target, "mapping": step["mapping"]}]


def _selected(manifest: collections.CollectionManifest, rows, where: Any, index, step) -> list[str]:
    """The keys of the rows the Records filter `where` selects, bound to the collection's declared fields."""
    from .query_engine import row_predicates, typed_rows, validation

    bound = validation.normalize_query(
        {"version": 1, "where": where},
        declarations={manifest.collection_id: typed_rows.declaration(manifest, _FILTER_BASIS)},
        collection=manifest.collection_id,
    )
    if bound.findings or bound.query is None or bound.query.where is None:
        finding = bound.findings[0] if bound.findings else None
        raise _refused(
            index, step, "where is a Records filter on the collection's fields"
            + (f" ({finding.code} at {finding.at})" if finding else ""),
        )
    instant = dt.datetime.now(dt.UTC)
    return [
        key for key, values in rows.items()
        if row_predicates.evaluate(
            bound.query.where,
            lambda node, values=values, key=key: key if node.path == "item_key" else values.get(node.path, row_predicates.MISSING),
            instant=instant,
        ) is True
    ]


@dataclass(frozen=True, slots=True)
class _Basis:
    type_name: str
    logical_vault_id: str


# A split's filter binds to the one collection it splits and reads only its rows, so the source it names stands for
# nothing outside the call.
_FILTER_BASIS = _Basis("restructure", "restructure")


def _refuse_orphans(edit: ManifestEdit, cid: str, rows, selected: Collection[str], index, step) -> None:
    """In a Planning collection, refuse a move of the `selected` rows out of `rows` that leaves an item apart from its
    `parent` or `area`, naming them: a split, or the undo of a merge that takes rows back out of its target."""
    from . import planning

    if edit.document.get("semantic_profile") != "planning":
        return
    apart = [
        {"item": key, "field": name, "names": named}
        for key, values in rows.items() for name in planning.HIERARCHY_FIELDS
        if (named := collections.plan_ref_target(cid, values.get(name))) is not None
        and named in rows and (key in selected) != (named in selected)
    ]
    if apart:
        raise _refused(
            index, step, f"{len(apart)} item(s) would end apart from their parent or area: " + ", ".join(
                f"{item['item']} ({item['field']} {item['names']})" for item in apart[:_ROWS_NAMED]),
            items=apart[:_ROWS_NAMED], items_total=len(apart),
        )


def _split_out(world: _World, index, step, titles):
    source = world.home
    edit, rows = world.edits[source], world.rows[source]
    title = _name(step["title"], "title")
    if edit.document["lifecycle"] != collections.LIFECYCLE_ACTIVE:
        raise _refused(index, step, f"the collection is {edit.document['lifecycle']!r}, not {collections.LIFECYCLE_ACTIVE!r}")
    if title in titles or title == edit.document["title"] or any(
            world.edits[cid].document["title"] == title for cid in world.created):
        raise _refused(index, step, f"another collection is titled {title!r}", title=title)
    selected = _selected(world.manifest(source), rows, step["where"], index, step)
    if not selected:
        raise _refused(index, step, "the filter selects no row, so nothing would move")
    _refuse_orphans(edit, source, rows, set(selected), index, step)
    new = world.create(source, title)
    keys = {key: key for key in selected}
    for key in selected:
        world.rows[new][key] = _carried(world, rows[key], {}, source, new, keys)
        world.move(new, key, source, key)
        del rows[key]
    return [{"step": "rejoin", "target": new}]


def _rejoin(world: _World, index, step, titles):
    other = world.holding(step["target"], index, step)
    folded, added = _fold(world, other, world.home, {}, index, step)
    return [{"step": "resplit", "target": other, "folded": folded, "added": added}]


def _resplit(world: _World, index, step, titles):
    other = world.holding(step["target"], index, step)
    _unfold(world, other, world.home, {}, step["folded"], step["added"], index, step)
    return [{"step": "rejoin", "target": other}]


_HANDLERS: Mapping[str, Callable[..., list[dict[str, Any]]]] = {
    "add_field": _add_field,
    "remove_field": _remove_field,
    "rename_field": _rename_field,
    "retype_field": _retype_field,
    "retire_field": _retire_field,
    "restore_field": _restore_field,
    "retitle": _retitle,
    "retire": _retire,
    "restore": _restore,
    "rekey": _rekey,
    "merge_into": _merge_into,
    "unmerge": _unmerge,
    "split_out": _split_out,
    "rejoin": _rejoin,
    "resplit": _resplit,
}


# The step kinds whose handler reads row values (it counts the rows that hold a field, rewrites them, or moves them).
# Closed by the handlers above: a substrate that keeps its rows apart from its manifest loads them only for these.
# nosemgrep: ep-word-set -- a subset of this module's closed step kinds, each implemented by a handler above.
_READS_ROWS = frozenset({"remove_field", "rename_field", "retype_field", *_MOVES})


def reads_rows(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a step needs the collection's row values to apply or to prove its round trip."""
    return any(step["step"] in _READS_ROWS for step in steps)


def moves_rows(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a step moves rows between keys or collections, so the plan may retract rows or write another one."""
    return any(step["step"] in _MOVES for step in steps)


def creates_collections(steps: Any) -> bool:
    """Whether a call's steps, as given, make a new collection; a store that admits it separately asks first."""
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        return False
    return any(isinstance(step, Mapping) and step.get("step") == "split_out" for step in steps)


def needs_titles(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a step compares a title with the others in the vault."""
    # nosemgrep: ep-word-membership -- the two closed step kinds that name a title, each implemented in this module.
    return any(step["step"] in {"retitle", "split_out"} for step in steps)


def _run(world: _World, steps, titles: Collection[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply `steps` in `world`; return them as applied, with each target named by its id, and their inverse."""
    applied: list[dict[str, Any]] = []
    inverse: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        try:
            if "target" in step:
                step = {**step, "target": world.holding(step["target"], index, step)}
            handler = _HANDLERS[step["step"]]
            undo = (handler(world, index, step, titles) if step["step"] in _MOVES
                    else handler(world.edits[world.home], world.rows[world.home], index, step, titles))
        except manifest_edit.ManifestLayoutError as error:
            raise _refused(index, step, error.reason) from error
        applied.append(dict(step))
        inverse = [*undo, *inverse]
    return applied, inverse


def _differences(world: _World, back: _World) -> dict[str, Any]:
    """Where the round trip `back` differs from the collections `world` first read: rows by key, and manifests."""
    rows, manifests = [], []
    for cid, held in world.start.items():
        after = back.rows[cid]
        rows += sorted(key for key in held.rows.keys() | after.keys() if not same(held.rows.get(key), after.get(key)))
        if ManifestEdit(held.text).document != back.edits[cid].document:
            manifests.append(cid)
    return {"manifest": bool(manifests), "rows": rows}


def _change(world: _World, cid: str) -> Change:
    """What committing `world` writes to collection `cid`: the rows written and retracted, and where moved rows came from."""
    held = world.start.get(cid)
    before = held.rows if held is not None else {}
    after = world.rows[cid]
    origins = {
        key: origin for (owner, key), origin in world.origins.items() if owner == cid and key in after and origin != (cid, key)
    }
    written = tuple(sorted(key for key in after if key in origins or not same(after[key], before.get(key))))
    return Change(
        cid, world.paths[cid], world.edits[cid].text, cid in world.created, after, written,
        tuple(sorted(before.keys() - after.keys())), origins,
    )


def plan(
    text: str, rows: Mapping[str, Mapping[str, Any]], steps: Sequence[Mapping[str, Any]], *, titles: Collection[str] = (),
    collection_id: str = "", path: str = "", reach: Reach | None = None,
) -> Plan:
    """Apply `steps` to the manifest `text` and `rows`, prove the result lossless, and return it.

    The proof applies the recorded inverse to the result and compares it with the starting manifest and every
    starting row of every collection the steps read. A step that does not apply, or a round trip that differs,
    refuses naming the step and the rows. A move needs the substrate's `reach`; without one it refuses here.
    """
    if reach is None and moves_rows(steps):
        raise _refuse_file_moves(next(step for step in steps if step["step"] in _MOVES))
    world = _World(Holding(collection_id, path, text, rows), reach)
    applied, inverse = _run(world, steps, titles)
    back = world.fork()
    try:
        _run(back, inverse, titles)
    except collections.CollectionError as error:
        raise refuse(
            "RESTRUCTURE_NOT_LOSSLESS",
            f"the steps cannot be undone from their result ({error.reason})",
            cause=error.code, **error.details,
        ) from error
    differs = _differences(world, back)
    if differs["manifest"] or differs["rows"]:
        raise refuse(
            "RESTRUCTURE_NOT_LOSSLESS",
            "the steps do not round-trip"
            + (f"; rows {', '.join(differs['rows'][:_ROWS_NAMED])} differ after the undo" if differs["rows"] else "")
            + ("; the manifest differs after the undo" if differs["manifest"] else ""),
            rows=differs["rows"][:_ROWS_NAMED], rows_total=len(differs["rows"]), manifest=differs["manifest"],
        )
    home = _change(world, collection_id)
    others = tuple(_change(world, cid) for cid in world.edits if cid != collection_id)
    return Plan(
        home.text, home.rows, home.written, tuple(applied), tuple(inverse), home.retracted, home.origins,
        tuple(change for change in others if change.created or change.written or change.retracted
              or ManifestEdit(world.start[change.collection_id].text).document != world.edits[change.collection_id].document),
        {cid: held.text for cid, held in world.start.items()},
    )


@dataclass(frozen=True, slots=True)
class Resolved:
    """What one restructure call comes to: the steps to run, the entry it undoes, and the proven plan."""

    run: tuple[dict[str, Any], ...]
    undoes: str | None
    planned: Plan


def resolve(
    text: str,
    *,
    steps: Sequence[Mapping[str, Any]] | None,
    undo: str | None,
    origin: str,
    history: Callable[[], Sequence[Mapping[str, Any]]],
    rows: Callable[[], Mapping[str, Mapping[str, Any]]],
    titles: Callable[[], Collection[str]],
    collection_id: str = "",
    path: str = "",
    reach: Reach | None = None,
    automatic: Callable[[str, Plan, Sequence[Mapping[str, Any]], str | None, Sequence[Mapping[str, Any]]], None] | None = None,
) -> Resolved:
    """Turn a call's `steps` or `undo` into a plan, reading history, rows and titles only where a step needs them.

    A `declined` origin keeps the proposal and plans no change: it is not proved, because it is not applied.
    `collection_id`, `path` and `reach` name the called collection and how a move reaches others (see `plan`).
    `automatic` is the substrate's bound check of an automatic call, `automatic_collection.require_restructure_bounds`
    with its storage bound. It runs once, on the proven plan, with the starting manifest text, the steps, the entry
    undone and the shape history, and refuses before anything is written.
    """
    if (steps is None) == (undo is None):
        raise refuse("INVALID_RESTRUCTURE", "give steps or undo, and not both")
    if undo is not None and origin == "declined":
        raise refuse("INVALID_RESTRUCTURE", "an undo is applied, so it has no declined origin")
    entries = history() if undo is not None or automatic is not None else ()
    undoes = None
    if undo is not None:
        target = undo_target(entries, undo)
        undoes, run = target["id"], parse_steps(target["inverse"], allow_inverse=True)
    else:
        run = parse_steps(steps)
    if origin == "declined":
        return Resolved(run, undoes, Plan(text, {}, (), run, ()))
    planned = plan(
        text, rows() if reads_rows(run) else {}, run, titles=titles() if needs_titles(run) else frozenset(),
        collection_id=collection_id, path=path, reach=reach,
    )
    if automatic is not None:
        automatic(text, planned, planned.steps, undoes, entries)
    return Resolved(tuple(planned.steps), undoes, planned)


def require_one_call(planned: Plan) -> None:
    """Refuse a plan that writes and retracts more rows together than one call commits, on either storage mode.

    A move counts each row it carries twice, once where it retracts the row and once where it writes it.
    """
    written = len(planned.changed_rows) + sum(len(change.written) for change in planned.others)
    retracted = len(planned.retracted) + sum(len(change.retracted) for change in planned.others)
    require_rows(written + retracted)


def require_rows(count: int) -> None:
    """Refuse `count` written and retracted rows past what one call commits."""
    if count > MAX_REWRITTEN_ROWS:
        raise refuse(
            "RESTRUCTURE_TOO_MANY_ROWS",
            f"one call writes and retracts at most {MAX_REWRITTEN_ROWS} rows together, and this restructure "
            f"would write and retract {count}",
            rows_total=count, maximum=MAX_REWRITTEN_ROWS,
            remediation="add fields only, which writes no rows and works at any size; otherwise the collection keeps "
                        "its current shape",
        )


def dependent_refusal(
    text: str, steps: Sequence[Mapping[str, Any]], cause: collections.CollectionError
) -> collections.CollectionError:
    """The refusal for a proposal that a dependent of the changed part no longer validates under.

    It names the step, the dependent the parser or validator named, and where the manifest still names the field.
    """
    named = [step.get("field") for step in steps if isinstance(step.get("field"), str)]
    document = ManifestEdit(text).document
    places = [place for name in named for place in mentions(document, name)]
    return refuse(
        "RESTRUCTURE_DEPENDENT_BROKEN",
        f"a dependent would no longer validate: {cause.reason}" + (f" ({', '.join(places[:_ROWS_NAMED])})" if places else ""),
        cause=cause.code, dependents=places[:_ROWS_NAMED], **{k: v for k, v in cause.details.items() if k != "cause"},
    )


def references_lost(
    source: tuple[str, str] | None, field: str, cause: collections.CollectionError | None = None,
) -> collections.CollectionError:
    """The refusal of a restructure after which a declared reference would no longer find its rows, naming it.

    `source` is the referring collection's id and title, and `field` the reference's place in that manifest. `cause`
    is the target check that the proposed manifest fails; without one, rows that references find would leave. A
    collection the caller cannot read is not named, nor is its field.
    """
    where = f"collection {source[1]} references this collection through {field}" if source else (
        "a collection you cannot read references this collection")
    lost = f"and {cause.reason}" if cause else "and this restructure takes away or renames rows that those references find"
    return refuse(
        "RESTRUCTURE_DEPENDENT_BROKEN",
        f"{where}, {lost}; nothing changed",
        **({"cause": cause.code} if cause else {}),
        **({"collection": source[0], "dependents": [field]} if source else {}),
        remediation="leave the referenced rows and key as they are, or change those references first",
    )


ENTRY_KEYS = frozenset({"version", "id", "collection_id", "at", "origin", "why", "steps", "inverse", "undoes"})
# A file-only vault keeps its shape history in the activity log, one line per entry under this prefix, in the form of
# the audit lines it sits beside. The prefix differs from every audit prefix, so no audit reader takes the line.
SHAPE_LINE_PREFIX = "Collection shape-v1 "


def call_origin(origin: Any, *, automatic: bool, allowed: Sequence[str] = CALLER_ORIGINS) -> str:
    """The origin a call records: `automatic` for a call that carries the flag, else `parse_origin`.

    The flag stands for the origin, so a call that carries it names none. The facade passes the origin it resolved down
    to the substrate, which resolves it again, so `automatic` itself is accepted beside the flag.
    """
    if not automatic:
        return parse_origin(origin, allowed=allowed)
    if origin not in (None, "automatic"):
        raise refuse("INVALID_RESTRUCTURE", "an automatic restructure names no origin")
    return "automatic"


def parse_origin(origin: Any, *, allowed: Sequence[str] = CALLER_ORIGINS) -> str:
    """`origin`, or the default when absent. A caller may name only `CALLER_ORIGINS`; the server's own writes pass `ORIGINS`."""
    if origin is None:
        return DEFAULT_ORIGIN
    if origin not in allowed:
        raise refuse("INVALID_RESTRUCTURE", f"origin is one of {', '.join(allowed)}")
    return origin


def entry(
    *, transition_id: str, collection_id: str, at: str, origin: str, why: str, steps: Sequence[Mapping[str, Any]],
    inverse: Sequence[Mapping[str, Any]], undoes: str | None = None,
) -> dict[str, Any]:
    """One shape-history entry. Its id is the transition that committed it; its undo call is read from the id."""
    return {
        "version": 1, "id": transition_id, "collection_id": collection_id, "at": at, "origin": origin, "why": why,
        "steps": [dict(step) for step in steps], "inverse": [dict(step) for step in inverse], "undoes": undoes,
    }


def creation_entry(*, transition_id: str, collection_id: str, at: str, origin: str, why: str) -> dict[str, Any]:
    """A collection's first entry: who designed its first shape. It has no steps, so it has no inverse to undo."""
    return entry(transition_id=transition_id, collection_id=collection_id, at=at, origin=origin, why=why, steps=[], inverse=[])


def not_read_back(rows: Sequence[str]) -> collections.CollectionError:
    """Refuse a rewrite whose stored rows do not read back as the plan wrote them, naming those rows."""
    return refuse(
        "RESTRUCTURE_NOT_LOSSLESS",
        f"{len(rows)} rewritten row(s) do not read back as planned: {', '.join(rows[:_ROWS_NAMED])}",
        rows=list(rows[:_ROWS_NAMED]), rows_total=len(rows),
    )


def bound_to_row(rows: Sequence[str]) -> collections.CollectionError:
    """Refuse a move of rows whose body carries origin notes: they are bound to the row they were written to."""
    return refuse(
        "RESTRUCTURE_STEP_REFUSED",
        f"{len(rows)} row(s) carry origin notes bound to where they were written, so they cannot move: "
        f"{', '.join(rows[:_ROWS_NAMED])}",
        rows=list(rows[:_ROWS_NAMED]), rows_total=len(rows),
    )


def adds_only(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether every step adds a field: the automatic daily bound does not count such an entry.

    A creation entry has no steps, so it holds vacuously: the create has its own daily bound and marker.
    """
    return all(step["step"] == "add_field" for step in steps)


def log_line(shape: Mapping[str, Any]) -> str:
    """The activity-log line of one entry: a machine JSON body that holds the whole entry."""
    return SHAPE_LINE_PREFIX + json.dumps(shape, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def entries_by_collection(lines: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """The entries of every collection that `lines` name, each list newest first, from one parse of the lines.

    A line that does not parse raises: a history read never skips one. The log is read oldest-to-newest inside each
    file, so the lines are reversed and then ordered by their stamp. The sort is stable, so entries of one second keep
    their order in the file.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    for line in reversed(lines):
        shape = json.loads(line.removeprefix(SHAPE_LINE_PREFIX))
        if not isinstance(shape, dict) or set(shape) != ENTRY_KEYS or shape["origin"] not in ORIGINS:
            raise ValueError("a shape-history line is not an entry")
        found.setdefault(shape["collection_id"], []).append(shape)
    return {cid: sorted(items, key=lambda item: item["at"], reverse=True) for cid, items in found.items()}


def entries_from_lines(lines: Sequence[str], collection_id: str) -> list[dict[str, Any]]:
    """The entries of `collection_id`, newest first (see `entries_by_collection`)."""
    return entries_by_collection(lines).get(collection_id, [])


def undo_target(history: Sequence[Mapping[str, Any]], entry_id: Any) -> Mapping[str, Any]:
    """The entry an undo reverses, or the refusal that names why none can be."""
    target = next((item for item in history if item["id"] == entry_id), None)
    if target is None:
        raise refuse("RESTRUCTURE_UNDO_REFUSED", "undo names no shape-history entry of this collection", undo=entry_id)
    if target["origin"] == "declined":
        raise refuse("RESTRUCTURE_UNDO_REFUSED", "that entry is a declined proposal and changed nothing", undo=entry_id)
    if not target["steps"]:
        raise refuse("RESTRUCTURE_UNDO_REFUSED", "that entry records the collection's creation, which has no undo", undo=entry_id)
    done = next((item for item in history if item["undoes"] == entry_id), None)
    if done is not None:
        raise refuse(
            "RESTRUCTURE_UNDO_REFUSED", f"that entry was already undone by {done['id']}", undo=entry_id, undone_by=done["id"]
        )
    return target


HISTORY_PAGE = 20
MAX_HISTORY_PAGE = 100
# An inspection reply is bounded (Planning's holds 64 KiB), so a page also stops at this size of entries.
_PAGE_BYTES = 24 * 1024


def page_limit(limit: int | None) -> int:
    """The page size a caller asked for, or the default; a store and a file vault bound it the same way."""
    limit = HISTORY_PAGE if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE:
        raise refuse("INVALID_RESTRUCTURE", f"limit is a whole number from 1 to {MAX_HISTORY_PAGE}")
    return limit


def not_found_after() -> collections.CollectionError:
    return refuse("INVALID_RESTRUCTURE", "continuation names no shape-history entry of this collection")


def undoable(item: Mapping[str, Any]) -> bool:
    """Whether an undo of the entry can apply: a creation (no steps) and a declined proposal changed nothing to undo."""
    return bool(item["steps"]) and item["origin"] != "declined"


def shown(items: Sequence[Mapping[str, Any]], collection: str) -> list[dict[str, Any]]:
    """The leading `items` that fit one reply, each undoable one with the call that undoes it; the first always shows.

    An entry that no undo can reverse carries no call: offering one would offer a call that always refuses.
    """
    fitted: list[dict[str, Any]] = []
    size = 0
    for item in items:
        shown_item = {**item, "undo_call": {"collection": collection, "undo": item["id"]}} if undoable(item) else dict(item)
        size += len(json.dumps(shown_item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if fitted and size > _PAGE_BYTES:
            break
        fitted.append(shown_item)
    return fitted


def page(
    entries: Sequence[Mapping[str, Any]], *, limit: int | None, after: str | None, collection: str
) -> dict[str, Any]:
    """A page of newest-first entries older than the entry `after`, each with the call that undoes it."""
    limit = page_limit(limit)
    start = 0
    if after is not None:
        position = next((index for index, item in enumerate(entries) if item["id"] == after), None)
        if position is None:
            raise not_found_after()
        start = position + 1
    fitted = shown(entries[start : start + limit], collection)
    result: dict[str, Any] = {"entries": fitted, "total": len(entries)}
    if start + len(fitted) < len(entries):
        result["continuation"] = fitted[-1]["id"]
    return result


def normalized_page(value: Any) -> dict[str, Any] | None:
    """`value` when it has exactly the shape `page` returns, copied; otherwise None, so egress admits nothing else."""
    if (
        not isinstance(value, Mapping)
        or not {"entries", "total"} <= set(value) <= {"entries", "total", "continuation"}
        or not isinstance(value["entries"], list)
        or len(value["entries"]) > MAX_HISTORY_PAGE
        or type(value["total"]) is not int
        or value["total"] < 0
        or ("continuation" in value and type(value["continuation"]) is not str)
    ):
        return None
    for item in value["entries"]:
        if (
            not isinstance(item, Mapping)
            or set(item) - {"undo_call"} != ENTRY_KEYS
            or item["origin"] not in ORIGINS
            or not all(type(item[name]) is str for name in ("id", "collection_id", "at", "why"))
            or not all(isinstance(item[name], list) for name in ("steps", "inverse"))
            # The call is present exactly when an undo of the entry can apply.
            or ("undo_call" in item) != undoable(item)
            or ("undo_call" in item and not isinstance(item["undo_call"], Mapping))
        ):
            return None
    return copy.deepcopy(dict(value))
