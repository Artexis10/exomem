"""The agent-facing tool surface: typed tools, two doors, and the calls that reach them.

An agent sees 20 typed tools (`public_tools`), `recall_context`, and two doors:
`inspect_memory` (reads) and `manage_memory` (writes). A door takes
`{"operation": "<area>.<action>", "arguments": {...}}`, and `operation` is a closed enum.

Nothing here declares an operation a second time. A typed tool names the grouped commands it
stands for. Every other operation of a converted command is a door operation, named from its
grouped command and kind (`operation_entries`). Each door operation routes through its nested
rendering, which stays an internal routing record: it is neither listed nor callable, and
`command_surface.resolve_invocation` still checks it against the command's own argument rules.
Authorization, the writer lease, egress and the ledger see the grouped command as they always did.
"""

from __future__ import annotations

import functools
import inspect
import json
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import get_close_matches
from typing import Any

from . import operation_entries
from .operation_entries import Effect, Rendering
from .public_tools import TOOLS, Context, Refusal, WireTool

#: The tools that keep their grouped command's own spelling and arguments: the recall contract
#: and the two Cloud transfer tools. They are listed as the command registry publishes them.
PASSTHROUGH_COMMANDS: dict[str, str] = {  # nosemgrep: ep-word-dict -- closed public names of the commands that keep their own spelling.
    "activate_context": "recall_context",
    "observe_transfer": "observe_transfer",
    "transfer_memory": "transfer_memory",
}

DOORS: dict[Effect, str] = {"read": "inspect_memory", "write": "manage_memory"}  # nosemgrep: ep-word-dict -- the two door names, one per effect.

#: Operations a door never carries because a typed tool or another operation replaces them.
#: `audit` is a maintenance finding list; `compilation` is `compile_source`'s draft.
_DROPPED: frozenset[tuple[str, str]] = frozenset({  # nosemgrep: ep-word-set -- closed review modes the surface retires.
    ("review_memory", "audit"), ("review_memory", "compilation"),
})

#: The area that prefixes each converted command's door operations. A closed API name set.
_AREAS: dict[str, str] = {  # nosemgrep: ep-word-dict -- the closed area names of the door operations.
    "configure_memory": "settings", "coordination_status": "writer", "viewer_map": "graph",
    "query_dataset": "dataset", "browse_memory": "files", "manage_memory_file": "files",
    "episode_memory": "episodes", "compile_source": "sources", "process_media": "media",
    "read_media": "media", "review_memory": "review", "review_item_context": "review",
    "connect_memory": "connections", "adopt_vault": "imports", "adoption_studio": "imports",
    "maintain_memory": "maintenance", "triage_memory": "review", "schema_memory": "schema", "govern_memory": "governance",
    "record_memory": "records", "plan_memory": "plans", "approve_procedure": "procedures",
    "observe_memory": "observations", "bootstrap": "instructions",
}
#: Renderings that sit in another area than their command's.
_RENDERING_AREAS: dict[str, str] = {  # nosemgrep: ep-word-dict -- the closed rendering names whose area differs.
    "grant_session_access": "access", "change_session_access": "access",
    "manage_authorization_session": "session",
}
#: A door operation's action, where the kind alone would read badly or collide with a read twin.
_KIND_NAMES: dict[tuple[str, str], str] = {  # nosemgrep: ep-word-dict -- the closed grouped kinds whose door action differs.
    ("configure_memory", "settings"): "inspect", ("coordination_status", "writer-status"): "status",
    ("viewer_map", "graph"): "map", ("query_dataset", "dataset"): "query",
    ("browse_memory", "folder-list"): "folders",
    ("manage_memory_file", "files"): "list", ("manage_memory_file", "trash-list"): "trash",
    ("manage_memory_file", "propose-reclassification"): "reclassify",
    ("manage_memory_file", "validate-create"): "create", ("manage_memory_file", "validate-append"): "append",
    ("adopt_vault", "scan-only"): "scan",
    ("maintain_memory", "collections-store"): "collections", ("maintain_memory", "collections-cutover"): "cutover",
    ("maintain_memory", "maintenance-job"): "job", ("maintain_memory", "maintenance-job-cancel"): "cancel_job",
    ("maintain_memory", "collections-store-adopt-local"): "adopt_local",
    ("maintain_memory", "collections-store-export"): "export",
    ("review_memory", "question"): "question", ("review_item_context", "item-context"): "item_context",
    ("review_memory", "write-advisory-result"): "advisory_result",
}


