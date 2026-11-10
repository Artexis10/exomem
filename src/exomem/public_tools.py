"""The typed agent tools: one wire model and one translation per tool.

An agent calls a typed tool with flat, typed fields. `translate` turns the validated call into
one grouped command and the leaf fields that a direct call carries, so authorization, the writer
lease, egress and the ledger see the grouped command. `spell` is the inverse: it writes a
grouped-command call the way this surface takes it, for a refusal's retry and for guidance.

The wire model is the one source of both the published schema and the strict validation. A
field that passes a leaf argument through unchanged keeps the leaf's name. A rare leaf knob sits
in `options` or `fields`, and its name is checked against the leaf's own signature, so a knob
added to a leaf needs no change here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .query_compat import public_filter_annotation


class Wire(BaseModel):
    """Base of every wire model: unknown fields and loose types are refused, as the server does."""

    model_config = ConfigDict(extra="forbid", strict=True)


Detail = Literal["compact", "full"]
Filters = public_filter_annotation()
Translated = tuple[str, dict[str, Any]]


class Refusal(Exception):
    """A wire call that no grouped command can take, with the values that would work."""

    def __init__(self, code: str, message: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.candidates = candidates


class Context:
    """What a translation may ask about the grouped commands, and nothing more."""

    def __init__(self, params: Callable[[str], frozenset[str]]) -> None:
        self.params = params


@dataclass(frozen=True, slots=True)
class Route:
    """One grouped call that a typed tool stands for, and how its leaf fields spell on the wire."""

    command: str
    #: Leaf values that select this route among the tool's routes.
    when: Mapping[str, object] = field(default_factory=dict)
    #: Leaf field to wire field, for the fields that differ.
    inverse: Mapping[str, str] = field(default_factory=dict)
    #: Wire fields the route implies.
    add: Mapping[str, object] = field(default_factory=dict)
    #: The wire bag that holds the leaf fields that no wire field names.
    bag: str | None = None
    #: Leaf fields that a wire call never carries because the server derives them.
    derived: frozenset[str] = frozenset()
    #: Spells a call this route cannot invert field by field; None means the call is not this route's.
    reverse: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None = None


@dataclass(frozen=True, slots=True)
class WireTool:
    name: str
    title: str
    description: str
    model: type[Wire]
    effect: Literal["read", "write"]
    translate: Callable[[dict[str, Any], Context], Translated]
    routes: tuple[Route, ...]
    destructive: bool = False
    idempotent: bool = False
    #: Top-level `_meta` of the published tool.
    meta: Mapping[str, object] = field(default_factory=dict)
    #: One call that read_instructions shows for this tool.
    example: Mapping[str, Any] = field(default_factory=dict)
    #: The grouped (command, kind) pairs of the entries this tool carries, so no door repeats them.
    claims: tuple[tuple[str, str], ...] = ()
    #: A call of this write tool can land the durable knowledge that a capture hook looks for.
    captures: bool = False

    def spell(self, command: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """The wire arguments of a grouped call this tool stands for, or None when it is not one."""
        for route in self.routes:
            if route.command != command or any(arguments.get(k) != v for k, v in route.when.items()):
                continue
            if route.reverse is not None:
                wire = route.reverse(arguments)
                if wire is not None:
                    return wire
                continue
            wire = dict(route.add)
            bag: dict[str, Any] = {}
            decides = "unlinked_reason" in self.model.model_fields
            if decides and arguments.get("relation_disposition") == "reviewed_none":
                wire["unlinked_reason"] = arguments.get("relation_review_reason")
            for key, value in arguments.items():
                if value is None or key in route.when or key in route.derived or (decides and key in _RELATION_DECISION):
                    continue
                if key in route.inverse:
                    wire[route.inverse[key]] = value
                elif key in self.model.model_fields and key != route.bag:
                    wire[key] = value
                elif route.bag is not None:
                    bag[key] = value
                else:
                    # A call that this tool would send without the field is another operation's call.
                    break
            else:
                if bag:
                    wire[route.bag] = bag  # type: ignore[index]
                return wire
            continue
        return None


#: The leaf fields of one reviewed-none decision. `unlinked_reason` carries it; the server derives the hash.
_RELATION_DECISION = frozenset({"relation_disposition", "relation_review_reason", "relation_review_hash"})


def _given(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _with_detail(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The wire fields as leaf fields: `detail` is the leaf's `response_detail`."""
    return _given({**{key: value for key, value in wire.items() if key != "detail"},
                   "response_detail": wire.get("detail")})


def _bag(
    wire: dict[str, Any], name: str, ctx: Context, command: str, handled: frozenset[str], tool: str
) -> dict[str, Any]:
    """The leaf fields held in the wire bag `name`, each checked against the leaf's signature."""
    bag = wire.pop(name, None) or {}
    allowed = ctx.params(command) - handled
    unknown = sorted(set(bag) - allowed)
    if unknown:
        raise Refusal(
            "INVALID_ARGUMENT",
            f"{tool} `{name}` has no field {', '.join(unknown)}.",
            tuple(dict.fromkeys(
                match for key in unknown for match in get_close_matches(key, sorted(allowed), n=3)
            )) or tuple(sorted(allowed))[:5],
        )
    return bag


def _leaf_call(
    wire: dict[str, Any], name: str, ctx: Context, command: str, tool: str, leaf: Mapping[str, Any]
) -> dict[str, Any]:
    """The leaf call of one translation: the wire bag `name` under the fields `leaf` sets.

    `leaf` lists every field the translation owns, each None when this call leaves it out. The bag
    may repeat none of them, so the set of owned fields has this one source.
    """
    return {**_bag(wire, name, ctx, command, frozenset(leaf), tool), **_given(leaf)}


