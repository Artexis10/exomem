"""Operation entries: what each Records and Planning operation accepts, and which tool carries it.

A grouped product command (`record_memory`, `plan_memory`) takes one selector
(`action`) and the union of every operation's fields. This module is the one
table of that structure. Each entry names one operation by its selector value
and, where the selector has a mode axis, its mode: the fields it accepts, the
fields it requires, whether it reads or writes, and whether a write can
overwrite or remove content. The leaves' own argument checks, the MCP renderings
that split reads from writes, and the startup coverage check all read these
entries.

The module is data and pure functions with no import of the leaves or the
command registry, so a leaf can import its tables from here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Effect = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class OperationEntry:
    """One operation of a grouped command: a selector value, plus its mode if it has one."""

    action: str
    rendering: str
    effect: Effect
    fields: frozenset[str]
    required: frozenset[str]
    #: A write that can overwrite or remove existing content. Additive writes stay False.
    destructive: bool = False
    mode: str | None = None

    def __post_init__(self) -> None:
        if not self.required <= self.fields:
            raise ValueError(f"required fields outside the accepted fields: {self.action}")
        if self.effect == "read" and self.destructive:
            raise ValueError(f"a read operation cannot be destructive: {self.action}")


@dataclass(frozen=True, slots=True)
class ModeAxis:
    """A selector value whose mode sits inside one argument, and so splits it across tools."""

    argument: str
    key: str


@dataclass(frozen=True, slots=True)
class RenderingSpec:
    """A public tool spelling of a grouped command: one side of its operations."""

    name: str
    effect: Effect
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class Rendering:
    """A rendering with the entries it carries, in the order the table lists them."""

    name: str
    command: str
    selector: str
    effect: Effect
    title: str
    description: str
    variants: tuple[OperationEntry, ...]
    mode_axes: Mapping[str, ModeAxis]
    #: Arguments the tool takes beside `operation`, from the grouped command's own signature.
    tool_arguments: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return self.effect == "read"

    @property
    def destructive(self) -> bool:
        """True when any variant can overwrite or remove content; always False for a read tool."""
        return any(variant.destructive for variant in self.variants)

    @property
    def fields(self) -> frozenset[str]:
        """Every field some variant of this tool accepts."""
        return frozenset().union(*(variant.fields for variant in self.variants))


@dataclass(frozen=True, slots=True)
class CommandOperations:
    """Everything the entries say about one grouped command."""

    command: str
    selector: str
    entries: tuple[OperationEntry, ...]
    renderings: tuple[RenderingSpec, ...]
    mode_axes: Mapping[str, ModeAxis]

    def actions(self) -> frozenset[str]:
        return frozenset(entry.action for entry in self.entries)

    def every_field(self) -> frozenset[str]:
        """Every field some operation accepts, which is the grouped command's whole signature."""
        return frozenset().union(*(entry.fields for entry in self.entries))

    def action_fields(self) -> dict[str, frozenset[str]]:
        """Accepted fields per selector value; every mode of one value shares one set."""
        return self._per_action("fields")

    def required_fields(self) -> dict[str, frozenset[str]]:
        """Required fields per selector value; every mode of one value shares one set."""
        return self._per_action("required")

    def _per_action(self, attribute: str) -> dict[str, frozenset[str]]:
        table: dict[str, frozenset[str]] = {}
        for entry in self.entries:
            value = getattr(entry, attribute)
            if table.setdefault(entry.action, value) != value:
                raise ValueError(
                    f"modes of one {self.command} operation disagree on {attribute}: {entry.action}"
                )
        return table

    def arguments_for(self, entry: OperationEntry) -> dict[str, object]:
        """The selector arguments that make a grouped call resolve to this entry."""
        arguments: dict[str, object] = {self.selector: entry.action}
        if entry.mode is not None:
            axis = self.mode_axes[entry.action]
            arguments[axis.argument] = {axis.key: entry.mode}
        return arguments

    def mode_of(self, action: str, arguments: Mapping[str, object]) -> str | None:
        """The mode a call carries for this selector value; None when absent or malformed."""
        axis = self.mode_axes.get(action)
        if axis is None:
            return None
        container = arguments.get(axis.argument)
        mode = container.get(axis.key) if isinstance(container, Mapping) else None
        return mode if isinstance(mode, str) else None

    def renderings_with_entries(
        self, tool_arguments: Mapping[str, tuple[str, ...]] | None = None
    ) -> tuple[Rendering, ...]:
        """The renderings, each holding its own entries."""
        extra = tool_arguments or {}
        return tuple(
            Rendering(
                name=spec.name,
                command=self.command,
                selector=self.selector,
                effect=spec.effect,
                title=spec.title,
                description=spec.description,
                variants=tuple(entry for entry in self.entries if entry.rendering == spec.name),
                mode_axes=self.mode_axes,
                tool_arguments=extra.get(spec.name, ()),
            )
            for spec in self.renderings
        )


