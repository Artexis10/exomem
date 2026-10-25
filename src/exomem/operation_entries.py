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

The selector and mode values are the closed selector of these two commands (their
`Literal` signatures and the egress selector registry), not vocabulary that a vault
or a customer defines. `assert_coverage` fails startup when the entries and that
registry drift apart.

The same entries also spell a call. `spell_call` turns a grouped-command call into
the tool and arguments that a surface of a given shape lists, and `unspell_call` turns
them back, so guidance on MCP never names a tool that the surface hides.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, Literal, get_args

Effect = Literal["read", "write"]

#: How the MCP surface publishes a command that has renderings. `nested` carries the
#: operation in one `operation` object, `flat` carries it as an `action` plus its fields,
#: and `grouped` lists only the grouped command. EXPERIMENTAL: the single-purpose-tools
#: pilot compares the three and then removes the losers.
ToolShape = Literal["nested", "flat", "grouped"]
TOOL_SHAPES: tuple[str, ...] = get_args(ToolShape)
DEFAULT_TOOL_SHAPE: ToolShape = "nested"


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


def selected_mode(
    mode_axes: Mapping[str, ModeAxis], action: str, arguments: Mapping[str, object]
) -> str | None:
    """The mode a call carries for this selector value; None when absent or malformed."""
    axis = mode_axes.get(action)
    if axis is None:
        return None
    container = arguments.get(axis.argument)
    mode = container.get(axis.key) if isinstance(container, Mapping) else None
    return mode if isinstance(mode, str) else None


@dataclass(frozen=True, slots=True)
class RenderingSpec:
    """A public tool spelling of a grouped command: one side of its operations."""

    name: str
    effect: Effect
    title: str
    description: str
    #: Top-level tool arguments beside `operation`; the command must take each one.
    tool_arguments: tuple[str, ...] = ()


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

    @property
    def kinds(self) -> tuple[str, ...]:
        """The selector values this tool carries, in table order."""
        return tuple(dict.fromkeys(variant.action for variant in self.variants))

    def arguments_for(self, entry: OperationEntry) -> dict[str, object]:
        """The grouped-command arguments that select this variant, and nothing else."""
        arguments: dict[str, object] = {self.selector: entry.action}
        if entry.mode is not None:
            axis = self.mode_axes[entry.action]
            arguments[axis.argument] = {axis.key: entry.mode}
        return arguments

    def modes_of(self, kind: str) -> tuple[str, ...]:
        """The modes this tool carries for one selector value; empty when it has no mode axis."""
        return tuple(
            variant.mode for variant in self.variants if variant.action == kind and variant.mode
        )

    def variant_for(self, kind: object, arguments: Mapping[str, object]) -> OperationEntry | None:
        """The variant this tool carries for a selector value and the mode in `arguments`."""
        if not isinstance(kind, str):
            return None
        mode = selected_mode(self.mode_axes, kind, arguments)
        return next(
            (v for v in self.variants if v.action == kind and v.mode == mode),
            None,
        )


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

    def renderings_with_entries(self) -> tuple[Rendering, ...]:
        """The renderings, each holding its own entries."""
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
                tool_arguments=spec.tool_arguments,
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
            # Success detail shapes a write's terminal response; a read has no such level.
            tool_arguments=("response_detail",),
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
            # Success detail shapes a write's terminal response; a read has no such level.
            tool_arguments=("response_detail",),
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
    command: str,
    renderings: Sequence[Rendering],
    *,
    actions: Iterable[str],
    modes: Mapping[str, Iterable[str]],
    is_read_only: Callable[[Rendering, OperationEntry], bool],
) -> None:
    """Fail unless every operation of `command` maps to exactly one variant of exactly one tool.

    `actions` and `modes` come from the registries that the lease and the importer
    already own, so a selector value or import mode added there without a variant
    fails here. `is_read_only` is the lease's own classifier: a variant in a read
    tool must classify as read and one in a write tool as a write, or the tool's
    annotation would disagree with whether its calls take writer authority.
    """
    problems: list[str] = []
    moded = {action for rendering in renderings for action in rendering.mode_axes}
    expected = {(action, None) for action in actions if action not in moded}
    for action in moded:
        expected |= {(action, mode) for mode in modes.get(action, ())}
    count: dict[tuple[str, str | None], int] = {}
    for rendering in renderings:
        for variant in rendering.variants:
            key = (variant.action, variant.mode)
            count[key] = count.get(key, 0) + 1
            if variant.effect != rendering.effect:
                problems.append(f"{key} is a {variant.effect} variant in {rendering.name}")
            if (variant.mode is None) == (variant.action in rendering.mode_axes):
                problems.append(f"{key} disagrees with the mode axis of its selector value")
            elif is_read_only(rendering, variant) != rendering.read_only:
                problems.append(f"{key} in {rendering.name} classifies against the tool's effect")
    problems += [f"{key} has {n} variants" for key, n in count.items() if n > 1]
    problems += [f"{key} has no variant" for key in sorted(expected - count.keys(), key=str)]
    problems += [f"{key} is not an operation" for key in sorted(count.keys() - expected, key=str)]
    if problems:
        raise RuntimeError(
            f"operation entries for {command} do not cover its selector: " + "; ".join(problems)
        )