def _unlinked(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewed-none decision of one `unlinked_reason`; the server binds it to the committed bytes."""
    reason = wire.get("unlinked_reason")
    return {"relation_disposition": None if reason is None else "reviewed_none",
            "relation_review_reason": reason, "relation_review_hash": None}


def _links(links: list[dict[str, Any]] | None) -> list[dict[str, str]] | None:
    return None if links is None else [
        {"kind": link["relation"], "target": link["page"]} for link in links
    ]


# --- Shared field types ---------------------------------------------------------------------

#: The id, title or path of one page, as the page tools take it.
Page = Annotated[str, Field(description="Page title or id.")]


class Link(Wire):
    relation: str
    page: str


class FileHandle(Wire):
    download_url: str
    file_id: str = Field(min_length=1, max_length=256)
    mime_type: str | None = Field(default=None, max_length=255)
    file_name: str | None = None


_UNLINKED = Field(default=None, description="Why this links to no other page.")
_PREVIEW = Field(default=None)
_DETAIL = Field(default=None)


# --- Reads ----------------------------------------------------------------------------------


class SearchMemory(Wire):
    query: str | None = Field(default=None, description="What to look for. Empty lists recent pages.")
    types: list[str] | None = None
    projects: list[str] | None = None
    tags: list[str] | None = None
    filters: dict[str, Any] | None = Field(
        default=None, description='Metadata filter, such as {"page.status": "active"}.'
    )
    options: dict[str, Any] | None = Field(
        default=None, description="Rare knobs: mode, rerank, scope, relations, categories, kinds."
    )
    deep: bool | None = Field(default=None, description="Return a packed context for reasoning.")
    limit: int | None = None
    more: str | None = Field(default=None, description="Continuation token.")
    detail: Detail | None = _DETAIL


def _search(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = {
        "query": wire.get("query"), "types": wire.get("types"), "tags": wire.get("tags"),
        "filters": wire.get("filters"), "deep": wire.get("deep"), "limit": wire.get("limit"),
        "continuation": wire.get("more"), "detail": wire.get("detail"), "projects": wire.get("projects"),
    }
    return "ask_memory", _leaf_call(wire, "options", ctx, "ask_memory", "search_memory", leaf)


class ReadPage(Wire):
    page: Page
    part: Literal["text", "history", "units"] | None = Field(default=None, description="text (default), history, or units: observations with their ids.")
    more: str | None = Field(default=None, description="Continuation token.")
    options: dict[str, Any] | None = Field(
        default=None, description="Rare knobs: links, frontmatter_only, include_raw, unit_ref, offset."
    )


def _read_page_leaf(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The leaf fields of read_page, each None when the wire call leaves it out."""
    return {
        "path": wire.get("page"), "continuation": wire.get("more"),
        "include_history": True if wire.get("part") == "history" else None,
        "units": True if wire.get("part") == "units" else None,
    }


def _spell_read(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The wire call of a read_memory call: the history and unit switches fold into `part`."""
    handled = set(_read_page_leaf({}))
    wire = _given({"page": arguments.get("path"), "more": arguments.get("continuation")})
    if arguments.get("include_history"):
        wire["part"] = "history"
    elif arguments.get("units"):
        wire["part"] = "units"
    rest = {key: value for key, value in arguments.items() if key not in handled and value is not None}
    return {**wire, **({"options": rest} if rest else {})}


def _read_page(wire: dict[str, Any], ctx: Context) -> Translated:
    return "read_memory", _leaf_call(wire, "options", ctx, "read_memory", "read_page", _read_page_leaf(wire))


class ReadInstructions(Wire):
    topic: str | None = Field(
        default=None,
        description="A tool, a door or a door operation such as schema.save.",
    )
    profile: str | None = Field(default=None, description="compact (default), full, diagnostics or session.")
    section: str | None = Field(default=None, description="One block of the compact policy; `index` lists them.")
    skill_contract: str | None = Field(default=None, description="Session digest of the installed skill.")


def _read_instructions(wire: dict[str, Any], ctx: Context) -> Translated:
    return "bootstrap", _given(wire)


# --- Notes ----------------------------------------------------------------------------------


class VocabularyDecision(Wire):
    """The answer to a vocabulary_preparation."""

    evidence_fingerprint: str
    outcome: Literal["reuse", "create", "defer"]
    canonical: str | None = Field(default=None, description="The value to use; none for defer.")


def _vocabulary(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The leaf `vocabulary_decision`, which names `canonical` even when it is none."""
    decision = wire.get("vocabulary_decision")
    return {"vocabulary_decision": None if decision is None else {"canonical": None, **decision}}


class CreateNote(Wire):
    title: str
    content: str = Field(description="Markdown body with a `## Observations` section of `- [category] text` lines. Link a page with [[Title]].")
    type: str | None = None
    project: str | None = Field(default=None, description="The research note's project key; a new slug registers itself.")
    projects: list[str] | None = Field(default=None, description="Project keys of another note; a new slug registers itself.")
    sources: list[str] | None = Field(default=None, description="Titles of the pages this draws from.")
    tags: list[str] | None = None
    fields: dict[str, Any] | None = Field(default=None, description="Type-specific metadata, such as severity or hypothesis.")
    unlinked_reason: str | None = _UNLINKED
    vocabulary_decision: VocabularyDecision | None = None
    suggest_links: bool | None = Field(default=None, description="Also return pages to link.")
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


#: Leaf fields a public note never sets: the server names the page from the title and holds no draft
#: between calls. Listing them keeps `fields` from sending one.
_NO_DRAFT_OR_SLUG = dict.fromkeys(("draft_id", "draft_hash", "draft_token", "slug"))


def _create_note(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = {
        "title": wire["title"], "content": wire["content"], "note_type": wire.get("type"),
        "project": wire.get("project"), "projects": wire.get("projects"), "sources": wire.get("sources"),
        "tags": wire.get("tags"), "suggestions": wire.get("suggest_links"), "validate_only": wire.get("preview"),
        "response_detail": wire.get("detail"), **_unlinked(wire), **_vocabulary(wire), **_NO_DRAFT_OR_SLUG,
    }
    return "remember", _leaf_call(wire, "fields", ctx, "remember", "create_note", leaf)


class AddObservation(Wire):
    note: Page
    category: str = Field(description="Observation category, such as decision or finding.")
    text: str = Field(description="The observation, on one line.")
    tags: list[str] | None = None
    context: str | None = None
    links: list[Link] | None = None
    kind: str | None = Field(default=None, description="A rich kind, such as hypothesis; omit for a compact observation.")
    verdict: str | None = Field(default=None, description="For a rich kind: confirmed, refuted, qualified, inconclusive or abandoned.")
    anchor: str | None = Field(default=None, description="Unique within the note; derived when omitted.")
    as_of: str | None = Field(default=None, description="ISO date this starts to hold.")
    until: str | None = Field(default=None, description="ISO date this stops holding.")
    check_by: str | None = Field(default=None, description="ISO date to revisit.")
    unlinked_reason: str | None = _UNLINKED
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


def _add_observation(wire: dict[str, Any], ctx: Context) -> Translated:
    links = wire.get("links")
    leaf = _given({
        "operation": "validate" if wire.get("preview") else "add",
        "path": wire["note"], "category": wire["category"], "content": wire["text"],
        "tags": wire.get("tags"), "context": wire.get("context"),
        "relations": _links([link for link in links]) if links else None,
        "kind": wire.get("kind"), "verdict": wire.get("verdict"), "id": wire.get("anchor"),
        "as_of": wire.get("as_of"), "until": wire.get("until"), "check_by": wire.get("check_by"),
        "response_detail": wire.get("detail"), **_unlinked(wire),
    })
    return "observe_memory", leaf


class TextEdit(Wire):
    old_text: str
    new_text: str
    replace_all: bool | None = None


def _decoded_object(item: Any) -> Any:
    """Decode an object that a connector sent as a JSON string, so the model checks the object itself."""
    if isinstance(item, str):
        try:
            decoded = json.loads(item)
        except json.JSONDecodeError:
            return item
        return decoded if isinstance(decoded, dict) else item
    return item


EncodedTextEdit = Annotated[TextEdit, BeforeValidator(_decoded_object, json_schema_input_type=TextEdit)]


#: The fields each action takes beyond the shared ones, so a field for another action is refused.
_EDIT_ACTION_FIELDS: dict[str, frozenset[str]] = {  # nosemgrep: ep-word-dict -- the closed wire fields of edit_note's own actions.
    "replace_text": frozenset({"old_text", "new_text", "replace_all", "edits"}),
    "replace_section": frozenset({"heading", "new_text", "position", "create_missing"}),
    "replace_body": frozenset({"body", "tags"}),
    "set_tags": frozenset({"tags"}),
    "set_fields": frozenset({"fields"}),
    "fill_row": frozenset({"row", "take", "overwrite"}),
    "edit_observation": frozenset({"observation", "category", "text", "tags", "context", "links"}),
    "remove_observation": frozenset({"observation"}),
}
_EDIT_SHARED = frozenset({"note", "why", "action", "unlinked_reason", "preview", "detail"})
_EDIT_REQUIRED: dict[str, tuple[str, ...]] = {  # nosemgrep: ep-word-dict -- the closed wire fields of edit_note's own actions.
    "replace_text": (), "replace_section": ("heading", "new_text"), "replace_body": ("body",),
    "set_tags": ("tags",), "set_fields": ("fields",), "fill_row": ("row", "take"),
    "edit_observation": ("observation",), "remove_observation": ("observation",),
}


class EditNote(Wire):
    note: Page = Field(description="Page title, or the page id for replace_body.")
    why: str
    action: Literal[*_EDIT_ACTION_FIELDS]
    old_text: str | None = Field(default=None, description="replace_text.")
    new_text: str | None = Field(default=None, description="replace_text, replace_section.")
    replace_all: bool | None = Field(default=None, description="replace_text: every match.")
    edits: list[EncodedTextEdit] | None = Field(default=None, description="replace_text: several in one commit.")
    heading: str | None = Field(default=None, description="replace_section.")
    position: Literal["append", "prepend", "replace"] | None = Field(default=None, description="replace_section; append by default.")
    create_missing: bool | None = Field(default=None, description="replace_section: add an absent heading.")
    body: str | None = Field(default=None, description="replace_body.")
    tags: list[str] | None = Field(default=None, description="set_tags, edit_observation: the full list.")
    fields: dict[str, Any] | None = Field(default=None, description="set_fields.")
    row: str | None = Field(default=None, description="fill_row: the row's leading text.")
    take: str | None = Field(default=None, description="fill_row.")
    overwrite: bool | None = Field(default=None, description="fill_row: replace a filled take.")
    observation: str | None = Field(default=None, description="edit_observation, remove_observation: the unit id.")
    category: str | None = Field(default=None, description="edit_observation.")
    text: str | None = Field(default=None, description="edit_observation.")
    context: str | None = Field(default=None, description="edit_observation.")
    links: list[Link] | None = Field(default=None, description="edit_observation: the full list.")
    unlinked_reason: str | None = _UNLINKED
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


def _edit_note(wire: dict[str, Any], ctx: Context) -> Translated:
    action = wire["action"]
    sent = {key for key, value in wire.items() if value is not None} - _EDIT_SHARED
    stray = sorted(sent - _EDIT_ACTION_FIELDS[action])
    if stray:
        raise Refusal("INVALID_ARGUMENT", f"{', '.join(stray)} does not apply to {action}.")
    missing = [key for key in _EDIT_REQUIRED[action] if wire.get(key) is None]
    if missing:
        raise Refusal("MISSING_ARGUMENT", f"{action} needs {', '.join(missing)}.")
    detail = _given({"response_detail": wire.get("detail")})
    # A page id names the version the agent read; the leaf compares it after the write is admitted.
    guard = {"expected_hash": wire["note"]} if _is_id(wire["note"]) else {}
    if action in {"edit_observation", "remove_observation"}:
        if action == "remove_observation" and wire.get("preview"):
            raise Refusal("INVALID_ARGUMENT", "remove_observation has no preview.")
        # The leaf previews an edit of one unit as `validate` with that unit.
        operation = "remove" if action == "remove_observation" else "validate" if wire.get("preview") else "update"
        leaf = {
            "operation": operation,
            "path": wire["note"], "unit": wire["observation"], "reason": wire["why"],
            **_given({
                "category": wire.get("category"), "content": wire.get("text"), "tags": wire.get("tags"),
                "context": wire.get("context"), "relations": _links(wire.get("links")),
            }),
            **_given(_unlinked(wire)), **guard, **detail,
        }
        return "observe_memory", leaf
    operation: dict[str, Any]
    if action == "replace_text":
        if wire.get("edits") is not None:
            if wire.get("old_text") is not None or wire.get("new_text") is not None:
                raise Refusal("INVALID_ARGUMENT", "replace_text takes old_text and new_text, or edits, not both.")
            operation = {"kind": "batch_replace", "edits": [
                _given({"old_string": e["old_text"], "new_string": e["new_text"], "replace_all": e.get("replace_all")})
                for e in wire["edits"]
            ]}
        elif wire.get("old_text") is None or wire.get("new_text") is None:
            raise Refusal("MISSING_ARGUMENT", "replace_text needs old_text and new_text, or edits.")
        else:
            operation = {"kind": "replace_string", "old_string": wire["old_text"],
                         **_given({"new_string": wire["new_text"], "replace_all": wire.get("replace_all")})}
    elif action == "replace_section":
        operation = {"kind": "edit_section", "heading": wire["heading"], "new_string": wire["new_text"],
                     **_given({"section_position": wire.get("position"), "create_missing": wire.get("create_missing")})}
    elif action == "replace_body":
        if not guard:
            raise Refusal("MISSING_ARGUMENT", "replace_body needs the page id that read_page returned in `note`.")
        operation = {"kind": "replace_body", "new_body": wire["body"], **_given({"tags": wire.get("tags")})}
    elif action == "set_tags":
        operation = {"kind": "replace_tags", "tags": wire["tags"]}
    elif action == "set_fields":
        if len(wire["fields"]) != 1:
            raise Refusal("INVALID_ARGUMENT", "set_fields changes one field per call.")
        ((name, value),) = wire["fields"].items()
        operation = {"kind": "patch_frontmatter", "field": name, "value": value}
    else:
        operation = {"kind": "fill_row", "row_key": wire["row"], "take": wire["take"],
                     **_given({"overwrite": wire.get("overwrite")})}
    operation.update(guard)
    operation.update(_given(_unlinked(wire)))
    if wire.get("preview"):
        operation["validate_only"] = True
    return "edit_memory", {"path": wire["note"], "why": wire["why"], "operation": operation, **detail}


#: edit_memory's own operation kinds mapped to the edit_note action that sends each one.
_EDIT_ACTIONS: dict[str, str] = {  # nosemgrep: ep-word-dict -- the closed operation kinds of edit_memory mapped to edit_note's own actions.
    "replace_string": "replace_text", "batch_replace": "replace_text", "edit_section": "replace_section",
    "replace_body": "replace_body", "replace_tags": "set_tags", "patch_frontmatter": "set_fields",
    "fill_row": "fill_row",
}


def _spell_edit(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The edit_note call of an edit_memory call, or None for a call that carries no nested operation."""
    operation = arguments.get("operation")
    if not isinstance(operation, Mapping) or operation.get("kind") not in _EDIT_ACTIONS:
        return None
    kind = operation["kind"]
    wire: dict[str, Any] = _given({
        "note": arguments.get("path"), "why": arguments.get("why"), "action": _EDIT_ACTIONS[kind],
        "detail": arguments.get("response_detail"),
    })
    if operation.get("validate_only"):
        wire["preview"] = True
    if operation.get("relation_disposition") == "reviewed_none":
        wire["unlinked_reason"] = operation.get("relation_review_reason")
    if kind == "replace_string":
        wire.update(_given({"old_text": operation.get("old_string"), "new_text": operation.get("new_string"),
                            "replace_all": operation.get("replace_all")}))
    elif kind == "batch_replace":
        wire["edits"] = [
            _given({"old_text": e.get("old_string"), "new_text": e.get("new_string"), "replace_all": e.get("replace_all")})
            for e in operation.get("edits") or ()
        ]
    elif kind == "edit_section":
        wire.update(_given({"heading": operation.get("heading"), "new_text": operation.get("new_string"),
                            "position": operation.get("section_position"), "create_missing": operation.get("create_missing")}))
    elif kind == "replace_body":
        wire.update(_given({"body": operation.get("new_body"), "tags": operation.get("tags")}))
    elif kind == "replace_tags":
        wire["tags"] = operation.get("tags")
    elif kind == "patch_frontmatter":
        wire["fields"] = {operation.get("field"): operation.get("value")}
    else:
        wire.update(_given({"row": operation.get("row_key"), "take": operation.get("take"),
                            "overwrite": operation.get("overwrite")}))
    return wire


def _is_id(value: object) -> bool:
    from .page_ids import is_page_id

    return is_page_id(value)


class SupersedeNote(Wire):
    note: Page = Field(description="The note to replace.")
    title: str
    content: str
    why: str
    type: str | None = None
    project: str | None = None
    projects: list[str] | None = None
    sources: list[str] | None = None
    tags: list[str] | None = None
    fields: dict[str, Any] | None = None
    unlinked_reason: str | None = _UNLINKED
    vocabulary_decision: VocabularyDecision | None = None
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


def _supersede_note(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = {
        "old_path": wire["note"], "title": wire["title"], "content": wire["content"],
        "reason": wire["why"], "note_type": wire.get("type"), "project": wire.get("project"),
        "projects": wire.get("projects"), "sources": wire.get("sources"), "tags": wire.get("tags"),
        "validate_only": wire.get("preview"), "response_detail": wire.get("detail"),
        **_unlinked(wire), **_vocabulary(wire), **_NO_DRAFT_OR_SLUG,
    }
    return "replace_memory", _leaf_call(wire, "fields", ctx, "replace_memory", "supersede_note", leaf)


# --- Sources, evidence, episodes and transfers -----------------------------------------------


class SaveSource(Wire):
    title: str
    source_kind: str | None = Field(default=None, description="What the material is, as an open slug such as contract.")
    content: str | None = Field(default=None, description="The original text; attached files go in `files`.")
    url: str | None = None
    files: list[FileHandle] | None = Field(default=None, description="Attached files, up to eight.")
    domain: str | None = None
    projects: list[str] | None = None
    tags: list[str] | None = None
    why: str | None = None
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: raw_protection, source_identity, expected_sha256, correction.")
    detail: Detail | None = _DETAIL


def _save_source(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = {
        "title": wire["title"], "content": wire.get("content"), "url": wire.get("url"),
        "tags": wire.get("tags"), "why_captured": wire.get("why"), "source_kind": wire.get("source_kind"),
        "domain": wire.get("domain"), "files": wire.get("files"), "response_detail": wire.get("detail"),
        "projects": wire.get("projects"),
    }
    return "capture_source", _leaf_call(wire, "options", ctx, "capture_source", "save_source", leaf)


class SaveEvidence(Wire):
    scope: str = Field(description="Case or project key, one path segment.")
    category: str = Field(description="One path segment.")
    name: str | None = Field(default=None, description="File name with extension, for `text`.")
    text: str | None = Field(default=None, description="Exact UTF-8 text to keep.")
    files: list[FileHandle] | None = Field(default=None, description="Attached files, up to eight.")
    description: str | None = None
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: raw_protection, transcriptions, archive.")
    detail: Detail | None = _DETAIL


def _save_evidence(wire: dict[str, Any], ctx: Context) -> Translated:
    text, files = wire.get("text") is not None, wire.get("files") is not None
    if text == files or (text and wire.get("name") is None):
        raise Refusal("INVALID_ARGUMENT", "save_evidence takes `text` with `name`, or `files`, and not both.")
    if files:
        if wire.get("description") is not None:
            raise Refusal("INVALID_ARGUMENT", "description applies to `text` evidence only.")
        leaf = {"scope": wire["scope"], "category": wire["category"], "files": wire["files"],
                "response_detail": wire.get("detail")}
        return "preserve_artifacts", _leaf_call(wire, "options", ctx, "preserve_artifacts", "save_evidence", leaf)
    leaf = {"scope": wire["scope"], "category": wire["category"], "filename": wire["name"],
            "content": wire["text"], "description": wire.get("description"), "response_detail": wire.get("detail")}
    return "preserve_evidence", _leaf_call(wire, "options", ctx, "preserve_evidence", "save_evidence", leaf)


class SaveEpisode(Wire):
    summary: str = Field(description="One line, at most 180 characters.")
    episode: str | None = Field(default=None, description="The `ep-` key a previous save returned; omit on the first save.")
    subject: str | None = None
    worked_on: list[str] | None = None
    decided: list[str] | None = None
    open: list[str] | None = Field(default=None, description="What was left open.")
    said: list[str] | None = Field(default=None, description="Up to three verbatim user statements.")
    about: list[str] | None = Field(default=None, description="Pages this concerns, by title.")
    client: str | None = None
    detail: Detail | None = _DETAIL


def _save_episode(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = _with_detail(wire)
    return "episode_memory", {"action": "record", **leaf}


class UploadFiles(Wire):
    lane: Literal["source", "evidence"] = Field(description="Where the bytes go: source (original material) or evidence (proof).")
    expected_sha256: str | None = Field(default=None, description="SHA-256 of the file you intend to send.")
    hold: bool | None = Field(default=None, description="Keep the upload for this session; needs expected_sha256.")
    detail: Detail | None = _DETAIL


def _upload_files(wire: dict[str, Any], ctx: Context) -> Translated:
    return "transfer_artifact", {"operation": "upload", **_given({
        "lane": wire["lane"], "expected_sha256": wire.get("expected_sha256"), "hold": wire.get("hold"),
        "response_detail": wire.get("detail")})}


#: The most pages one download call takes; the leaf's own bound is this value.
DOWNLOAD_BATCH_MAX = 8


class DownloadFiles(Wire):
    pages: list[str] = Field(min_length=1, max_length=DOWNLOAD_BATCH_MAX, description="Titles or ids of the pages.")
    detail: Detail | None = _DETAIL


def _download_leaf(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The leaf fields of download_files, each None when the wire call leaves it out."""
    return {"operation": "download", "artifacts": wire.get("pages"), "response_detail": wire.get("detail")}


def _download_files(wire: dict[str, Any], ctx: Context) -> Translated:
    return "transfer_artifact", _given(_download_leaf(wire))


def _spell_download(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The pages of a download call: the leaf's one `artifact` is a list of one.

    A call that names no page is a route in guidance, which leaves the pages to the tool schema.
    """
    # The leaf also takes one `artifact`, which this spelling folds into the list.
    if set(arguments) - {*_download_leaf({}), "artifact"}:
        return None
    pages = arguments.get("artifacts") or ([arguments["artifact"]] if arguments.get("artifact") else None)
    return _given({"pages": pages, "detail": arguments.get("response_detail")})


# --- Records and Planning -------------------------------------------------------------------


class QueryRecords(Wire):
    collection: str | None = Field(default=None, description="Omit to list collections.")
    filters: Filters = None
    columns: list[str] | None = Field(default=None, description="Columns to return; every row keeps its `id`.")
    sort_by: str | None = None
    descending: bool | None = None
    limit: int | None = None
    more: str | None = Field(default=None, description="Continuation token.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: aggregate, date_from, date_to, view, output_format.")


def _query_collection(command: str, tool: str, key: str | None):
    """`key` is the column that names a row: it and the row version make the row id the reply carries.

    A call that restricts `columns` gets those two added when `key` is given. A Records collection
    can be a dataset that has no row key, so a Records call gets only the columns it asked for.
    """

    def translate(wire: dict[str, Any], ctx: Context) -> Translated:
        collection = wire.get("collection")
        columns = wire.get("columns")
        if columns is not None and key is not None:
            columns = [*columns, *(name for name in (key, "item_version") if name not in columns)]
        leaf = {
            "action": "inspect" if collection is None else "query", "collection": collection,
            "filters": wire.get("filters"), "columns": columns, "sort_by": wire.get("sort_by"),
            "descending": wire.get("descending"), "limit": wire.get("limit"), "continuation": wire.get("more"),
        }
        call = _leaf_call(wire, "options", ctx, command, tool, leaf)
        # No collection lists them all; the other fields then have nothing to select.
        return command, {"action": "inspect"} if collection is None else call
    return translate


def _spell_collection_list(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The list of collections is the query tool's; the inspection of one collection is a door operation."""
    return None if arguments.get("collection") is not None else {}


class AddRecord(Wire):
    collection: str
    item: dict[str, Any] = Field(description="Fields by column name.")
    why: str
    body: str | None = Field(default=None, description="Free text kept with the row.")
    hold: bool | None = Field(default=None, description="Hold a row that fails validation.")
    held: str | None = Field(default=None, description="The id of a held row to resume.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: delivery, item_key.")
    detail: Detail | None = _DETAIL


def _add_record(wire: dict[str, Any], ctx: Context) -> Translated:
    leaf = {
        "action": "append", "collection": wire["collection"], "item": wire["item"], "why": wire["why"],
        "body": wire.get("body"), "hold": wire.get("hold"), "held": wire.get("held"),
        "response_detail": wire.get("detail"),
    }
    return "record_memory", _leaf_call(wire, "options", ctx, "record_memory", "add_record", leaf)


class EditRecord(Wire):
    collection: str
    record: str = Field(description="The row `id` from query_records, or `row_id` when the collection has its own `id` field.")
    changes: dict[str, Any] = Field(description="Columns to change, by name.")
    why: str
    body: str | None = None
    held: str | None = None
    refresh_presentation: bool | None = Field(
        default=None, description="Rebuild the row's Markdown view; `changes` may then be empty."
    )
    detail: Detail | None = _DETAIL


def _edit_record(wire: dict[str, Any], ctx: Context) -> Translated:
    return "record_memory", {"action": "update", **_given({
        "collection": wire["collection"], "item_id": wire["record"], "changes": wire["changes"],
        "why": wire["why"], "body": wire.get("body"), "held": wire.get("held"),
        "refresh_presentation": wire.get("refresh_presentation"),
        "response_detail": wire.get("detail")})}


class QueryPlanItems(Wire):
    collection: str | None = Field(default=None, description="Omit to list plan collections.")
    filters: Filters = None
    columns: list[str] | None = None
    sort_by: str | None = None
    descending: bool | None = None
    limit: int | None = None
    more: str | None = Field(default=None, description="Continuation token.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: lifecycle, hierarchy_mode, aggregate, view, output_format.")


class AddPlanItem(Wire):
    collection: str
    item: dict[str, Any] = Field(description="Fields by column name.")
    why: str
    body: str | None = None
    detail: Detail | None = _DETAIL


def _add_plan_item(wire: dict[str, Any], ctx: Context) -> Translated:
    return "plan_memory", {"action": "add", **_with_detail(wire)}


class EditPlanItem(Wire):
    collection: str
    plan_item: str = Field(description="The item id that query_plan_items returned.")
    why: str
    changes: dict[str, Any] | None = Field(default=None, description="Columns to change, by name.")
    status: str | None = Field(default=None, description="Move to this status; a refused move names the allowed ones.")
    body: str | None = None
    detail: Detail | None = _DETAIL


def _edit_plan_item(wire: dict[str, Any], ctx: Context) -> Translated:
    if (wire.get("status") is None) == (wire.get("changes") is None and wire.get("body") is None):
        raise Refusal("INVALID_ARGUMENT", "edit_plan_item takes `status`, or `changes` and `body`, not both and not neither.")
    common = _given({"collection": wire["collection"], "item_id": wire["plan_item"], "why": wire["why"],
                     "response_detail": wire.get("detail")})
    if wire.get("status") is not None:
        return "plan_memory", {"action": "triage", **common, "transition": {"status": wire["status"]}}
    return "plan_memory", {"action": "update", **common, **_given({"changes": wire.get("changes"), "body": wire.get("body")})}


#: The leaf fields of one status move; the server derives the rest.
_STATUS_MOVE = frozenset({"action", "collection", "item_id", "why", "response_detail", "transition",
                          "plan_id", "expected_item_version", "expected_container_hash"})


def _spell_status_move(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """A triage call is `edit_plan_item` with `status`, when its transition moves only the status or names none."""
    transition = arguments.get("transition")
    if transition is not None and (not isinstance(transition, Mapping) or set(transition) != {"status"}):
        return None
    if set(arguments) - _STATUS_MOVE:
        return None
    return _given({"collection": arguments.get("collection"), "plan_item": arguments.get("item_id"),
                   "why": arguments.get("why"), "status": None if transition is None else transition["status"],
                   "detail": arguments.get("response_detail")})


# --- Review ---------------------------------------------------------------------------------

#: The queues of list_review_items, which the review reader names by mode. A closed pair of API
#: names that the review reader's own mode registry fixes.
REVIEW_QUEUES: dict[str, str] = {  # nosemgrep: ep-word-dict -- the closed queue names of list_review_items mapped to review_memory's registered modes.
    "due": "attention", "activation": "activation", "stale": "stale", "contradiction": "contradiction",
    "unprocessed_sources": "unprocessed-sources", "unlinked": "relation-debt",
    "relation_suggestions": "relation-queue", "imports": "adoption", "upkeep": "upkeep",
    "plan_progress": "plan-progress", "vocabulary": "vocabulary", "procedure_approvals": "procedure_approvals",
}

class ListReviewItems(Wire):
    queue: Literal[*REVIEW_QUEUES] | None = Field(default=None, description="One queue; due items by default.")
    categories: list[str] | None = Field(default=None, description="Keep only these finding categories, or upkeep families.")
    limit: int | None = None


def _spell_review_queue(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The wire call of one review mode: the mode's queue name, and the limits the wire takes."""
    queue = next((name for name, mode in REVIEW_QUEUES.items() if mode == arguments.get("mode")), None)
    if queue is None or set(arguments) - {"mode", "categories", "limit"}:
        return None  # Another field means another operation: a meaning question names a path and a family.
    return _given({"queue": None if queue == "due" else queue, "categories": arguments.get("categories"),
                   "limit": arguments.get("limit")})


def _list_review_items(wire: dict[str, Any], ctx: Context) -> Translated:
    return "review_memory", {"mode": REVIEW_QUEUES[wire.get("queue") or "due"],
                             **_given({"categories": wire.get("categories"), "limit": wire.get("limit")})}


Decision = Literal["accept", "dismiss", "snooze", "reopen"]

#: The decisions that triage one item; `accept` records a relation and takes the connection route.
_TRIAGE_DECISIONS = tuple(decision for decision in get_args(Decision) if decision != "accept")


class DecideReviewItem(Wire):
    review_item: str = Field(description="A list_review_items id, or an advisory ref.")
    decision: Decision
    until: str | None = Field(default=None, description="snooze: YYYY-MM-DD.")
    why: str | None = None
    relation: str | None = Field(default=None, description="accept: the relation to record, when the suggestion offers several.")
    vocabulary_ref: str | None = Field(default=None, description="accept: the vocabulary decision that names a relation the suggestion lacks.")
    vocabulary_fingerprint: str | None = Field(default=None, description="accept: that decision's fingerprint.")
    detail: Detail | None = _DETAIL


def _spell_decision(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The wire call of a decision on one review item; a family, envelope or vocabulary decision is a door operation."""
    if arguments.get("item_id") is None or arguments.get("action") not in _TRIAGE_DECISIONS:
        return None
    return {"review_item": arguments["item_id"], "decision": arguments["action"],
            **_given({"until": arguments.get("until"), "why": arguments.get("why"), "detail": arguments.get("response_detail")})}


def _decide_review_item(wire: dict[str, Any], ctx: Context) -> Translated:
    detail = _given({"response_detail": wire.get("detail")})
    if wire["decision"] == "accept":
        return "connect_memory", {"operation": "accept-relation", "item_id": wire["review_item"],
                                  **_given({"why": wire.get("why"), "requested_relation": wire.get("relation"),
                                            "vocabulary_ref": wire.get("vocabulary_ref"),
                                            "vocabulary_fingerprint": wire.get("vocabulary_fingerprint")}), **detail}
    if any(wire.get(key) is not None for key in ("relation", "vocabulary_ref", "vocabulary_fingerprint")):
        raise Refusal("INVALID_ARGUMENT", "relation and the vocabulary decision apply to accept only.")
    return "triage_memory", {"action": wire["decision"], "item_id": wire["review_item"],
                             **_given({"until": wire.get("until"), "why": wire.get("why")}), **detail}


# --- The table ------------------------------------------------------------------------------

_FILES_META = {"openai/fileParams": ["files"]}


def _tool(name, title, description, model, effect, translate, routes, **kw) -> WireTool:
    return WireTool(name, title, description, model, effect, translate, tuple(routes), **kw)


_NOTE_INVERSE = {"validate_only": "preview", "response_detail": "detail"}

TOOLS: dict[str, WireTool] = {t.name: t for t in (  # nosemgrep: ep-word-dict -- the closed set of typed tool names the surface lists.
    _tool("search_memory", "Search memory",
          "Search notes, sources and evidence for what recall_context missed. Hits are named by title; open one with read_page. A miss covers this query only.",
          SearchMemory, "read", _search,
          [Route("ask_memory", inverse={"continuation": "more"}, bag="options")], idempotent=True),
    _tool("read_page", "Read page", "Read one page by title or id: text, type, date and links.",
          ReadPage, "read", _read_page,
          [Route("read_memory", reverse=_spell_read)], idempotent=True,
          claims=(("read_memory", "page"),)),
    _tool("read_instructions", "Read instructions",
          "Read the live memory policy, or the arguments and one example of a tool, a door or one door operation.",
          ReadInstructions, "read", _read_instructions, [Route("bootstrap")], idempotent=True,
          claims=(("bootstrap", "instructions"),)),
    _tool("create_note", "Create note",
          "Save a lasting conclusion as a new note. Raw material goes in save_source, proof in save_evidence. Link a page, or give unlinked_reason.",
          CreateNote, "write", _create_note,
          [Route("remember", inverse={"note_type": "type", "suggestions": "suggest_links", **_NOTE_INVERSE},
                 bag="fields", derived=frozenset({"draft_id", "draft_hash", "draft_token"}))],
          example={"title": "Weekly releases beat batch releases", "type": "insight", "content": "Weekly releases cut rollback size.\n\n## Observations\n- [finding] Weekly releases halved rollback size #release\n\n## Relations\n- supports [[Release cadence]]"},
          captures=True),
    _tool("add_observation", "Add observation",
          "Add one observation to an existing note; nothing existing changes.",
          AddObservation, "write", _add_observation,
          [Route("observe_memory", when={"operation": "add"},
                 inverse={"path": "note", "content": "text", "id": "anchor", "relations": "links", **_NOTE_INVERSE}),
           Route("observe_memory", when={"operation": "validate"}, add={"preview": True},
                 inverse={"path": "note", "content": "text", "id": "anchor", "relations": "links", **_NOTE_INVERSE})],
          example={"note": "Table groups and timestamp units", "category": "decision", "text": "Store event times as UTC microseconds", "tags": ["schema"], "links": [{"relation": "refines", "page": "Event schema v3"}]},
          claims=(("observe_memory", "observation"), ("observe_memory", "add")),
          captures=True),
    _tool("edit_note", "Edit note",
          "Change part of an existing note and say why. A changed conclusion goes to supersede_note. `action` picks the change; each action takes its own fields.",
          EditNote, "write", _edit_note,
          [Route("edit_memory", reverse=_spell_edit),
           Route("observe_memory", when={"operation": "update"}, add={"action": "edit_observation"},
                 inverse={"path": "note", "unit": "observation", "unit_ref": "observation", "content": "text", "reason": "why", **_NOTE_INVERSE},
                 derived=frozenset({"expected_hash"})),
           Route("observe_memory", when={"operation": "remove"}, add={"action": "remove_observation"},
                 inverse={"path": "note", "unit": "observation", "unit_ref": "observation", "reason": "why", **_NOTE_INVERSE},
                 derived=frozenset({"expected_hash"}))],
          destructive=True,
          example={"note": "Retry policy", "why": "Cap changed after the outage review", "action": "replace_text", "old_text": "cap at 30 s", "new_text": "cap at 10 s"},
          claims=(("observe_memory", "update"), ("observe_memory", "remove")),
          captures=True),
    _tool("supersede_note", "Supersede note",
          "Replace a note whose conclusion changed. The old note stays readable and points to the new one.",
          SupersedeNote, "write", _supersede_note,
          [Route("replace_memory", inverse={"old_path": "note", "reason": "why", "note_type": "type", **_NOTE_INVERSE},
                 bag="fields", derived=frozenset({"draft_id", "draft_hash", "draft_token"}))],
          destructive=True,
          captures=True),
    _tool("save_source", "Save source",
          "Save original material verbatim. Never retype a file: pass attached files in `files`.",
          SaveSource, "write", _save_source,
          [Route("capture_source", inverse={"why_captured": "why", "response_detail": "detail"}, bag="options")],
          meta=_FILES_META,
          captures=True),
    _tool("save_evidence", "Save evidence",
          "Keep proof as append-only evidence: text with a name, or attached files.",
          SaveEvidence, "write", _save_evidence,
          [Route("preserve_evidence", inverse={"filename": "name", "content": "text", "response_detail": "detail"}),
           Route("preserve_artifacts", inverse={"response_detail": "detail"}, bag="options")],
          meta=_FILES_META,
          captures=True),
    _tool("save_episode", "Save episode",
          "Save a recap of this session: what was worked on, decided and left open. Call once at a stopping point when policy allows.",
          SaveEpisode, "write", _save_episode,
          [Route("episode_memory", when={"action": "record"}, inverse={"response_detail": "detail"})],
          claims=(("episode_memory", "record"),),
          captures=True),
    _tool("upload_files", "Upload files",
          "Get a one-use upload for bytes on this machine. The reply gives the step that moves them; then pass the handle to save_source or save_evidence.",
          UploadFiles, "write", _upload_files,
          [Route("transfer_artifact", when={"operation": "upload"}, inverse={"response_detail": "detail"})]),
    _tool("download_files", "Download files",
          "Get the original files of pages back out of memory. The reply gives a link or a command that saves each.",
          DownloadFiles, "write", _download_files,
          [Route("transfer_artifact", when={"operation": "download"}, reverse=_spell_download)]),
    _tool("query_records", "Query records",
          "Read the person's Records. Without `collection`, list the collections and their fields; with one, filter and sort its rows.",
          QueryRecords, "read", _query_collection("record_memory", "query_records", None),
          [Route("record_memory", when={"action": "query"}, inverse={"continuation": "more"}, bag="options"),
           Route("record_memory", when={"action": "inspect"}, reverse=_spell_collection_list)], idempotent=True,
          example={"collection": "Workouts", "filters": [{"column": "type", "op": "eq", "value": "run"}], "sort_by": "date", "descending": True, "limit": 20},
          claims=(("record_memory", "query"),)),
    _tool("add_record", "Add record",
          "Add a row to a Records collection the person owns. Outcomes go here; intentions go to Planning.",
          AddRecord, "write", _add_record,
          [Route("record_memory", when={"action": "append"}, inverse={"response_detail": "detail"}, bag="options")],
          claims=(("record_memory", "append"),),
          captures=True),
    _tool("edit_record", "Edit record",
          "Change fields of one record.",
          EditRecord, "write", _edit_record,
          [Route("record_memory", when={"action": "update"}, inverse={"item_id": "record", "response_detail": "detail"},
                 derived=frozenset({"item_key", "expected_item_version", "expected_container_hash"}))],
          destructive=True,
          example={"collection": "Workouts", "record": "b3k9q2x.7mna", "changes": {"distance_km": 12.4}, "why": "Watch sync was wrong"},
          claims=(("record_memory", "update"),),
          captures=True),
    _tool("query_plan_items", "Query plan items",
          "Read the person's Planning. Without `collection`, list the plan collections; with one, filter its items.",
          QueryPlanItems, "read", _query_collection("plan_memory", "query_plan_items", "plan_id"),
          [Route("plan_memory", when={"action": "query"}, inverse={"continuation": "more"}, bag="options"),
           Route("plan_memory", when={"action": "inspect"}, reverse=_spell_collection_list)], idempotent=True,
          claims=(("plan_memory", "query"),)),
    _tool("add_plan_item", "Add plan item", "Add an item to a plan the person owns.",
          AddPlanItem, "write", _add_plan_item,
          [Route("plan_memory", when={"action": "add"}, inverse={"response_detail": "detail"})],
          claims=(("plan_memory", "add"),),
          captures=True),
    _tool("edit_plan_item", "Edit plan item",
          "Change one plan item, or move it to a new status with `status`.",
          EditPlanItem, "write", _edit_plan_item,
          [Route("plan_memory", when={"action": "update"}, inverse={"item_id": "plan_item", "response_detail": "detail"},
                 derived=frozenset({"plan_id", "expected_item_version", "expected_container_hash"})),
           Route("plan_memory", when={"action": "triage"}, reverse=_spell_status_move)],
          destructive=True,
          example={"collection": "Launch plan", "plan_item": "p3x9t2k.7mna", "why": "Draft finished", "status": "done"},
          claims=(("plan_memory", "update"), ("plan_memory", "triage")),
          captures=True),
    _tool("list_review_items", "List review items",
          "List what needs the person's review: due items by default, or one queue.",
          ListReviewItems, "read", _list_review_items,
          [Route("review_memory", reverse=_spell_review_queue)], idempotent=True,
          claims=(("review_memory", "attention"), ("review_memory", "activation"), ("review_memory", "stale"), ("review_memory", "contradiction"), ("review_memory", "unprocessed-sources"), ("review_memory", "relation-debt"), ("review_memory", "relation-queue"), ("review_memory", "adoption"), ("review_memory", "upkeep"), ("review_memory", "plan-progress"), ("review_memory", "vocabulary"), ("review_memory", "procedure_approvals"),)),
    _tool("decide_review_item", "Decide review item",
          "Record the person's decision on one review item. accept applies to a relation suggestion.",
          DecideReviewItem, "write", _decide_review_item,
          [Route("triage_memory", reverse=_spell_decision),
           Route("connect_memory", when={"operation": "accept-relation"},
                 inverse={"item_id": "review_item", "requested_relation": "relation", "response_detail": "detail"},
                 add={"decision": "accept"})],
          destructive=False, example={"review_item": "r5t2k8w.2pxa", "decision": "accept", "relation": "refines"},
          claims=(("triage_memory", "decision"), ("connect_memory", "accept-relation")),
          # An accepted relation is a saved write; the capture hook reads no argument, so a dismissal counts with it.
          captures=True),
)}