def _entry(
    action: str,
    rendering: str,
    fields: Iterable[str],
    required: Iterable[str] = (),
    *,
    effect: Effect,
    destructive: bool = False,
    mode: str | None = None,
) -> OperationEntry:
    return OperationEntry(
        action=action,
        rendering=rendering,
        effect=effect,
        fields=frozenset(fields),
        required=frozenset(required),
        destructive=destructive,
        mode=mode,
    )


READ_RECORDS = "read_records"
CHANGE_RECORDS = "change_records"
READ_PLANS = "read_plans"
CHANGE_PLANS = "change_plans"

_RECORD_IMPORT_FIELDS = ("collection", "import_request")
_RECORD_QUERY_FIELDS = (
    "collection", "view", "filters", "columns", "sort_by", "descending", "limit",
    "aggregate", "date_from", "date_to", "date_column", "expand_children", "expand_child",
    "continuation", "include_agent_history", "output_format", "query",
)
_MANIFEST_REVISION_FIELDS = (
    "collection", "manifest_text", "expected_manifest_hash", "expected_container_hash", "why",
)
_REBASELINE_FIELDS = (
    "collection", "expected_manifest_hash", "expected_container_hash",
    "acknowledged_gap_codes", "why",
)

RECORDS = CommandOperations(
    command="record_memory",
    selector="action",
    mode_axes=MappingProxyType({"import": ModeAxis("import_request", "mode")}),
    renderings=(
        RenderingSpec(
            READ_RECORDS,
            "read",
            "Read records",
            "Read human-owned Records without changing them: describe the manifest contract, "
            "validate or inspect a collection, query rows, read an item's history, or preview "
            "or check an import. `operation.kind` selects the operation.",
        ),
        RenderingSpec(
            CHANGE_RECORDS,
            "write",
            "Change records",
            "Change human-owned Records: create a collection, append, update or bulk-upsert "
            "items, discard a held candidate, revise or rebaseline a manifest, or start or "
            "cancel an import. `operation.kind` selects the operation. Guarded writes need the "
            "hashes that the matching read returns.",
        ),
    ),
    entries=(
        _entry("describe", READ_RECORDS, (), effect="read"),
        _entry("inspect", READ_RECORDS, ("collection",), effect="read"),
        _entry(
            "validate", READ_RECORDS,
            ("collection", "manifest_path", "manifest_text", "scaffold"), ("manifest_text",),
            effect="read",
        ),
        _entry("query", READ_RECORDS, _RECORD_QUERY_FIELDS, ("collection",), effect="read"),
        _entry(
            "history", READ_RECORDS, ("collection", "item_key", "limit", "continuation"),
            ("collection", "item_key"), effect="read",
        ),
        _entry(
            "import", READ_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="read", mode="preview",
        ),
        _entry(
            "import", READ_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="read", mode="status",
        ),
        _entry(
            "create", CHANGE_RECORDS, ("manifest_path", "manifest_text", "why", "scaffold"),
            ("manifest_path", "manifest_text", "why"), effect="write",
        ),
        # `item` is required only when the caller is not resuming a held candidate,
        # which the leaf's argument rules say in as many words.
        _entry(
            "append", CHANGE_RECORDS,
            (
                "collection", "item", "why", "item_key", "expected_container_hash", "body",
                "delivery", "held", "hold",
            ),
            ("collection", "why"), effect="write",
        ),
        _entry(
            "update", CHANGE_RECORDS,
            (
                "collection", "item_key", "changes", "expected_container_hash",
                "expected_item_version", "why", "refresh_presentation", "held", "hold",
            ),
            ("collection", "item_key", "expected_container_hash", "expected_item_version", "why"),
            effect="write", destructive=True,
        ),
        _entry(
            "bulk_upsert", CHANGE_RECORDS,
            ("collection", "rows", "why", "expected_container_hash", "source", "on_reject"),
            ("collection", "rows", "why", "expected_container_hash"),
            effect="write", destructive=True,
        ),
        _entry(
            "discard", CHANGE_RECORDS, ("collection", "held", "why"),
            ("collection", "held", "why"), effect="write", destructive=True,
        ),
        _entry(
            "revise", CHANGE_RECORDS, _MANIFEST_REVISION_FIELDS, _MANIFEST_REVISION_FIELDS,
            effect="write", destructive=True,
        ),
        _entry(
            "rebaseline", CHANGE_RECORDS, _REBASELINE_FIELDS, _REBASELINE_FIELDS,
            effect="write", destructive=True,
        ),
        # A start replaces the values of rows it re-imports; cancelling a job removes nothing.
        _entry(
            "import", CHANGE_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="write", destructive=True, mode="start",
        ),
        _entry(
            "import", CHANGE_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="write", mode="cancel",
        ),
    ),
)

_PLAN_QUERY_FIELDS = (
    "collection", "view", "filters", "columns", "sort_by", "descending", "limit",
    "aggregate", "date_from", "date_to", "date_column", "lifecycle", "hierarchy_mode",
    "hierarchy_depth", "hierarchy_limit", "continuation", "include_agent_history",
    "output_format",
)
_PLAN_GUARDED_FIELDS = (
    "collection", "plan_id", "expected_container_hash", "expected_item_version", "why",
)