def _triage_kind(arguments: Mapping[str, Any]) -> str:
    """The kind of a `triage_memory` call. The leaf tells them apart by the namespace of `ref`, which it owns."""
    from . import envelope, review_state

    ref = str(arguments.get("ref") or "")
    if arguments.get("action") == "decide-vocabulary":
        return "decide-vocabulary"
    if envelope.is_envelope_ref(ref):
        return "set-envelope"
    return "set-family" if review_state.is_family_ref(ref) else "decision"


#: Grouped commands whose renderings have no selector but several kinds: the kind a call stands for.
_KIND_OF_CALL: dict[str, Any] = {"triage_memory": _triage_kind}  # nosemgrep: ep-word-dict -- the one grouped command whose kinds its own ref namespaces select.


@dataclass(frozen=True, slots=True)
class DoorOp:
    name: str
    door: str
    command: str
    rendering: str
    kind: str
    effect: Effect


def _action_name(command: str, rendering: str, kind: str) -> str:
    named = _KIND_NAMES.get((command, kind))
    return named if named is not None else kind.replace("-", "_")


def _door_ops(renderings: Iterable[tuple[str, Rendering]]) -> dict[str, dict[str, DoorOp]]:
    doors: dict[str, dict[str, DoorOp]] = {name: {} for name in DOORS.values()}
    claimed = {pair for tool in TOOLS.values() for pair in tool.claims}
    for command, rendering in renderings:
        for kind in rendering.kinds:
            if (command, kind) in claimed or (command, kind) in _DROPPED:
                continue
            area = _RENDERING_AREAS.get(rendering.name) or _AREAS[command]
            # Curation kinds read as one workflow of their own, not as maintenance findings.
            if command == "maintain_memory" and kind.startswith("curation-"):
                area, action = "curation", kind.removeprefix("curation-").replace("-", "_")
            else:
                action = _action_name(command, rendering.name, kind)
            name = f"{area}.{action}"
            door = DOORS[rendering.effect]
            if name in doors[door]:
                raise RuntimeError(f"two operations share the door name {door}:{name}")
            doors[door][name] = DoorOp(name, door, command, rendering.name, kind, rendering.effect)
    return doors


@functools.cache
def _params(command: str) -> frozenset[str]:
    from . import commands as commands_module

    return next((frozenset(p.name for p in c.params) for c in commands_module.PRODUCT_COMMANDS if c.name == command), frozenset())


_CONTEXT = Context(_params)


