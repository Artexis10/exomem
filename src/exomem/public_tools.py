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
from typing import Annotated, Any, Literal

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


def _unlinked(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewed-none decision of one `unlinked_reason`; the server binds it to the committed bytes."""
    reason = wire.get("unlinked_reason")
    if reason is None:
        return {}
    return {"relation_disposition": "reviewed_none", "relation_review_reason": reason}


def _links(links: list[dict[str, Any]] | None) -> list[dict[str, str]] | None:
    return None if links is None else [
        {"kind": link["relation"], "target": link["page"]} for link in links
    ]


# --- Shared field types ---------------------------------------------------------------------

#: The id, title or path of one page, as the page tools take it.
Page = Annotated[str, Field(description="Page title or id.")]
Why = Annotated[str, Field(description="The reason, one sentence.")]


class Link(Wire):
    relation: str = Field(description="Relation name.")
    page: Page


class FileHandle(Wire):
    download_url: str
    file_id: str = Field(min_length=1, max_length=256)
    mime_type: str | None = Field(default=None, max_length=255)
    file_name: str | None = None


_UNLINKED = Field(default=None, description="Why this stands alone, when it links to no other page.")
_PREVIEW = Field(default=None, description="Check the call and write nothing.")
_DETAIL = Field(default=None, description="compact (default) or full.")


# --- Reads ----------------------------------------------------------------------------------


class SearchMemory(Wire):
    query: str | None = Field(default=None, description="What to look for. Empty lists recent pages.")
    types: list[str] | None = Field(default=None, description="Page types.")
    projects: list[str] | None = None
    tags: list[str] | None = None
    filters: dict[str, Any] | None = Field(
        default=None, description='Structured page or unit metadata filter, such as {"page.status": "active"}.'
    )
    options: dict[str, Any] | None = Field(
        default=None, description="Rare knobs: mode, rerank, scope, relations, categories, kinds. read_instructions lists them."
    )
    deep: bool | None = Field(default=None, description="Return a packed context for reasoning.")
    limit: int | None = None
    more: str | None = Field(default=None, description="Token from the previous reply.")
    detail: Detail | None = _DETAIL


_SEARCH_HANDLED = frozenset({"query", "types", "projects", "tags", "filters", "deep", "limit", "continuation", "detail"})


def _search(wire: dict[str, Any], ctx: Context) -> Translated:
    options = _bag(wire, "options", ctx, "ask_memory", _SEARCH_HANDLED, "search_memory")
    leaf = _given({
        "query": wire.get("query"), "types": wire.get("types"), "tags": wire.get("tags"),
        "filters": wire.get("filters"), "deep": wire.get("deep"), "limit": wire.get("limit"),
        "continuation": wire.get("more"), "detail": wire.get("detail"), "projects": wire.get("projects"),
    })
    return "ask_memory", {**options, **leaf}


class ReadPage(Wire):
    page: Page
    part: Literal["text", "history", "units"] | None = Field(default=None, description="text (default), history, or units: the observations and the id each edit_note action takes.")
    more: str | None = Field(default=None, description="Token from the previous reply.")
    options: dict[str, Any] | None = Field(
        default=None, description="Rare knobs: links, frontmatter_only, include_raw, unit_ref, offset."
    )


def _spell_read(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The wire call of a read_memory call: the history and unit switches fold into `part`."""
    handled = {"path", "continuation", "include_history", "units"}
    wire = _given({"page": arguments.get("path"), "more": arguments.get("continuation")})
    if arguments.get("include_history"):
        wire["part"] = "history"
    elif arguments.get("units"):
        wire["part"] = "units"
    rest = {key: value for key, value in arguments.items() if key not in handled and value is not None}
    return {**wire, **({"options": rest} if rest else {})}


def _read_page(wire: dict[str, Any], ctx: Context) -> Translated:
    handled = frozenset({"path", "include_history", "continuation", "units"})
    options = _bag(wire, "options", ctx, "read_memory", handled, "read_page")
    leaf = _given({
        "path": wire["page"], "continuation": wire.get("more"),
        "include_history": True if wire.get("part") == "history" else None,
        "units": True if wire.get("part") == "units" else None,
    })
    return "read_memory", {**options, **leaf}


class ReadInstructions(Wire):
    topic: str | None = Field(
        default=None,
        description="A tool name, a door name or a door operation such as schema.save. Returns its arguments and an example.",
    )
    profile: str | None = Field(default=None, description="compact (default), full, diagnostics or session.")
    section: str | None = Field(default=None, description="One block of the compact policy; `index` lists them.")
    skill_contract: str | None = Field(default=None, description="Session digest of the installed skill.")


def _read_instructions(wire: dict[str, Any], ctx: Context) -> Translated:
    return "bootstrap", _given(wire)


# --- Notes ----------------------------------------------------------------------------------


class VocabularyDecision(Wire):
    """The answer to a `vocabulary_preparation` that a write returned instead of saving."""

    evidence_fingerprint: str = Field(description="The fingerprint in the vocabulary_preparation.")
    outcome: Literal["reuse", "create", "defer"]
    canonical: str | None = Field(default=None, description="The value to use; none for defer.")


def _vocabulary(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The leaf `vocabulary_decision`, which names `canonical` even when it is none."""
    decision = wire.get("vocabulary_decision")
    return {} if decision is None else {"vocabulary_decision": {"canonical": None, **decision}}


class CreateNote(Wire):
    title: str
    content: str = Field(description="Markdown body with a `## Observations` section of `- [category] text` lines. Link another page with [[Title]].")
    type: str | None = Field(default=None, description="Note type.")
    project: str | None = Field(default=None, description="The one project key of a research note. Any slug is legal; a new key registers itself.")
    projects: list[str] | None = Field(default=None, description="Project keys of any other note. Any slug is legal; a new key registers itself.")
    sources: list[str] | None = Field(default=None, description="Titles of the pages this draws from.")
    tags: list[str] | None = None
    fields: dict[str, Any] | None = Field(default=None, description="Type-specific metadata, such as severity or hypothesis.")
    unlinked_reason: str | None = _UNLINKED
    vocabulary_decision: VocabularyDecision | None = None
    suggest_links: bool | None = Field(default=None, description="Also return pages to link.")
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


_CREATE_HANDLED = frozenset({
    "title", "content", "note_type", "project", "sources", "tags", "suggestions", "validate_only",
    "response_detail", "relation_disposition", "relation_review_hash", "relation_review_reason",
    "draft_id", "draft_hash", "draft_token", "vocabulary_decision", "slug", "projects",
})


def _create_note(wire: dict[str, Any], ctx: Context) -> Translated:
    extra = _bag(wire, "fields", ctx, "remember", _CREATE_HANDLED, "create_note")
    leaf = _given({
        "title": wire["title"], "content": wire["content"], "note_type": wire.get("type"),
        "project": wire.get("project"), "projects": wire.get("projects"), "sources": wire.get("sources"),
        "tags": wire.get("tags"), "suggestions": wire.get("suggest_links"), "validate_only": wire.get("preview"),
        "response_detail": wire.get("detail"), **_unlinked(wire), **_vocabulary(wire),
    })
    return "remember", {**extra, **leaf}


class AddObservation(Wire):
    note: Page
    category: str = Field(description="Observation category, such as decision or finding.")
    text: str = Field(description="The observation, on one line.")
    tags: list[str] | None = None
    context: str | None = None
    links: list[Link] | None = Field(default=None, description="Pages this observation relates to.")
    kind: str | None = Field(default=None, description="A rich kind, such as hypothesis; omit for a compact observation.")
    verdict: str | None = Field(default=None, description="For a rich kind: confirmed, refuted, qualified, inconclusive or abandoned.")
    anchor: str | None = Field(default=None, description="A unique anchor within the note; derived when omitted.")
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


class EditNote(Wire):
    note: Page = Field(description="Page title, or the page id for replace_body.")
    why: Why
    action: Literal[
        "replace_text", "replace_section", "replace_body", "set_tags", "set_fields", "fill_row",
        "edit_observation", "remove_observation",
    ]
    old_text: str | None = Field(default=None, description="replace_text: the exact text to find.")
    new_text: str | None = Field(default=None, description="replace_text and replace_section: the new text.")
    replace_all: bool | None = Field(default=None, description="replace_text: change every match.")
    edits: list[EncodedTextEdit] | None = Field(default=None, description="replace_text: several changes in one commit.")
    heading: str | None = Field(default=None, description="replace_section: the heading to change.")
    position: Literal["append", "prepend", "replace"] | None = Field(default=None, description="replace_section: where the text goes; append by default.")
    create_missing: bool | None = Field(default=None, description="replace_section: add the heading when absent.")
    body: str | None = Field(default=None, description="replace_body: the whole new body.")
    tags: list[str] | None = Field(default=None, description="set_tags and edit_observation: the full tag list.")
    fields: dict[str, Any] | None = Field(default=None, description="set_fields: one field and its value.")
    row: str | None = Field(default=None, description="fill_row: the leading text of the row.")
    take: str | None = Field(default=None, description="fill_row: the text of the take.")
    overwrite: bool | None = Field(default=None, description="fill_row: replace a filled take.")
    observation: str | None = Field(default=None, description="edit_observation and remove_observation: the unit id.")
    category: str | None = Field(default=None, description="edit_observation: the new category.")
    text: str | None = Field(default=None, description="edit_observation: the new text.")
    context: str | None = Field(default=None, description="edit_observation: the new context.")
    links: list[Link] | None = Field(default=None, description="edit_observation: the full list of related pages.")
    unlinked_reason: str | None = _UNLINKED
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


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
            **_unlinked(wire), **detail,
        }
        return "observe_memory", leaf
    # A page id names the version the agent read; the leaf compares it after the write is admitted.
    guard = {"expected_hash": wire["note"]} if _is_id(wire["note"]) else {}
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
    operation.update(_unlinked(wire))
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
    content: str = Field(description="Markdown body of the new note.")
    why: Why
    type: str | None = None
    project: str | None = None
    projects: list[str] | None = None
    sources: list[str] | None = None
    tags: list[str] | None = None
    fields: dict[str, Any] | None = Field(default=None, description="Type-specific metadata.")
    unlinked_reason: str | None = _UNLINKED
    vocabulary_decision: VocabularyDecision | None = None
    preview: bool | None = _PREVIEW
    detail: Detail | None = _DETAIL


_SUPERSEDE_HANDLED = _CREATE_HANDLED | {"old_path", "reason"}


def _supersede_note(wire: dict[str, Any], ctx: Context) -> Translated:
    extra = _bag(wire, "fields", ctx, "replace_memory", _SUPERSEDE_HANDLED, "supersede_note")
    leaf = _given({
        "old_path": wire["note"], "title": wire["title"], "content": wire["content"],
        "reason": wire["why"], "note_type": wire.get("type"), "project": wire.get("project"),
        "projects": wire.get("projects"), "sources": wire.get("sources"), "tags": wire.get("tags"), "validate_only": wire.get("preview"),
        "response_detail": wire.get("detail"), **_unlinked(wire), **_vocabulary(wire),
    })
    return "replace_memory", {**extra, **leaf}


# --- Sources, evidence, episodes and transfers -----------------------------------------------


class SaveSource(Wire):
    title: str
    source_kind: str | None = Field(default=None, description="What the material is, as an open slug such as contract.")
    content: str | None = Field(default=None, description="The original text. Pass attached files in `files` instead of retyping them.")
    url: str | None = None
    files: list[FileHandle] | None = Field(default=None, description="Up to eight attached files, as the host hands them over.")
    domain: str | None = None
    projects: list[str] | None = None
    tags: list[str] | None = None
    why: str | None = Field(default=None, description="Why this is saved.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: raw_protection, source_identity, expected_sha256, correction.")
    detail: Detail | None = _DETAIL


def _save_source(wire: dict[str, Any], ctx: Context) -> Translated:
    handled = frozenset({"title", "content", "url", "tags", "why_captured", "source_kind", "domain",
                         "projects", "files", "response_detail"})
    options = _bag(wire, "options", ctx, "capture_source", handled, "save_source")
    leaf = _given({
        "title": wire["title"], "content": wire.get("content"), "url": wire.get("url"),
        "tags": wire.get("tags"), "why_captured": wire.get("why"), "source_kind": wire.get("source_kind"),
        "domain": wire.get("domain"), "files": wire.get("files"), "response_detail": wire.get("detail"),
        "projects": wire.get("projects"),
    })
    return "capture_source", {**options, **leaf}


class SaveEvidence(Wire):
    scope: str = Field(description="Case or project key, one path segment.")
    category: str = Field(description="One path segment.")
    name: str | None = Field(default=None, description="File name with extension, for `text`.")
    text: str | None = Field(default=None, description="Exact UTF-8 text to keep.")
    files: list[FileHandle] | None = Field(default=None, description="Up to eight attached files, as the host hands them over.")
    description: str | None = None
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: raw_protection, transcriptions, archive.")
    detail: Detail | None = _DETAIL


def _save_evidence(wire: dict[str, Any], ctx: Context) -> Translated:
    text, files = wire.get("text") is not None, wire.get("files") is not None
    if text == files or (text and wire.get("name") is None):
        raise Refusal("INVALID_ARGUMENT", "save_evidence takes `text` with `name`, or `files`, and not both.")
    if files:
        handled = frozenset({"scope", "category", "files", "response_detail"})
        options = _bag(wire, "options", ctx, "preserve_artifacts", handled, "save_evidence")
        if wire.get("description") is not None:
            raise Refusal("INVALID_ARGUMENT", "description applies to `text` evidence only.")
        return "preserve_artifacts", {**options, **_given({
            "scope": wire["scope"], "category": wire["category"], "files": wire["files"],
            "response_detail": wire.get("detail")})}
    handled = frozenset({"scope", "category", "filename", "content", "description", "response_detail"})
    options = _bag(wire, "options", ctx, "preserve_evidence", handled, "save_evidence")
    return "preserve_evidence", {**options, **_given({
        "scope": wire["scope"], "category": wire["category"], "filename": wire["name"],
        "content": wire["text"], "description": wire.get("description"), "response_detail": wire.get("detail")})}


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


class DownloadFiles(Wire):
    pages: list[str] = Field(min_length=1, max_length=8, description="Titles or ids of the pages whose originals to get.")
    detail: Detail | None = _DETAIL


def _download_files(wire: dict[str, Any], ctx: Context) -> Translated:
    return "transfer_artifact", {"operation": "download", "artifacts": wire["pages"],
                                 **_given({"response_detail": wire.get("detail")})}


def _spell_download(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The pages of a download call: the leaf's one `artifact` is a list of one.

    A call that names no page is a route in guidance, which leaves the pages to the tool schema.
    """
    if set(arguments) - {"operation", "artifact", "artifacts", "response_detail"}:
        return None
    pages = arguments.get("artifacts") or ([arguments["artifact"]] if arguments.get("artifact") else None)
    return _given({"pages": pages, "detail": arguments.get("response_detail")})


# --- Records and Planning -------------------------------------------------------------------


class QueryRecords(Wire):
    collection: str | None = Field(default=None, description="Omit to list the collections and their fields.")
    filters: Filters = None
    columns: list[str] | None = Field(default=None, description="Rows keep the `id` that edit_record takes only if you also name `record_id` and `item_version`.")
    sort_by: str | None = None
    descending: bool | None = None
    limit: int | None = None
    more: str | None = Field(default=None, description="Token from the previous reply.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: aggregate, date_from, date_to, view, output_format.")


_QUERY_HANDLED = frozenset({"collection", "filters", "columns", "sort_by", "descending", "limit", "continuation", "action"})


def _query_collection(command: str, tool: str, key: str | None):
    """`key` is the column that names a row: it and the row version make the row id the reply carries.

    A call that restricts `columns` gets those two added when `key` is given. A Records collection
    can be a dataset that has no row key, so a Records call gets only the columns it asked for.
    """

    def translate(wire: dict[str, Any], ctx: Context) -> Translated:
        options = _bag(wire, "options", ctx, command, _QUERY_HANDLED, tool)
        if wire.get("collection") is None:
            return command, {"action": "inspect"}
        columns = wire.get("columns")
        if columns is not None and key is not None:
            columns = [*columns, *(name for name in (key, "item_version") if name not in columns)]
        leaf = _given({
            "collection": wire["collection"], "filters": wire.get("filters"), "columns": columns,
            "sort_by": wire.get("sort_by"), "descending": wire.get("descending"), "limit": wire.get("limit"),
            "continuation": wire.get("more"),
        })
        return command, {"action": "query", **options, **leaf}
    return translate


def _spell_collection_list(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The list of collections is the query tool's; the inspection of one collection is a door operation."""
    return None if arguments.get("collection") is not None else {}


class AddRecord(Wire):
    collection: str
    item: dict[str, Any] = Field(description="The row's fields, by column name.")
    why: Why
    body: str | None = Field(default=None, description="Free text kept with the row.")
    hold: bool | None = Field(default=None, description="Hold a row that fails validation instead of refusing it.")
    held: str | None = Field(default=None, description="The id of a held row to resume.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: delivery, item_key.")
    detail: Detail | None = _DETAIL


_ADD_RECORD_HANDLED = frozenset({"action", "collection", "item", "why", "body", "hold", "held", "response_detail"})


def _add_record(wire: dict[str, Any], ctx: Context) -> Translated:
    options = _bag(wire, "options", ctx, "record_memory", _ADD_RECORD_HANDLED, "add_record")
    return "record_memory", {"action": "append", **options, **_with_detail(wire)}


class EditRecord(Wire):
    collection: str
    record: str = Field(description="The row `id` that query_records returned, or `row_id` where the collection has its own `id` field.")
    changes: dict[str, Any] = Field(description="Columns to change, by name.")
    why: Why
    body: str | None = None
    held: str | None = None
    refresh_presentation: bool | None = Field(
        default=None, description="Rebuild the row's managed Markdown view; `changes` may then be empty."
    )
    detail: Detail | None = _DETAIL


def _edit_record(wire: dict[str, Any], ctx: Context) -> Translated:
    return "record_memory", {"action": "update", **_given({
        "collection": wire["collection"], "item_id": wire["record"], "changes": wire["changes"],
        "why": wire["why"], "body": wire.get("body"), "held": wire.get("held"),
        "refresh_presentation": wire.get("refresh_presentation"),
        "response_detail": wire.get("detail")})}


class QueryPlanItems(Wire):
    collection: str | None = Field(default=None, description="Omit to list the plan collections.")
    filters: Filters = None
    columns: list[str] | None = None
    sort_by: str | None = None
    descending: bool | None = None
    limit: int | None = None
    more: str | None = Field(default=None, description="Token from the previous reply.")
    options: dict[str, Any] | None = Field(default=None, description="Rare knobs: lifecycle, hierarchy_mode, aggregate, view, output_format.")


class AddPlanItem(Wire):
    collection: str
    item: dict[str, Any] = Field(description="The item's fields, by column name.")
    why: Why
    body: str | None = None
    detail: Detail | None = _DETAIL


def _add_plan_item(wire: dict[str, Any], ctx: Context) -> Translated:
    return "plan_memory", {"action": "add", **_with_detail(wire)}


class EditPlanItem(Wire):
    collection: str
    plan_item: str = Field(description="The item id that query_plan_items returned.")
    why: Why
    changes: dict[str, Any] | None = Field(default=None, description="Columns to change, by name.")
    status: str | None = Field(default=None, description="Move the item to this status. A refused move names the allowed ones.")
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
    queue: Literal[
        "due", "activation", "stale", "contradiction", "unprocessed_sources", "unlinked",
        "relation_suggestions", "imports", "upkeep", "plan_progress", "vocabulary", "procedure_approvals",
    ] | None = Field(default=None, description="One queue; due items by default.")
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


class DecideReviewItem(Wire):
    review_item: str = Field(description="The id that list_review_items returned.")
    decision: Literal["accept", "dismiss", "snooze", "reopen"] = Field(description="accept applies to a relation suggestion.")
    until: str | None = Field(default=None, description="snooze: YYYY-MM-DD.")
    why: str | None = None
    relation: str | None = Field(default=None, description="accept: the relation to record, when the suggestion offers several.")
    vocabulary_ref: str | None = Field(default=None, description="accept: the recorded vocabulary decision that names a relation the suggestion does not offer.")
    vocabulary_fingerprint: str | None = Field(default=None, description="accept: the fingerprint of that decision.")
    detail: Detail | None = _DETAIL


def _spell_decision(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The wire call of a decision on one review item; a family, envelope or vocabulary decision is a door operation."""
    if arguments.get("item_id") is None or arguments.get("action") not in ("dismiss", "snooze", "reopen"):
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
          "Search notes, sources and evidence for something recall_context did not cover. Hits are named by title; open one with read_page. A miss covers this query only.",
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
          "Save a lasting conclusion as a new note. Put raw material in save_source and proof in save_evidence. Link another page with [[Title]], or give unlinked_reason.",
          CreateNote, "write", _create_note,
          [Route("remember", inverse={"note_type": "type", "suggestions": "suggest_links", **_NOTE_INVERSE},
                 bag="fields", derived=frozenset({"draft_id", "draft_hash", "draft_token"}))],
          example={"title": "Weekly releases beat batch releases", "type": "insight", "content": "Weekly releases cut rollback size.\n\n## Observations\n- [finding] Weekly releases halved rollback size #release\n\n## Relations\n- supports [[Release cadence]]"},
          captures=True),
    _tool("add_observation", "Add observation",
          "Add one observation to an existing note. Nothing existing changes. Use preview to check first.",
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
                 inverse={"path": "note", "unit": "observation", "unit_ref": "observation", "content": "text", "reason": "why", **_NOTE_INVERSE}),
           Route("observe_memory", when={"operation": "remove"}, add={"action": "remove_observation"},
                 inverse={"path": "note", "unit": "observation", "unit_ref": "observation", "reason": "why", **_NOTE_INVERSE})],
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
          "Get a one-use upload for bytes that are on this machine. The reply gives the one step that moves them; then call save_source or save_evidence with the handle.",
          UploadFiles, "write", _upload_files,
          [Route("transfer_artifact", when={"operation": "upload"}, inverse={"response_detail": "detail"})]),
    _tool("download_files", "Download files",
          "Get the original files of one or more pages back out of memory. The reply gives a link or a command that saves each.",
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
          "Change fields of one record, using the row id that query_records returned.",
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
          "Change one plan item, or move it to a new status with `status`. A refused move names the allowed ones.",
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
          claims=(("triage_memory", "decision"), ("connect_memory", "accept-relation"))),
)}