PLANS = CommandOperations(
    command="plan_memory",
    selector="action",
    mode_axes=MappingProxyType({}),
    renderings=(
        RenderingSpec(
            READ_PLANS,
            "read",
            "Read plans",
            "Read human-owned Planning intent without changing it: list or inspect plan "
            "collections, validate a manifest, query plan items, or read an item's history. "
            "`operation.kind` selects the operation.",
        ),
        RenderingSpec(
            CHANGE_PLANS,
            "write",
            "Change plans",
            "Change human-owned Planning intent: create a collection, add, update or triage a "
            "plan item, or revise or rebaseline a manifest. `operation.kind` selects the "
            "operation. Writes select one queried item and need the hashes that the read "
            "returned.",
        ),
    ),
    entries=(
        _entry("inspect", READ_PLANS, ("collection",), effect="read"),
        _entry(
            "validate", READ_PLANS, ("collection", "manifest_path", "manifest_text"),
            ("manifest_text",), effect="read",
        ),
        _entry("query", READ_PLANS, _PLAN_QUERY_FIELDS, ("collection",), effect="read"),
        _entry(
            "history", READ_PLANS, ("collection", "plan_id", "limit", "continuation"),
            ("collection", "plan_id"), effect="read",
        ),
        _entry(
            "create", CHANGE_PLANS, ("manifest_path", "manifest_text", "why", "scaffold"),
            ("manifest_path", "manifest_text", "why"), effect="write",
        ),
        _entry(
            "add", CHANGE_PLANS,
            ("collection", "item", "plan_id", "expected_container_hash", "body", "why"),
            ("collection", "item", "why"), effect="write",
        ),
        _entry(
            "update", CHANGE_PLANS,
            (*_PLAN_GUARDED_FIELDS, "changes", "body"), _PLAN_GUARDED_FIELDS,
            effect="write", destructive=True,
        ),
        _entry(
            "triage", CHANGE_PLANS, (*_PLAN_GUARDED_FIELDS, "transition"),
            (*_PLAN_GUARDED_FIELDS, "transition"), effect="write", destructive=True,
        ),
        _entry(
            "revise", CHANGE_PLANS, _MANIFEST_REVISION_FIELDS, _MANIFEST_REVISION_FIELDS,
            effect="write", destructive=True,
        ),
        _entry(
            "rebaseline", CHANGE_PLANS, _REBASELINE_FIELDS, _REBASELINE_FIELDS,
            effect="write", destructive=True,
        ),
    ),
)

#: Every grouped command that has renderings, by command name.
OPERATIONS: Mapping[str, CommandOperations] = MappingProxyType(
    {operations.command: operations for operations in (RECORDS, PLANS)}
)


def assert_coverage(
    operations: CommandOperations,
    *,
    actions: Iterable[str],
    modes: Mapping[str, Iterable[str]],
    is_read_only: Callable[[OperationEntry], bool],
) -> None:
    """Fail unless every operation maps to exactly one variant of exactly one tool.

    `actions` and `modes` come from the registries that the lease and the importer
    already own, so a selector value or import mode added there without an entry
    fails here. `is_read_only` is the lease's own classifier: a read variant must
    classify as read and a write variant as write, or a tool's annotation would
    disagree with whether it takes writer authority.
    """
    problems: list[str] = []
    expected = {(action, None) for action in actions if action not in operations.mode_axes}
    for action in operations.mode_axes:
        expected |= {(action, mode) for mode in modes.get(action, ())}
    seen: dict[tuple[str, str | None], int] = {}
    specs = {spec.name: spec for spec in operations.renderings}
    for entry in operations.entries:
        key = (entry.action, entry.mode)
        seen[key] = seen.get(key, 0) + 1
        spec = specs.get(entry.rendering)
        if spec is None:
            problems.append(f"{key} names unknown tool {entry.rendering}")
        elif spec.effect != entry.effect:
            problems.append(f"{key} is a {entry.effect} variant in {spec.effect} tool {spec.name}")
        if (entry.mode is None) != (entry.action not in operations.mode_axes):
            problems.append(f"{key} disagrees with the mode axis of its selector value")
        if is_read_only(entry) != (entry.effect == "read"):
            problems.append(f"{key} is declared {entry.effect} but the lease classifies it otherwise")
    problems += [f"{key} has {count} variants" for key, count in seen.items() if count > 1]
    problems += [f"{key} has no variant" for key in sorted(expected - seen.keys(), key=str)]
    problems += [f"{key} is not a known operation" for key in sorted(seen.keys() - expected, key=str)]
    try:
        operations.action_fields()
        operations.required_fields()
    except ValueError as error:
        problems.append(str(error))
    if problems:
        raise RuntimeError(
            f"operation entries for {operations.command} do not cover its selector: "
            + "; ".join(problems)
        )