# --- Spelling a call --------------------------------------------------------------------


@cache
def _renderings_of(command: str) -> tuple[Rendering, ...]:
    operations = OPERATIONS.get(command)
    return () if operations is None else operations.renderings_with_entries()


def _require_shape(shape: str) -> None:
    if shape not in TOOL_SHAPES:
        raise ValueError(f"tool shape must be one of {', '.join(TOOL_SHAPES)}; got {shape!r}")


def spellings(command: str) -> tuple[str, ...]:
    """Every public name a grouped command can be called by: itself, then its renderings."""
    return (command, *(rendering.name for rendering in _renderings_of(command)))


def listed_tools(command: str, shape: str, effect: Effect | None = None) -> tuple[str, ...]:
    """The tool names a surface of this shape lists for one grouped command.

    `effect` keeps one side's tools. A command without renderings, or any command on the
    `grouped` shape, is listed under its own name whatever the side asked for.
    """
    _require_shape(shape)
    renderings = _renderings_of(command) if shape != "grouped" else ()
    if not renderings:
        return (command,)
    return tuple(r.name for r in renderings if effect in (None, r.effect))


def selector_label(command: str, shape: str) -> str:
    """The argument that names the operation, as a client of this shape writes it."""
    _require_shape(shape)
    operations = OPERATIONS.get(command)
    if operations is None:
        raise ValueError(f"no operation entries for {command}")
    return "operation.kind" if shape == "nested" else operations.selector


def spell_call(
    command: str, arguments: Mapping[str, Any], shape: str
) -> tuple[str, dict[str, Any]]:
    """The tool and arguments that carry a grouped-command call on a surface of this shape.

    The inverse of `unspell_call`: a call that `resolve_invocation` accepts resolves back
    to `arguments`. A command without renderings, and the `grouped` shape, spell a call as
    itself. Raises ValueError for an operation that no rendering carries, because guidance
    that names a tool the surface cannot call must fail where it is written.
    """
    _require_shape(shape)
    renderings = _renderings_of(command) if shape != "grouped" else ()
    if not renderings:
        return command, dict(arguments)
    selector = OPERATIONS[command].selector
    kind = arguments.get(selector)
    fields = {key: value for key, value in arguments.items() if key != selector}
    for rendering in renderings:
        if rendering.variant_for(kind, fields) is None:
            continue
        if shape == "nested":
            return rendering.name, {"operation": {"kind": kind, **fields}}
        return rendering.name, {selector: kind, **fields}
    raise ValueError(f"no rendering of {command} carries {selector}={kind!r}")


def call_text(command: str, arguments: Mapping[str, Any], shape: str) -> str:
    """A call in prose: the tool name and its arguments, as `tool(key='value', ...)`."""
    tool, spelled = spell_call(command, arguments, shape)
    return f"{tool}({', '.join(f'{key}={value!r}' for key, value in spelled.items())})"