class PublicSurface:
    """Every public tool that the converted commands and the typed table can serve."""

    def __init__(self, operations: Mapping[str, operation_entries.CommandOperations]) -> None:
        self.tools: Mapping[str, WireTool] = TOOLS
        self._renderings: dict[str, tuple[Rendering, ...]] = {
            command: ops.renderings_with_entries() for command, ops in operations.items()
        }
        self.doors = _door_ops((c, r) for c, rs in self._renderings.items() for r in rs)
        self._by_internal: dict[tuple[str, str, str], DoorOp] = {
            (op.command, op.rendering, op.kind): op for ops in self.doors.values() for op in ops.values()
        }
        self._check()

    def _check(self) -> None:
        """Every typed claim and every drop names a real entry, and no entry has two homes."""
        known = {(c, kind) for c, rs in self._renderings.items() for r in rs for kind in r.kinds}
        seen: dict[tuple[str, str], str] = {}
        for tool in self.tools.values():
            for pair in tool.claims:
                if pair not in known:
                    raise RuntimeError(f"{tool.name} claims {pair}, which no grouped command carries")
                if pair in seen:
                    raise RuntimeError(f"{pair} is claimed by {seen[pair]} and {tool.name}")
                seen[pair] = tool.name
        missing = sorted(_DROPPED - known)
        if missing:
            raise RuntimeError(f"dropped entries that no grouped command carries: {missing}")

    # --- Listing ----------------------------------------------------------------------------

    def listed_names(self, commands: Iterable[object]) -> tuple[str, ...]:
        """The tool names a surface that serves these commands lists, in discovery order."""
        available = {command.name for command in commands}
        names = [name for name, tool in self.tools.items() if self._served(tool, available)]
        for door in DOORS.values():
            if any(op.command in available for op in self.doors[door].values()):
                names.append(door)
        names.extend(public for command, public in PASSTHROUGH_COMMANDS.items() if command in available)
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _served(tool: WireTool, available: set[str]) -> bool:
        return all(route.command in available for route in tool.routes)

    def door_operations(self, door: str, commands: Iterable[object]) -> tuple[DoorOp, ...]:
        # A served command can carry fewer variants than the registry holds (Cloud and remote
        # admission withhold some), so an operation stays only while its variant is served.
        served = {
            command.name: {(rendering.name, kind) for rendering in command.renderings for kind in rendering.kinds}
            for command in commands
        }
        return tuple(
            op for op in self.doors[door].values() if (op.rendering, op.kind) in served.get(op.command, ())
        )

    # --- Calls ------------------------------------------------------------------------------

    def resolve(
        self, name: str, arguments: Mapping[str, Any], commands: Sequence[Any], *,
        strict: bool, passthrough: frozenset[str],
    ) -> tuple[Any, dict[str, Any]]:
        """The grouped command and flat arguments that a public call stands for.

        Raises `command_surface.OpError` for a call the surface cannot take. A door call goes
        through its internal rendering, so the rendering's refusal keeps its command.
        """
        from .cli_ops import OpError
        from .command_surface import resolve_command, resolve_invocation

        carried = {key: value for key, value in arguments.items() if key in passthrough}
        body = {key: value for key, value in arguments.items() if key not in passthrough}
        if name in self.tools:
            tool = self.tools[name]
            try:
                wire = tool.model.model_validate(body).model_dump(exclude_none=True)
            except Exception as error:  # noqa: BLE001 - pydantic's ValidationError; its input is withheld below
                raise _wire_refusal(name, error, tool=tool) from None
            try:
                command_name, leaf = tool.translate(wire, _CONTEXT)
            except Refusal as refusal:
                raise OpError(refusal.code, f"{name}: {refusal.message}",
                              details={"candidates": list(refusal.candidates)} if refusal.candidates else None,
                              ) from None
            command = resolve_command(command_name, commands)
            if command is None:
                raise OpError("INVALID_OPERATION", f"{name} is not available on this server.")
            leaf = self._checked(name, command, leaf, strict=strict)
            return command, {**leaf, **carried}
        door = next((effect for effect, label in DOORS.items() if label == name), None)
        if door is None:
            raise OpError("INVALID_OPERATION", f"{name} is not a tool of this server.")
        operations = {op.name: op for op in self.door_operations(name, commands)}
        operation, fields, tool_fields = _door_call(name, body, operations)
        op = operations[operation]
        nested = {"operation": {"kind": op.kind, **fields}, **tool_fields}
        wire = {**nested, **carried}
        try:
            return resolve_invocation(
                op.rendering, wire, commands, passthrough=passthrough, strict=strict, subject=f"{name} {operation}"
            )
        except OpError as error:
            if getattr(error, "command", None) is None:
                error.writes = op.effect == "write"
            else:
                error.wire_arguments = wire
            raise

    def _checked(self, name: str, command: Any, leaf: dict[str, Any], *, strict: bool) -> dict[str, Any]:
        """The leaf call after the grouped command's own argument rules and types accept it."""
        from .cli_ops import OpError
        from .command_surface import _type_errors

        operations = operation_entries.OPERATIONS.get(command.name)
        skip = ""
        if operations is not None:
            skip = operations.selector or ""
            action = leaf.get(operations.selector) if operations.selector else None
            entry = next((e for e in operations.entries if operations.selector is None or e.matches(action, leaf)), None)
            if entry is None:
                raise OpError("INVALID_OPERATION", f"{name} has no operation for {operations.selector}={action!r}.")
            fields = {key: value for key, value in leaf.items() if key not in {operations.selector, "response_detail"}}
            command.argument_rules.validate(entry.action, fields)
            unexpected = sorted(key for key, value in fields.items() if value is not None and key not in entry.fields)
            missing = sorted(key for key in entry.required if fields.get(key) is None)
            if unexpected:
                raise OpError("INVALID_ARGUMENT", f"{name}: unexpected {', '.join(unexpected)}.")
            if missing:
                raise OpError("MISSING_ARGUMENT", f"{name}: needs {', '.join(missing)}.")
        wrong = _type_errors(command, leaf, skip=skip, strict=strict)
        if wrong:
            raise OpError("INVALID_ARGUMENT", f"wrong type in {name}: {'; '.join(wrong)}")
        return leaf

    # --- Spelling ---------------------------------------------------------------------------

    def spell(self, command: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """The tool and arguments that carry a grouped-command call. Raises ValueError when none does."""
        if command in PASSTHROUGH_COMMANDS:
            return PASSTHROUGH_COMMANDS[command], dict(arguments)
        if command in self.tools:
            # Guidance names a typed tool by the preferred name of its grouped command: search_memory, create_note.
            command = self.tools[command].routes[0].command
        for tool in self.tools.values():
            wire = tool.spell(command, arguments)
            if wire is not None:
                return tool.name, wire
        for rendering in self._renderings.get(command, ()):
            selector = rendering.selector
            kind = arguments.get(selector) if selector else None
            fields = {key: value for key, value in arguments.items() if key != selector}
            named = _KIND_OF_CALL[command](arguments) if command in _KIND_OF_CALL else None
            variant = next(
                (entry for entry in rendering.variants
                 if (entry.kind == named if named is not None else selector is None or entry.action == kind)
                 and all(replace(item, implied=False).matches(fields) for item in entry.constraints)),
                None,
            )
            op = None if variant is None else self._by_internal.get((command, rendering.name, variant.kind))
            if op is None:
                continue
            tool_fields = rendering.take_tool_arguments(fields)
            if op.door == DOORS["write"] and "response_detail" in fields:
                # The write door carries the reply detail beside `operation`, not inside `arguments`.
                tool_fields["response_detail"] = fields.pop("response_detail")
            wire = {"operation": op.name, **({"arguments": rendering.wire_fields(fields)} if fields else {})}
            if "response_detail" in tool_fields:
                wire["detail"] = tool_fields["response_detail"]
            return op.door, wire
        selector = (operation_entries.OPERATIONS.get(command) or _ops_of(command))
        raise ValueError(f"no public tool carries {command} {arguments.get(getattr(selector, 'selector', None))!r}")

    def carries(self, command: str) -> bool:
        """Whether some public tool stands for this grouped command."""
        return (command in PASSTHROUGH_COMMANDS or command in self._renderings
                or any(route.command == command for tool in self.tools.values() for route in tool.routes))

    def tools_for(self, command: str, effect: Effect | None = None) -> tuple[str, ...]:
        """The tools that carry some operation of a grouped command, one side if asked."""
        if command in PASSTHROUGH_COMMANDS:
            return (PASSTHROUGH_COMMANDS[command],)
        names = [t.name for t in self.tools.values()
                 if any(r.command == command for r in t.routes) and effect in (None, t.effect)]
        for side, door in DOORS.items():
            if effect in (None, side) and any(op.command == command for op in self.doors[door].values()):
                names.append(door)
        return tuple(dict.fromkeys(names)) or (command,)

    def unspell(self, tool: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """The grouped command and flat arguments that a public call stands for, or None.

        It checks which operation the call names, and nothing about the operation's own fields:
        the grouped command's argument rules do that when the call runs.
        """
        if tool in PASSTHROUGH_COMMANDS.values():
            command = next(c for c, public in PASSTHROUGH_COMMANDS.items() if public == tool)
            return command, dict(arguments)
        if tool in self.tools:
            wire = self.tools[tool]
            try:
                body = wire.model.model_validate(dict(arguments)).model_dump(exclude_none=True)
                return wire.translate(body, _CONTEXT)
            except Exception:  # noqa: BLE001 - any refusal means this call stands for no grouped call
                return None
        if tool in DOORS.values():
            ops = self.doors[tool]
            try:
                operation, fields, tool_fields = _door_call(tool, dict(arguments), ops)
            except Exception:  # noqa: BLE001 - a malformed door call stands for no grouped call
                return None
            op = ops[operation]
            rendering = next(r for r in self._renderings[op.command] if r.name == op.rendering)
            try:
                fields = rendering.leaf_fields(fields)
            except ValueError:
                return None
            variant = rendering.variant_for(op.kind, fields)
            if variant is None:
                return None
            return op.command, {**rendering.selected_arguments(variant, fields), **tool_fields}
        return None


def _ops_of(command: str):
    return operation_entries.register_operations(command)


def _wire_refusal(name: str, error: Exception, *, tool: WireTool | None = None):
    """A refusal of a call whose shape no tool takes: the first failing field, its input withheld."""
    from pydantic import ValidationError

    from .cli_ops import OpError

    refusal = OpError("INVALID_ARGUMENT", f"{name} did not accept the call.")
    refusal.writes = tool is not None and tool.effect == "write"
    if isinstance(error, ValidationError):
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        known = sorted(tool.model.model_fields) if tool is not None else []
        if first["type"] == "extra_forbidden" and known:
            close = get_close_matches(str(first["loc"][-1]), known, n=3) or known[:5]
            refusal.details["candidates"] = close
        reason = {"missing": "is required", "extra_forbidden": "is not a field of this tool"}.get(
            first["type"], first["msg"]
        )
        refusal.message = f"{name}: {where or 'the call'} {reason}."
        refusal.args = (f"{refusal.code}: {refusal.message}",)
    return refusal


def _door_call(
    door: str, body: Mapping[str, Any], operations: Mapping[str, DoorOp],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The operation name, its fields and the tool-level fields of one door call."""
    from .cli_ops import OpError

    writes = door == DOORS["write"]
    extra = sorted(set(body) - {"operation", "arguments", *({"detail"} if writes else set())})
    operation = body.get("operation")
    if extra or not isinstance(operation, str):
        refusal = OpError("INVALID_ARGUMENT", f"{door} takes operation, arguments{' and detail' if writes else ''}.")
        refusal.writes = writes
        raise refusal
    if operation not in operations:
        refusal = OpError("INVALID_OPERATION", f"{door} has no operation {operation!r}.",
                          details={"candidates": get_close_matches(operation, sorted(operations), n=5)
                                   or sorted(operations)[:5]})
        refusal.writes = writes
        raise refusal
    fields = body.get("arguments")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except ValueError:
            fields = None
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        refusal = OpError("INVALID_ARGUMENT", f"{door} {operation}: arguments must be an object.")
        refusal.writes = writes
        raise refusal
    tool_fields = {"response_detail": body["detail"]} if body.get("detail") is not None else {}
    return operation, fields, tool_fields


# --- Help -------------------------------------------------------------------------------------


def help_for(topic: str | None, commands: Sequence[Any]) -> dict[str, Any]:
    """The arguments and one example of a tool, a door or one door operation, from the tables that route it."""
    from .cli_ops import OpError

    surface = public_surface()
    listed = surface.listed_names(commands)
    if topic is None:
        return {"topics": list(listed), "doors": {
            door: sorted({op.name.split(".")[0] for op in surface.door_operations(door, commands)})
            for door in (d for d in DOORS.values() if d in listed)
        }}
    if topic in surface.tools and topic in listed:
        tool = surface.tools[topic]
        return {"tool": topic, "arguments": tool_schema(tool)["properties"],
                **({"example": dict(tool.example)} if tool.example else {})}
    if topic in DOORS.values() and topic in listed:
        ops = surface.door_operations(topic, commands)
        areas: dict[str, list[str]] = {}
        for op in ops:
            area, action = op.name.split(".", 1)
            areas.setdefault(area, []).append(action)
        return {"tool": topic, "operations": areas,
                "next": "read_instructions(topic='<area>.<action>') lists one operation's arguments."}
    for door in DOORS.values():
        ops = {op.name: op for op in surface.door_operations(door, commands)}
        if topic in ops:
            return _door_help(surface, ops[topic], commands)
    names = sorted({*listed, *(op.name for door in DOORS.values() for op in surface.door_operations(door, commands))})
    raise OpError("INVALID_OPERATION", f"{topic!r} is not a tool or an operation of this server.",
                  details={"candidates": get_close_matches(topic, names, n=5) or list(listed)[:5]})


def _door_help(surface: PublicSurface, op: DoorOp, commands: Sequence[Any]) -> dict[str, Any]:
    from .operation_schema import operation_schema

    command = next(c for c in commands if c.name == op.command)
    rendering = next(r for r in command.renderings if r.name == op.rendering)
    # The leaf's annotations are strings (postponed evaluation), so resolve them before they become a schema.
    hints = typing.get_type_hints(command.leaf, include_extras=True)
    parameters = {name: parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY,
                                          annotation=hints.get(name, parameter.annotation))
                  for name, parameter in inspect.signature(command.leaf).parameters.items()
                  if name in {p.name for p in command.params}}
    branches = operation_schema(rendering, parameters)["oneOf"]
    branch = dict(zip(rendering.kinds, branches, strict=True))[op.kind]
    properties = {key: value for key, value in branch.get("properties", {}).items() if key != "kind"}
    required = [key for key in branch.get("required", []) if key != "kind"]
    result: dict[str, Any] = {"tool": op.door, "operation": op.name, "arguments": properties, "required": required}
    if "description" in branch:
        result["note"] = branch["description"]
    if "anyOf" in branch:
        result["alternatives"] = branch["anyOf"]
    result["example"] = {"operation": op.name, "arguments": {key: f"<{key}>" for key in required}}
    return result


# --- Published schemas ----------------------------------------------------------------------------


def _without_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _without_titles(value) for key, value in node.items()
                if not (key == "title" and isinstance(value, str))}
    if isinstance(node, list):
        return [_without_titles(item) for item in node]
    return node


@functools.cache
def _tool_schema(name: str) -> str:
    from .command_surface import compact_input_schema
    from .edit_operations import inline_schema

    tool = TOOLS[name]
    schema = _without_titles(inline_schema(tool.model.model_json_schema()))
    schema["additionalProperties"] = False
    return json.dumps(compact_input_schema(schema))


def tool_schema(tool: WireTool) -> dict[str, Any]:
    return json.loads(_tool_schema(tool.name))


def door_schema(door: str, operations: Sequence[DoorOp]) -> dict[str, Any]:
    writes = door == DOORS["write"]
    properties: dict[str, Any] = {
        "operation": {"type": "string", "enum": [op.name for op in operations],
                      "description": "area.action. read_instructions(topic=<operation>) lists its arguments."},
        "arguments": {"type": "object", "description": "The operation's fields, as read_instructions lists them."},
    }
    if writes:
        properties["detail"] = {"type": "string", "enum": ["compact", "full"], "description": "compact (default) or full."}
    return {"type": "object", "properties": properties, "required": ["operation"], "additionalProperties": False}


_DOOR_TEXT = {
    "read": ("Inspect memory",
             "Read-only operations that no typed tool covers: files and folders, schema and governance, imports, "
             "maintenance findings, connections, review context, episodes, media, and collection and plan details."),
    "write": ("Manage memory",
              "Operations that change memory and that no typed tool covers: files, schema, governance, imports, "
              "maintenance repairs, connections, episodes, media and collection and plan structure. Preview a change "
              "with the inspect_memory operation of the same name when it has one."),
}


def build_tools(commands: Sequence[Any]) -> dict[str, Any]:
    """The FunctionTools that discovery lists for typed tools and doors."""
    from fastmcp.tools import FunctionTool
    from mcp.types import ToolAnnotations

    surface = public_surface()
    listed = surface.listed_names(commands)
    tools: dict[str, Any] = {}

    def handler(**_: object):
        # Middleware rewrites every public call to its grouped command before dispatch, so a
        # call that reaches this body skipped authorization's view of the real command: fail closed.
        raise RuntimeError("a public tool reached its handler without translation")

    def publish(name: str, title: str, description: str, schema: dict[str, Any], annotations, meta=None):
        function = replace_name(handler, name, description)
        tool = FunctionTool.from_function(function, name=name, description=description, annotations=annotations,
                                          meta=dict(meta) if meta else None, output_schema=None)
        tool.parameters = schema
        tools[name] = tool

    for name, wire in surface.tools.items():
        if name in listed:
            publish(name, wire.title, wire.description, tool_schema(wire),
                    ToolAnnotations(title=wire.title, readOnlyHint=wire.effect == "read",
                                    destructiveHint=wire.destructive, idempotentHint=wire.idempotent or wire.effect == "read",
                                    openWorldHint=False), wire.meta)
    for effect, door in DOORS.items():
        if door not in listed:
            continue
        operations = surface.door_operations(door, commands)
        title, description = _DOOR_TEXT[effect]
        destructive = any(
            v.destructive for op in operations for r in surface._renderings[op.command] if r.name == op.rendering
            for v in r.variants if v.kind == op.kind)
        publish(door, title, description, door_schema(door, operations),
                ToolAnnotations(title=title, readOnlyHint=effect == "read",
                                destructiveHint=destructive, idempotentHint=effect == "read", openWorldHint=False))
    return tools


def replace_name(handler, name: str, description: str):
    def call(**kwargs: object):
        return handler(**kwargs)

    call.__name__ = call.__qualname__ = name
    call.__doc__ = description
    call.__signature__ = inspect.Signature([inspect.Parameter(
        "arguments", inspect.Parameter.KEYWORD_ONLY, annotation=dict[str, Any])])  # type: ignore[attr-defined]
    call.__annotations__ = {"arguments": dict[str, Any]}
    return call


# --- Hooks ----------------------------------------------------------------------------------------


def write_selectors() -> tuple[str, ...]:
    """Every `tool:operation` that lands a durable write, for a hook that cannot import this module.

    A typed write tool is stamped by its name alone; a door write by its operation. A call with
    `preview: true` lands nothing, and the hook reads that field itself.
    """
    surface = public_surface()
    selectors = {name for name, tool in surface.tools.items() if tool.effect == "write" and tool.captures}
    for op in surface.doors[DOORS["write"]].values():
        operations = operation_entries.register_operations(op.command)
        if operations is not None and operations.capture_write:
            selectors.add(f"{op.door}:{op.name}")
    return tuple(sorted(selectors))


@functools.cache
def public_surface() -> PublicSurface:
    operations = {command: operation_entries.OPERATIONS.get(command) or operation_entries.register_operations(command)
                  for command in operation_entries.converted_commands()}
    return PublicSurface({command: ops for command, ops in operations.items() if ops is not None})
