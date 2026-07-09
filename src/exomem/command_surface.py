"""Command-surface metadata shared by MCP, REST, and CLI adapters."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass

from mcp.types import ToolAnnotations
from pydantic import Field

# Text-write ops -> the argument field(s) whose value must not be a base64 binary
# blob. The model pays for those characters as output tokens before the request
# arrives, so they are rejected at every write boundary (MCP middleware + REST
# coercion) and the caller is pointed at /upload.
GUARDED_WRITE_FIELDS: dict[str, tuple[str, ...]] = {
    "add": ("content",),
    "note": ("content",),
    "edit": ("new_body", "new_string"),
    "replace": ("content",),
    "create_file": ("content",),
    "append_to_file": ("content",),
    "preserve": ("content",),
    "remember": ("content",),
    "capture_source": ("content",),
    "preserve_evidence": ("content",),
    "edit_memory": ("new_body", "new_string"),
    "replace_memory": ("content",),
    "manage_memory_file": ("content",),
}


# Write ops whose mutation OVERWRITES or REMOVES existing vault content, as opposed
# to purely additive writes (add / note / create_file / append_to_file / link /
# preserve / recover_from_trash / reconcile). Drives the MCP `destructiveHint` so a
# cautious client does not badge an append as destructive.
DESTRUCTIVE_OPS: frozenset[str] = frozenset(
    {
        "edit",
        "replace",
        "delete",
        "move_file",
        "audit_fix",
        "edit_memory",
        "replace_memory",
        "manage_memory_file",
        "maintain_memory",
    }
)


def mcp_tool_annotations(name: str, *, read_only: bool) -> ToolAnnotations:
    """MCP behaviour hints for one tool — what cautious clients render as badges."""
    return ToolAnnotations(
        title=name,
        readOnlyHint=read_only,
        destructiveHint=False if read_only else (name in DESTRUCTIVE_OPS),
        openWorldHint=False,
    )


@dataclass(frozen=True)
class Param:
    """One operation parameter, surface-agnostic."""

    name: str
    type: str = "str"
    required: bool = False
    help: str = ""
    cli_positional: bool = False


@dataclass(frozen=True)
class Command:
    name: str
    leaf: Callable
    params: tuple[Param, ...]
    surfaces: frozenset
    tier: int = 1
    cli_writes: bool = False
    needs_schema: bool = False
    description: str = ""
    product_surface: str = "advanced"
    product_actions: tuple[str, ...] = ()
    first_run_safe: bool = False
    routes: tuple[str, ...] = ()

    @property
    def doc(self) -> str:
        """The full description Claude reads — the leaf's own docstring."""
        return self.description or (self.leaf.__doc__ or "")

    @property
    def guarded_fields(self) -> tuple[str, ...]:
        """Text fields whose value must not be a base64 binary blob."""
        return GUARDED_WRITE_FIELDS.get(self.name, ())

    @property
    def read_only(self) -> bool:
        """True for non-mutating ops (search / get / list)."""
        return not self.cli_writes

    @property
    def mcp_annotations(self) -> ToolAnnotations:
        """MCP behaviour hints for this command's generated tool."""
        return mcp_tool_annotations(self.name, read_only=self.read_only)


def bind_vault(
    leaf: Callable,
    *injected: object,
    name: str | None = None,
    description: str | None = None,
) -> Callable:
    """Return a callable FastMCP introspects exactly like a hand-written wrapper."""
    sig = inspect.signature(leaf)
    params = list(sig.parameters.values())
    visible = params[len(injected):]

    try:
        resolved = typing.get_type_hints(leaf)
    except Exception:  # noqa: BLE001 - fall back to inspect's annotations
        resolved = {}

    help_text = parse_args_help(description if description is not None else leaf.__doc__)
    visible = [
        p.replace(
            annotation=_annotate_description(
                resolved.get(p.name, p.annotation), help_text.get(p.name, "")
            )
        )
        for p in visible
    ]
    new_sig = sig.replace(parameters=visible)

    def wrapper(**kwargs):
        return leaf(*injected, **kwargs)

    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    wrapper.__name__ = name or leaf.__name__
    wrapper.__qualname__ = wrapper.__name__
    wrapper.__doc__ = description if description is not None else leaf.__doc__
    ann = {
        p.name: p.annotation
        for p in visible
        if p.annotation is not inspect.Parameter.empty
    }
    if "return" in resolved:
        ann["return"] = resolved["return"]
    wrapper.__annotations__ = ann
    return wrapper


def _annotate_description(annotation: object, description: str) -> object:
    if (
        not description
        or annotation is inspect.Parameter.empty
        or typing.get_origin(annotation) is typing.Annotated
    ):
        return annotation
    return typing.Annotated[annotation, Field(description=description)]


def parse_args_help(doc: str | None) -> dict[str, str]:
    """Best-effort `{param: one-line help}` from a Google-style `Args:` block."""
    if not doc:
        return {}
    lines = inspect.cleandoc(doc).splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Args:")
    except StopIteration:
        return {}
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        stripped = ln.strip()
        head, sep, rest = stripped.partition(":")
        if sep and head and head.replace("_", "").isalnum() and " " not in head:
            if cur is not None:
                out[cur] = " ".join(buf).strip()
            cur, buf = head, [rest.strip()]
        elif cur is not None:
            buf.append(stripped)
    if cur is not None:
        out[cur] = " ".join(buf).strip()
    return out


def type_tag(annotation: object) -> str:
    """Map a resolved type annotation to a REST/CLI coercion tag."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return type_tag(non_none[0])
        return "json"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is str:
        return "str"
    if annotation is dict or origin is dict:
        return "dict"
    if annotation is list or origin is list:
        args = typing.get_args(annotation)
        return "list[str]" if args in ((), (str,)) else "json"
    return "json"


def derive_params(
    leaf: Callable, *, skip: int, positional: str | None = None
) -> tuple[Param, ...]:
    """Derive the declarative `Param` tuple from a leaf signature + docstring."""
    sig = inspect.signature(leaf)
    try:
        hints = typing.get_type_hints(leaf)
    except Exception:  # noqa: BLE001
        hints = {}
    helps = parse_args_help(leaf.__doc__)
    params: list[Param] = []
    for p in list(sig.parameters.values())[skip:]:
        ann = hints.get(p.name, p.annotation)
        params.append(
            Param(
                name=p.name,
                type=type_tag(ann),
                required=p.default is inspect.Parameter.empty,
                help=helps.get(p.name, ""),
                cli_positional=(p.name == positional),
            )
        )
    return tuple(params)