def call_route(command: str, arguments: Mapping[str, Any], shape: str) -> dict[str, Any]:
    """A call as a payload route, `{"tool": ..., "args": ...}`, spelled for this shape."""
    tool, spelled = spell_call(command, arguments, shape)
    return {"tool": tool, "args": spelled}


def tools_text(command: str, shape: str, effect: Effect | None = None) -> str:
    """The tool names a surface of this shape lists for a command, for one line of prose."""
    return "/".join(listed_tools(command, shape, effect))


def route(command: str, shape: str, actions: Sequence[str]) -> dict[str, Any]:
    """The route a bootstrap advertises for some operations of a grouped command.

    A command without renderings, and the `grouped` shape, name one tool. Otherwise each
    listed tool names the operations it carries, and `selector` names the argument that
    holds the operation. Raises ValueError for an operation that no rendering carries,
    because a route to a call the surface cannot take must fail where it is written.
    """
    _require_shape(shape)
    renderings = _renderings_of(command) if shape != "grouped" else ()
    if not renderings:
        return {"tool": command, "actions": list(actions)}
    by_tool = {r.name: [a for a in actions if a in r.kinds] for r in renderings}
    missing = [a for a in actions if not any(a in carried for carried in by_tool.values())]
    if missing:
        raise ValueError(f"no rendering of {command} carries {', '.join(missing)}")
    return {"tools": by_tool, "selector": selector_label(command, shape)}


def split_operation(
    rendering: Rendering, shape: str, body: Mapping[str, Any]
) -> tuple[Any, dict[str, Any]] | None:
    """The operation kind and its fields from a rendering call's arguments, or None.

    `body` holds the arguments without the transport keys and the tool's own extra
    arguments. A nested call must carry one `operation` object and nothing else.
    """
    if shape == "nested":
        operation = body.get("operation")
        if set(body) != {"operation"} or not isinstance(operation, Mapping):
            return None
        return operation.get("kind"), {k: v for k, v in operation.items() if k != "kind"}
    return body.get(rendering.selector), {k: v for k, v in body.items() if k != rendering.selector}


def unspell_call(
    tool: str, arguments: Mapping[str, Any], shape: str
) -> tuple[str, dict[str, Any]] | None:
    """The grouped command and flat arguments that a tool call of this shape stands for.

    None when `tool` is not a name this shape lists for a converted command, or when the
    arguments do not select an operation that the tool carries. It checks which operation
    the call names and nothing about the operation's own fields: the grouped command's
    argument rules do that when the call runs. A tool's own arguments beside the operation
    cross into the flat arguments as they do on the server.
    """
    _require_shape(shape)
    for command, operations in OPERATIONS.items():
        if shape == "grouped":
            if tool != command:
                continue
            kind = arguments.get(operations.selector)
            mode = (
                selected_mode(operations.mode_axes, kind, arguments) if isinstance(kind, str) else None
            )
            if not any(entry.action == kind and entry.mode == mode for entry in operations.entries):
                return None
            return command, dict(arguments)
        for rendering in _renderings_of(command):
            if rendering.name != tool:
                continue
            body = dict(arguments)
            tool_arguments = {key: body.pop(key) for key in rendering.tool_arguments if key in body}
            parsed = split_operation(rendering, shape, body)
            if parsed is None:
                return None
            kind, fields = parsed
            if rendering.variant_for(kind, fields) is None:
                return None
            return command, {**fields, **tool_arguments, operations.selector: kind}
    return None


def write_selectors() -> tuple[str, ...]:
    """Every `tool:operation` selector that always writes, for a hook that cannot import this module.

    A rendering selector names a write operation of a change tool, whatever the host calls
    it. A grouped selector leaves out an operation that is a read in some modes (an import
    preview against an import start), because the operation alone does not say which. A
    standalone hook stamps this list, and a test fails when the hook and these entries drift.
    """
    selectors: set[str] = set()
    for command, operations in OPERATIONS.items():
        reads = {entry.action for entry in operations.entries if entry.effect == "read"}
        for entry in operations.entries:
            if entry.effect != "write":
                continue
            selectors.add(f"{entry.rendering}:{entry.action}")
            if entry.action not in reads:
                selectors.add(f"{command}:{entry.action}")
    return tuple(sorted(selectors))
