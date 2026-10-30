"""Source-owned operation entries and their public MCP jobs.

Each entry names one grouped operation, its accepted fields, literal conditions,
public kind and effect. Command construction supplies the published signature;
schemas, normalization, guidance and coverage read the same metadata.

The module has no command-registry import. Leaves can read their entries without
loading the registry. Selector values describe the closed product API, never
vocabulary that a vault or customer defines. Vocabulary subjects and governance
coverage come from their existing registries.

`spell_call` and `unspell_call` preserve grouped execution identities while naming
the tools and arguments that each serving surface publishes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
from types import MappingProxyType
from typing import Any, Literal, get_args

Effect = Literal["read", "write"]

#: How the MCP surface publishes a command that has renderings. `nested` carries the
#: operation in one `operation` object, and `grouped` lists only the grouped command.
#: `grouped` remains an internal spelling for compatibility and historical measurements;
#: the server no longer offers the retired grouped comparison catalogue.
ToolShape = Literal["nested", "grouped"]
TOOL_SHAPES: tuple[str, ...] = get_args(ToolShape)
DEFAULT_TOOL_SHAPE: ToolShape = "nested"

#: The argument path that names the operation in a nested call, as guidance and refusals say it.
OPERATION_KIND = "operation.kind"


@dataclass(frozen=True, slots=True)
class LiteralConstraint:
    """An exact scalar at a grouped argument path; omission is admitted explicitly."""

    path: tuple[str, ...]
    value: bool | str | None
    allow_omitted: bool = False
    #: A public kind can supply a secondary grouped selector instead of repeating it.
    implied: bool = False

    def __post_init__(self) -> None:
        if not self.path or any(not isinstance(part, str) or not part for part in self.path):
            raise ValueError("a literal constraint needs a nonempty field path")
        if self.value is not None and type(self.value) not in (bool, str):
            raise ValueError("a literal constraint accepts only boolean, string or null")

    def matches(self, arguments: Mapping[str, object]) -> bool:
        current: object = arguments
        for part in self.path:
            if not isinstance(current, Mapping):
                return False
            if part not in current:
                return self.allow_omitted or self.implied
            current = current[part]
        # Python equates True and 1; a JSON boolean discriminant must not admit an integer.
        return type(current) is type(self.value) and current == self.value


@dataclass(frozen=True, slots=True)
class OperationEntry:
    """One operation of a grouped command, narrowed by exact argument literals."""

    action: str
    rendering: str
    effect: Effect
    fields: frozenset[str]
    required: frozenset[str]
    #: A write that can overwrite or remove existing content. Additive writes stay False.
    destructive: bool = False
    constraints: tuple[LiteralConstraint, ...] = ()
    #: Public discriminator when the grouped selector would collide with another job.
    public_kind: str | None = None

    @property
    def kind(self) -> str:
        return self.public_kind or self.action

    def __post_init__(self) -> None:
        if not self.required <= self.fields:
            raise ValueError(f"required fields outside the accepted fields: {self.action}")
        if self.effect == "read" and self.destructive:
            raise ValueError(f"a read operation cannot be destructive: {self.action}")
        paths = [constraint.path for constraint in self.constraints]
        if any(path[0] not in self.fields for path in paths):
            raise ValueError(f"constraint outside the accepted fields: {self.action}")
        if len(set(paths)) != len(paths) or any(
            left != right and left == right[:len(left)] for left in paths for right in paths
        ):
            raise ValueError(f"overlapping constraint paths: {self.action}")

    def matches(self, action: object, arguments: Mapping[str, object]) -> bool:
        return self.action == action and all(item.matches(arguments) for item in self.constraints)


@dataclass(frozen=True, slots=True)
class RenderingSpec:
    """A public tool spelling of a grouped command: one side of its operations."""

    name: str
    effect: Effect
    title: str
    description: str
    #: Top-level tool arguments beside `operation`; the command must take each one.
    tool_arguments: tuple[str, ...] = ()
    #: Wire field names that would otherwise collide with the operation discriminator.
    field_aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Rendering:
    """A rendering with the entries it carries, in the order the table lists them."""

    name: str
    command: str
    selector: str | None
    effect: Effect
    title: str
    description: str
    variants: tuple[OperationEntry, ...]
    #: Arguments the tool takes beside `operation`, from the grouped command's own signature.
    tool_arguments: tuple[str, ...] = ()
    field_aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        wire = [name for name, _ in self.field_aliases]
        leaf = [name for _, name in self.field_aliases]
        if len(set(wire)) != len(wire) or len(set(leaf)) != len(leaf):
            raise ValueError("field aliases must map distinct wire and leaf fields")
        if "kind" in wire or set(wire) & set(leaf):
            raise ValueError("field aliases must not collide with the discriminator or each other")
        fields = frozenset().union(*(variant.fields for variant in self.variants))
        if "kind" in fields and "kind" not in leaf:
            raise ValueError("a leaf kind field needs an alias to preserve the discriminator")
        if set(wire) & fields:
            raise ValueError("a field alias collides with another leaf field")

    def leaf_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Translate wire fields without allowing an alias to overwrite a raw leaf field."""
        result = dict(fields)
        for wire, leaf in self.field_aliases:
            if leaf in fields:
                raise ValueError(f"conflicting raw field {leaf!r}; use alias {wire!r}")
            if wire in fields:
                result[leaf] = result.pop(wire)
        return result

    def wire_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Translate grouped fields without overwriting an existing alias argument."""
        result = dict(fields)
        for wire, leaf in self.field_aliases:
            if wire in fields:
                raise ValueError(f"conflicting alias field {wire!r} for {leaf!r}")
            if leaf in fields:
                result[wire] = result.pop(leaf)
        return result

    @property
    def read_only(self) -> bool:
        return self.effect == "read"

    @property
    def destructive(self) -> bool:
        """True when any variant can overwrite or remove content; always False for a read tool."""
        return any(variant.destructive for variant in self.variants)

    @property
    def kinds(self) -> tuple[str, ...]:
        """The selector values this tool carries, in table order."""
        return tuple(dict.fromkeys(variant.kind for variant in self.variants))

    def take_tool_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Remove and return this tool's own arguments, which sit beside `operation` on the wire."""
        return {key: arguments.pop(key) for key in self.tool_arguments if key in arguments}

    def arguments_for(self, entry: OperationEntry) -> dict[str, object]:
        """The grouped-command arguments that select this variant, and nothing else."""
        arguments: dict[str, object] = {self.selector: entry.action} if self.selector else {}
        for constraint in entry.constraints:
            container = arguments
            for part in constraint.path[:-1]:
                container = container.setdefault(part, {})
            container[constraint.path[-1]] = constraint.value
        return arguments

    def selected_arguments(self, entry: OperationEntry, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Flatten the selected public kind and its implied grouped selectors."""
        from copy import deepcopy

        result = deepcopy(dict(fields))
        for constraint in entry.constraints:
            if constraint.implied:
                container = result
                for part in constraint.path[:-1]:
                    container = container.setdefault(part, {})
                container.setdefault(constraint.path[-1], constraint.value)
        if self.selector:
            result[self.selector] = entry.action
        return result

    def constraint_hint(self, kind: object) -> str | None:
        """Name the admitted discriminants when a known kind has no matching alternative."""
        variants = [variant for variant in self.variants if variant.kind == kind]
        paths = dict.fromkeys(item.path for variant in variants for item in variant.constraints)
        aliases = {leaf: wire for wire, leaf in self.field_aliases}
        parts = []
        for path in paths:
            values = dict.fromkeys(
                item.value for variant in variants for item in variant.constraints if item.path == path
            )
            wire_path = (aliases.get(path[0], path[0]), *path[1:])
            parts.append(f"{'.'.join(wire_path)} to be one of: {', '.join(str(v) for v in values)}")
        return f"{kind} needs {'; '.join(parts)}" if parts else None

    def variant_for(self, kind: object, arguments: Mapping[str, object]) -> OperationEntry | None:
        """The variant this tool carries for a selector and all literal constraints."""
        if not isinstance(kind, str):
            return None
        return next(
            (v for v in self.variants if v.kind == kind
             and all(item.matches(arguments) for item in v.constraints)),
            None,
        )


@dataclass(frozen=True, slots=True)
class CommandOperations:
    """Everything the entries say about one grouped command."""

    command: str
    selector: str | None
    entries: tuple[OperationEntry, ...]
    renderings: tuple[RenderingSpec, ...]
    #: These writes can satisfy the capture hook's durable-knowledge check.
    capture_write: bool = False

    def actions(self) -> frozenset[str]:
        return frozenset(entry.action for entry in self.entries)

    def every_field(self) -> frozenset[str]:
        """Every field some operation accepts, which is the grouped command's whole signature."""
        return frozenset().union(*(entry.fields for entry in self.entries))

    def action_fields(self) -> dict[str, frozenset[str]]:
        """Accepted fields per selector value; its alternatives share one set."""
        return self._per_action("fields")

    def required_fields(self) -> dict[str, frozenset[str]]:
        """Required fields per selector value; its alternatives share one set."""
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
                tool_arguments=spec.tool_arguments,
                field_aliases=spec.field_aliases,
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
    constraints: tuple[LiteralConstraint, ...] = (),
    public_kind: str | None = None,
) -> OperationEntry:
    return OperationEntry(
        action=action,
        rendering=rendering,
        effect=effect,
        fields=frozenset(fields),
        required=frozenset(required),
        destructive=destructive,
        constraints=constraints,
        public_kind=public_kind,
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
    capture_write=True,
    renderings=(
        RenderingSpec(
            READ_RECORDS,
            "read",
            "Read records",
            "Read human-owned Records without changing them: describe the manifest contract, "
            "validate or inspect a collection, query rows, read an item's history, or preview "
            "or check an import. `operation.kind` selects the operation. Originals belong in "
            "Sources or Evidence. Help: bootstrap(section=\"records_planning\").",
        ),
        RenderingSpec(
            CHANGE_RECORDS,
            "write",
            "Change records",
            "Change human-owned Records: create a collection, append, update or bulk-upsert "
            "items, discard a held candidate, revise or rebaseline a manifest, or start or "
            "cancel an import. `operation.kind` selects the operation. Propose a new collection "
            "before creating it. Guarded writes need the hashes that the matching read returns. "
            "Help: bootstrap(section=\"records_planning\").",
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
            effect="read", constraints=(LiteralConstraint(("import_request", "mode"), "preview"),),
        ),
        _entry(
            "import", READ_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="read", constraints=(LiteralConstraint(("import_request", "mode"), "status"),),
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
            effect="write", destructive=True,
            constraints=(LiteralConstraint(("import_request", "mode"), "start"),),
        ),
        _entry(
            "import", CHANGE_RECORDS, _RECORD_IMPORT_FIELDS, _RECORD_IMPORT_FIELDS,
            effect="write", constraints=(LiteralConstraint(("import_request", "mode"), "cancel"),),
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
    capture_write=True,
    renderings=(
        RenderingSpec(
            READ_PLANS,
            "read",
            "Read plans",
            "Read human-owned Planning intent without changing it: list or inspect plan "
            "collections, validate a manifest, query plan items, or read an item's history. "
            "`operation.kind` selects the operation. Reread if stale; time never proves "
            "completion. Help: bootstrap(section=\"records_planning\").",
        ),
        RenderingSpec(
            CHANGE_PLANS,
            "write",
            "Change plans",
            "Change human-owned Planning intent: create a collection, add, update or triage a "
            "plan item, or revise or rebaseline a manifest. `operation.kind` selects the "
            "operation. Writes select one queried item and need the hashes that the read "
            "returned. Reread if stale; time never proves completion. "
            "Help: bootstrap(section=\"records_planning\").",
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

# These rows describe closed product API selectors, never vault vocabulary. Field names
# belong to the existing command signatures; schema types and help come from those signatures.
# A missing field list is used only for a singleton that carries its complete signature.
_TOOL_HELP = {
    "bootstrap": ("read", "Read setup and status", "Read instructions, saved engagement settings or writer status. The writer-status operation works without building instructions."),
    "read_memory": ("read", "Read memory", "Read a page or view, or the complete bounded graph with coverage and counts."),
    "browse_memory": ("read", "Browse memory", "Browse folders, list files or trash, or preview file reclassification without writing."),
    "prepare_note": ("read", "Prepare a note", "Prepare a sourced compilation draft or validate an observation without writing."),
    "preserve_evidence": ("write", "Preserve evidence", "Preserve original text or attached files as governed Evidence. File attachments stay in top-level files."),
    "read_review": ("read", "Read review", "Read review queues, provenance, drift, backlog or one review item's bounded context. Submit meaning questions through triage_memory."),
    "triage_memory": ("write", "Triage memory", "Save a review decision or submit a meaning question. These writes do not grant mutation authority."),
    "read_media": ("read", "Read media", "Read native video frames or inspect media processing status."),
    "change_media": ("write", "Change media", "Process, retry or reprocess media."),
    "read_connections": ("read", "Read connections", "Find links, relations and entity candidates, or inspect a bounded graph neighborhood."),
    "change_connections": ("write", "Change connections", "Create a governed entity or accept a reviewed relation using current fingerprints."),
    "read_schema": ("read", "Read schema", "Inspect schemas without saving changes. Use preview for a registry delta merged with its live overlay, including collision and cap checks. Pass proposal={\"upsert\":{\"<key>\":{...}}}. Use validate-document for a complete registry document."),
    "change_schema": ("write", "Change schema", "Save, refresh or restore a reviewed schema or vocabulary contract."),
    "read_governance": ("read", "Read governance", "Inspect access policy, evaluate an audience or read vocabulary requests. Existing credential rules apply."),
    "change_governance": ("write", "Change governance", "Persist owner policy proposals, standing grants, rule changes or companion backfill. Existing owner checks apply."),
    "grant_session_access": ("write", "Grant session access", "Redeem a token to grant access to this authorization session. A verified authorization session is required."),
    "change_session_access": ("write", "Change session access", "Revoke a session grant or declare a purpose. A verified authorization session is required."),
    "manage_authorization_session": ("write", "Manage authorization session", "Open, inspect, rotate or close an authorization session. Open forbids a credential; other operations require one. Session status remains write-classified."),
    "change_files": ("write", "Change files", "Create, append, move, delete, recover or reclassify governed files. Preview reclassification through browse_memory."),
    "change_observations": ("write", "Change observations", "Add, update or remove a semantic unit with the existing review and fingerprint guards."),
    "change_settings": ("write", "Change settings", "Set or clear saved engagement only on an explicit user setting-change request. Inspect settings through bootstrap first."),
    "read_episode": ("read", "Read episode", "Inspect episode recaps, candidates or source coverage without writing."),
    "record_episode": ("write", "Record episode", "Record an episode, prepare a capture, save a disposition or resume its workflow."),
    "check_memory": ("read", "Check memory", "Inspect maintenance findings and preview admitted repairs without writing."),
    "repair_memory": ("write", "Repair memory", "Apply admitted maintenance repairs with their current guards. Curation proposals persist workflow state."),
    "read_import": ("read", "Read import", "Scan existing material without importing it, inspect import runs or read a bounded work item. Stage external folders with the CLI import command first. Status retains recorded coverage and verification counts."),
    "import_memory": ("write", "Import memory", "Import selected originals and optional draft notes without changing original files. Select folder rules with adopt_as, note_type, project and domain. Planning saves resumable progress. Carry returned continuations through plan and apply; finish records coverage."),
}


def _literal(field: str, value: bool | str | None, omitted: bool = False, *, implied: bool = False) -> LiteralConstraint:
    return LiteralConstraint(tuple(field.split(".")), value, allow_omitted=omitted, implied=implied)


def _row(action, tool, fields=None, required=(), *, kind=None, constraints=(), destructive=False):
    return (action, tool, fields, required, kind, constraints, destructive)


# Fields remain grouped by the leaf job that owns them. Conditional values are closed API
# literals and must agree with the lease classifier in validate_operation_entries.
_ADOPTION_SCAN_FIELDS = ("path", "max_depth", "include_hidden", "samples", "pack_limit")
_REMAINING = {
    "bootstrap": (None, (_row("instructions", "bootstrap"),)),
    "coordination_status": (None, (_row("writer-status", "bootstrap"),)),
    "configure_memory": ("action", (
        _row("inspect", "bootstrap", (), kind="settings"),
        _row("set", "change_settings", ("prominence", "expected_revision", "context"), ("prominence", "expected_revision")),
        _row("clear", "change_settings", ("context", "expected_revision"), ("context", "expected_revision"), destructive=True),
    )),
    "read_memory": (None, (_row("page", "read_memory"),)),
    "viewer_map": (None, (_row("graph", "read_memory"),)),
    "browse_memory": ("mode", (
        _row("overview", "browse_memory", ("path", "max_depth", "include_hidden", "samples")),
        _row("list", "browse_memory", ("path", "recursive", "include_hidden", "limit", "cursor"), kind="folder-list"),
    )),
    "compile_source": (None, (_row("compile", "prepare_note"),)),
    "query_dataset": (None, (_row("dataset", "read_memory"),)),
    "adopt_vault": ("mode", (
        _row("scan-only", "read_import", (*_ADOPTION_SCAN_FIELDS, "semantic_max_files", "semantic_max_bytes", "semantic_example_limit")),
        _row("save-manifest", "import_memory", (*_ADOPTION_SCAN_FIELDS, "manifest_path")),
        _row("copy-as-sources", "import_memory", (*_ADOPTION_SCAN_FIELDS, "selected_paths"), ("selected_paths",)),
        _row("compile-selected", "import_memory", (*_ADOPTION_SCAN_FIELDS, "selected_paths"), ("selected_paths",)),
    )),
    "adoption_studio": ("action", (
        _row("start", "import_memory", ("path", "include_hidden", "initialize_kb", "collection", "after", "consume")),
        _row("status", "read_import", ("run_id", "path")),
        _row("select", "import_memory", ("run_id", "include", "exclude", "overrides", "include_junk"), ("run_id",)),
        _row("plan", "import_memory", ("run_id", "continuation"), ("run_id",)),
        _row("apply", "import_memory", ("run_id", "plan_id", "continuation", "retry_failed", "only_paths"), ("run_id", "plan_id")),
        _row("cancel", "import_memory", ("run_id", "why"), ("run_id",)),
        _row("finish", "import_memory", ("run_id", "write_manifest"), ("run_id",)),
        _row("work-item", "read_import", ("run_id", "sources", "max_sources", "max_chars_per_source"), ("run_id",)),
        _row("propose", "import_memory", ("run_id", "proposals"), ("run_id",)),
        _row("apply-proposal", "import_memory", ("ref", "expected_fingerprint", "why", "expected_hash"), ("ref", "expected_fingerprint", "why"), destructive=True),
    )),
    "read_media": (None, (_row("video", "read_media"),)),
    "process_media": ("operation", (
        _row("status", "read_media", ("path", "paths")),
        _row("process", "change_media", ("path", "paths", "language", "multilingual")),
        _row("retry", "change_media", ("path", "paths")),
        _row("reprocess", "change_media", ("path", "paths", "language", "multilingual")),
    )),
    "triage_memory": (None, (_row("decision", "triage_memory"),)),
    "review_item_context": (None, (_row("item-context", "read_review"),)),
    "review_memory": ("mode", (
        _row("attention", "read_review", ("categories", "limit", "state")),
        _row("activation", "read_review", ("categories", "limit", "state")),
        _row("item", "read_review", ("ref", "limit"), ("ref",)),
        _row("audit", "read_review", ("categories", "detail", "legacy_sample_limit")),
        _row("dispositions", "read_review", ()),
        _row("provenance", "read_review", ("tag", "key", "value", "path", "limit")),
        _row("evolution", "read_review", ("query", "path", "limit")),
        _row("compilation", "read_review", ("sources", "suggested_title"), ("sources",)),
        *(_row(mode, "read_review", ("categories", "limit", "state")) for mode in (
            "stale", "contradiction", "unprocessed-sources", "relation-debt", "relation-queue")),
        _row("adoption", "read_review", ("ref", "state", "limit")),
        _row("upkeep", "read_review", ("categories", "limit")),
        _row("plan-progress", "read_review", ("path", "limit")),
        _row("write-advisory-result", "read_review", ("ref",), ("ref",)),
        _row("vocabulary", "read_review", ("limit", "continuation", "state", "path", "query", "family"),
             constraints=(_literal("path", None, True), _literal("query", "", True), _literal("family", None, True))),
        _row("vocabulary", "triage_memory", ("path", "query", "family", "ref"), ("path", "query", "family"), kind="question"),
    )),
    "connect_memory": ("operation", (
        _row("suggest-links", "read_connections", ("path", "draft_title", "draft_body", "limit", "scope")),
        _row("suggest-relations", "read_connections", ("path", "draft_title", "draft_body", "include_model_suggestions", "limit")),
        _row("context", "read_connections", ("path", "query", "unit_ref", "categories", "kinds", "depth", "relation_types", "node_types", "max_nodes", "max_edges", "traversal_profile", "limit", "max_body_chars", "entity_family")),
        _row("graph-context", "read_connections", ("path", "query", "unit_ref", "categories", "kinds", "depth", "relation_types", "node_types", "max_nodes", "max_edges", "traversal_profile", "limit", "max_body_chars", "entity_family")),
        _row("inbound-links", "read_connections", ("target", "path")),
        _row("resolve-entity", "read_connections", ("name", "entity_type", "entity_family", "limit"), ("name",)),
        _row("resolve-relation", "read_connections", ("path", "target", "query", "requested_relation", "limit", "continuation")),
        _row("create-entity", "change_connections", ("entity_type", "entity_family", "registry_scope", "name", "slug", "summary", "why_in_kb", "tags", "connections", "affiliation", "relationship", "domain", "language", "repo", "license", "used_in", "decided", "project", "decision_status", "aliases", "identity_decision", "facets", "relations"), ("name",)),
        _row("accept-relation", "change_connections", ("path", "requested_relation", "ref", "expected_hash", "expected_fingerprint", "why", "vocabulary_ref", "vocabulary_fingerprint"), ("ref", "expected_hash", "expected_fingerprint", "why")),
    ), True),
    "observe_memory": ("operation", (
        _row("validate", "prepare_note", None, ("path",), kind="observation"),
        _row("add", "change_observations", None, ("path", "content", "category")),
        _row("update", "change_observations", None, ("path", "unit_ref", "expected_fingerprint", "expected_hash"), destructive=True),
        _row("remove", "change_observations", ("path", "unit_ref", "expected_fingerprint", "expected_hash", "reason"), ("path", "unit_ref", "expected_fingerprint", "expected_hash"), destructive=True),
    ), True),
    "manage_memory_file": ("operation", (
        _row("list", "browse_memory", ("path", "recursive", "include_hidden"), kind="files"),
        _row("trash-list", "browse_memory", ("date",)),
        _row("propose-reclassification", "browse_memory", ("path", "source_kind", "domain", "reason"), ("path",)),
        *(_row(action, "change_files", ("path", "content", "frontmatter", "overwrite", "allow_curated", "kind", "parents", "validate_only", "draft_id", "draft_hash", "draft_token", "semantic_transition_token", "relation_disposition", "relation_review_hash", "relation_review_reason", "promotion_reason"), ("path",), constraints=(_literal("validate_only", False, True),), destructive=action == "append") for action in ("create", "append")),
        *(_row(action, "browse_memory", ("path", "content", "frontmatter", "overwrite", "allow_curated", "kind", "parents", "validate_only", "draft_id", "draft_hash", "draft_token", "semantic_transition_token", "relation_disposition", "relation_review_hash", "relation_review_reason", "promotion_reason"), ("path",), kind=f"validate-{action}", constraints=(_literal("validate_only", True, implied=True),)) for action in ("create", "append")),
        _row("move", "change_files", ("old_path", "new_path", "update_wikilinks", "allow_curated", "promotion_reason"), ("old_path", "new_path"), destructive=True),
        _row("delete", "change_files", ("path", "confirm", "recursive", "force_orphan", "force_superseded", "allow_curated", "expected_dead_inbound"), ("path",), destructive=True),
        _row("recover", "change_files", ("trash_path", "path", "restore_path"), ()),
        _row("reclassify", "change_files", ("path", "old_path", "source_kind", "domain", "reason"), ("reason",), destructive=True),
        _row("revert-reclassification", "change_files", ("path", "reason"), ("path",), destructive=True),
    ), True),
    "episode_memory": ("action", (
        _row("record", "record_episode", ("episode", "subject", "summary", "worked_on", "decided", "open", "said", "about", "client")),
        _row("inspect", "read_episode", ("episode", "subject")),
        _row("candidates", "read_episode", ("episode", "order", "max_leaves", "postcommit")),
        _row("coverage", "read_episode", ("episode", "order", "max_leaves", "postcommit")),
        _row("prepare", "record_episode", ("episode", "candidate", "proposal", "input_revision", "journal_digest", "order", "max_leaves")),
        _row("disposition", "record_episode", ("episode", "candidate", "disposition", "reason", "input_revision", "journal_digest")),
        _row("resume", "record_episode", ("episode", "candidate", "input_revision", "journal_digest", "order", "max_leaves", "postcommit")),
    ), True),
}


def _schema_rows():
    from .vocabulary import registry_specs

    rows = []
    # The generic vocabulary operations come from the registry contract. Their subjects
    # come from registered specs, so a new registry needs no vocabulary list here.
    for subject in registry_specs():
        for action, fields, required in (
            ("inspect", ("subject", "registry_scope", "limit", "continuation"), ()),
            ("propose", ("subject", "proposal", "registry_scope"), ("proposal",)),
            ("save", ("subject", "proposal", "expected_hash", "why", "vocabulary_ref", "vocabulary_fingerprint", "registry_scope"), ("proposal", "why")),
            ("history", ("subject", "registry_scope"), ()),
            ("restore", ("subject", "version", "expected_hash", "why", "registry_scope"), ("version", "why")),
        ):
            rows.append(_row(action, "change_schema" if action in {"save", "restore"} else "read_schema",
                             fields, required, kind="preview" if action == "propose" else None,
                             constraints=(_literal("subject", subject),), destructive=action in {"save", "restore"}))
    # These are closed schema API subjects, not extensible vocabulary entry keys.
    for subject in ("contract", "categories", "relations", "traversal-profiles"):
        for action in ("infer", "validate", "diff"):
            fields = ("subject", "name", "project", "page_type", "strict", "compare_to", "proposal", "why", "expected_hash", "save", "context", "date_from", "date_to", "continuation", "limit", "include_model_suggestions")
            subject_constraint = _literal("subject", subject, subject == "contract")
            constraints = (subject_constraint, _literal("save", False, True)) if action == "infer" else (subject_constraint,)
            # Registry document validation has different semantics from a live-delta preview.
            kind = "validate-document" if action == "validate" and subject in registry_specs() else None
            rows.append(_row(action, "read_schema", fields, ("name",) if subject == "contract" else (), kind=kind, constraints=constraints))
            if action == "infer":
                rows.append(_row(action, "change_schema", fields, ("name",) if subject == "contract" else ("proposal",),
                                 kind="infer-and-save", constraints=(subject_constraint, _literal("save", True, implied=True)), destructive=True))
    for action, fields, required in (
        ("inventory", ("subject",), ()),
        ("inspect", ("subject", "name"), ("name",)),
        ("validate", ("subject", "name", "proposal"), ()),
        ("resolve", ("subject", "name", "context", "proposal"), ("context",)),
        ("preview", ("subject", "name", "proposal"), ("proposal",)),
        ("save", ("subject", "name", "proposal", "expected_hash", "why"), ("proposal", "why")),
        ("refresh", ("subject", "name", "expected_hash", "why"), ("name", "expected_hash", "why")),
    ):
        write = action in {"save", "refresh"}
        rows.append(_row(action, "change_schema" if write else "read_schema", fields, required,
                         constraints=(_literal("subject", "workflow-contracts"),), destructive=write))
    for subject, save_operation in (("context-roles", "save-roles"), ("activation-conventions", "save-conventions")):
        for action, fields, required in (
            ("validate", ("subject", "proposal"), ()),
            ("diff", ("subject", "proposal"), ()),
            (save_operation, ("subject", "proposal", "why", "expected_hash"), ("proposal", "why", "expected_hash")),
            ("history", ("subject",), ()),
            ("restore", ("subject", "version", "expected_hash", "why"), ("version", "expected_hash", "why")),
        ):
            write = action in {save_operation, "restore"}
            rows.append(_row(action, "change_schema" if write else "read_schema", fields, required,
                             constraints=(_literal("subject", subject),), destructive=write))
    rows.extend((
        _row("inspect", "read_schema", ("subject", "name"), constraints=(_literal("subject", "query-engine"),)),
        _row("resolve-entity-type", "read_schema", ("subject", "query", "requested_type", "limit", "continuation"), constraints=(_literal("subject", "entity-types"),)),
        _row("propose-relation", "read_schema", ("subject", "proposal", "date_from", "date_to", "continuation", "limit"), ("proposal",), constraints=(_literal("subject", "relations"),)),
        _row("save-relations", "change_schema", ("subject", "proposal", "expected_hash", "why", "vocabulary_ref", "vocabulary_fingerprint"), ("proposal", "why"), constraints=(_literal("subject", "relations"),), destructive=True),
        _row("save-entity-types", "change_schema", ("subject", "proposal", "expected_hash", "why", "vocabulary_ref", "vocabulary_fingerprint"), ("proposal", "why"), destructive=True),
        _row("census", "read_schema", ("subject", "detail", "date_from", "date_to"), constraints=(_literal("subject", "relations"),)),
    ))
    return "operation", tuple(rows)


def _governance_rows():
    from .governance.operations import OPERATION_SPECS

    fields = {
        "list": (), "explain": ("path", "audience", "purpose"),
        "simulate": ("paths", "audience", "purpose"),
        "vocabulary-request": ("vocabulary_request_id",), "vocabulary-status": ("vocabulary_request_id",),
        "propose": ("documents", "selector_paths", "intent", "ttl_seconds", "target_ceiling", "duration"),
        "commit": ("proposal_id",),
        "grant": ("scope", "token", "authorization_session", "duration_seconds", "purpose", "scope_ids", "audience", "ceiling", "path", "raw_release"),
        "revoke": ("scope", "grant_id", "authorization_session"),
        "suspend": ("rule_ids",), "resume": ("rule_ids",), "undo": ("proposal_id",),
        "declare": ("authorization_session", "purpose"),
        "backfill_companion": ("backfill_action", "companion_input", "proposal_id"),
        "session": ("session_action", "authorization_session", "ttl_seconds"),
    }
    if set(fields) != set(OPERATION_SPECS):
        raise RuntimeError("governance operation fields drifted from OPERATION_SPECS")
    tools = {"inspect": "read_governance", "owner": "change_governance",
             "token_session": "grant_session_access", "self_session": "change_session_access"}
    rows = []
    for action, spec in OPERATION_SPECS.items():
        if action == "session":
            # These are the command's closed authorization-session actions.
            for session_action in ("open", "status", "rotate", "close"):
                rows.append(_row(action, "manage_authorization_session", fields[action],
                                 kind=session_action, constraints=(_literal("session_action", session_action, implied=True),)))
        elif action in {"grant", "revoke"}:
            rows.append(_row(action, tools[spec.authorization], fields[action],
                             constraints=(_literal("scope", "session", True),), destructive=spec.destructive))
            rows.append(_row(action, tools[spec.authorization], fields[action],
                             constraints=(_literal("scope", None),), destructive=spec.destructive))
            for variant in spec.variants:
                rows.append(_row(action, tools[variant.authorization], fields[action], kind=f"{action}-{variant.mode}",
                                 constraints=(_literal("scope", variant.mode, implied=True),), destructive=variant.destructive))
        elif action == "backfill_companion":
            for mode in ("preview", "commit"):
                rows.append(_row(action, "change_governance", fields[action], kind=f"backfill-{mode}",
                                 constraints=(_literal("backfill_action", mode, implied=True),)))
        else:
            rows.append(_row(action, tools[spec.authorization], fields[action], destructive=spec.destructive))
    return "operation", tuple(rows)


def _maintenance_rows():
    from .curation import CURATION_ACTIONS, READ_ONLY_ACTIONS

    rows = [_row("audit", "check_memory", ("categories", "detail", "legacy_sample_limit")),
            _row("collections-store", "check_memory", ("collection",))]
    for action, field, default in (("fix", "dry_run", True), ("backfill-ids", "dry_run", True),
                                    ("reconcile", "dry_run", False)):
        fields = ("categories", "dry_run", "rebuild_embeddings", "rebuild_graph", "detail", "legacy_sample_limit")
        for value in (True, False, None):
            read = value is True if action == "reconcile" else value is not False
            rows.append(_row(action, "check_memory" if read else "repair_memory", fields,
                             constraints=(_literal(field, value, value is default),), destructive=not read))
    for action in ("structured-files", "tag-variants", "collections-store-adopt-local", "collections-store-export"):
        fields = ("collection", "apply", "plan_id", "source_snapshot", "why", "exclude_groups", "acknowledge_skipped")
        for value in (None, False, True):
            write = value is True
            rows.append(_row(action, "repair_memory" if write else "check_memory", fields,
                             constraints=(_literal("apply", value, value is None),), destructive=write))
    fields_by_action = {
        "work-item": ("refs", "paths", "review_ref", "hydration_recheck"),
        "propose": ("plan",), "preview": ("run_id",), "status": ("run_id",),
        "apply": ("run_id", "plan_id", "expected_plan_fingerprint", "why", "vocabulary_ref", "vocabulary_fingerprint"),
        "resume": ("run_id", "plan_id", "vocabulary_ref", "vocabulary_fingerprint"),
        "propose-compensation": ("run_id",),
        "apply-compensation": ("run_id", "plan_id", "expected_plan_fingerprint", "why"),
        "revert": ("review_ref", "expected_plan_fingerprint", "why"),
    }
    if set(fields_by_action) != set(CURATION_ACTIONS):
        raise RuntimeError("curation operation fields drifted from CURATION_ACTIONS")
    for action in CURATION_ACTIONS:
        read = action in READ_ONLY_ACTIONS
        rows.append(_row("curation", "check_memory" if read else "repair_memory",
                         ("curation_action", *fields_by_action[action]),
                         kind=f"curation-{action}", constraints=(_literal("curation_action", action, implied=True),),
                         destructive=not read and action not in {"propose", "propose-compensation"}))
    return "mode", tuple(rows)


def register_operations(command: str, parameters: Iterable[Any] | None = None) -> CommandOperations | None:
    """Read source metadata, completing and retaining signature fields when supplied."""
    if command in OPERATIONS:
        return OPERATIONS[command]
    if command == "schema_memory":
        selector, rows = _schema_rows()
    elif command == "govern_memory":
        selector, rows = _governance_rows()
    elif command == "maintain_memory":
        selector, rows = _maintenance_rows()
    elif command in _REMAINING:
        selector, rows, *_ = _REMAINING[command]
    else:
        return None
    retain = parameters is not None
    parameters = tuple(parameters or ())
    available = frozenset(p.name for p in parameters if p.name != selector and p.name != "response_detail")
    signature_required = frozenset(p.name for p in parameters if p.required and p.name != selector)
    entries = []
    for action, tool, fields, required, kind, constraints, destructive in rows:
        fields = (available if retain else frozenset(required) | frozenset(item.path[0] for item in constraints)) if fields is None else frozenset(fields)
        # Singletons retain their exact required fields from the original signature.
        required = signature_required if selector is None else frozenset(required)
        effect = _TOOL_HELP[tool][0]
        entries.append(_entry(action, tool, fields, required, effect=effect,
                              constraints=constraints, public_kind=kind, destructive=destructive))
    specs = []
    for name in dict.fromkeys(entry.rendering for entry in entries):
        variants = [entry for entry in entries if entry.rendering == name]
        _, title, description = _TOOL_HELP[name]
        tool_arguments = ()
        if any(p.name == "response_detail" for p in parameters) and variants[0].effect == "write":
            tool_arguments += ("response_detail",)
        aliases = (("unit_kind", "kind"),) if command == "observe_memory" else (("file_kind", "kind"),) if command == "manage_memory_file" else ()
        specs.append(RenderingSpec(name, variants[0].effect, title,
                                   description + " Select one operation.kind.", tool_arguments, aliases))
    capture_write = _REMAINING.get(command, ())[2:] == (True,)
    result = CommandOperations(command, selector, tuple(entries), tuple(specs), capture_write)
    if retain:
        _OPERATION_TABLE[command] = result
    return result


#: Every converted command, populated once from source-owned metadata at registry construction.
_OPERATION_TABLE = {operations.command: operations for operations in (RECORDS, PLANS)}
OPERATIONS: Mapping[str, CommandOperations] = MappingProxyType(_OPERATION_TABLE)


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
    variants = [(rendering, variant) for rendering in renderings for variant in rendering.variants]
    expected_actions = set(actions)
    actual_actions = {variant.action for _, variant in variants}
    problems += [f"{(action, None)} has no variant" for action in sorted(expected_actions - actual_actions)]
    problems += [f"{(action, None)} is not an operation" for action in sorted(actual_actions - expected_actions)]
    count: dict[tuple, int] = {}
    for rendering, variant in variants:
        identity = tuple((item.path, type(item.value), item.value, item.allow_omitted, item.implied)
                         for item in variant.constraints)
        key = (variant.action, identity)
        count[key] = count.get(key, 0) + 1
        label = (variant.action, variant.constraints[0].value if len(identity) == 1 else identity or None)
        if variant.effect != rendering.effect:
            problems.append(f"{label} is a {variant.effect} variant in {rendering.name}")
        if is_read_only(rendering, variant) != rendering.read_only:
            problems.append(f"{label} in {rendering.name} classifies against the tool's effect")
        # Omission selects a variant only when the grouped default has the same effect.
        optional = [item for item in variant.constraints if item.allow_omitted and not item.implied]
        for size in range(1, len(optional) + 1):
            for omitted in combinations(optional, size):
                without = replace(
                    variant, constraints=tuple(item for item in variant.constraints if item not in omitted)
                )
                if is_read_only(rendering, without) != rendering.read_only:
                    problems.append(
                        f"{label} in {rendering.name} classifies against the tool's effect when omitted"
                    )
    for (action, identity), n in count.items():
        if n > 1:
            label = (action, identity[0][2] if len(identity) == 1 else identity or None)
            problems.append(f"{label} has {n} variants")
    for index, (_, left) in enumerate(variants):
        for _, right in variants[index + 1:]:
            if left.action != right.action:
                continue
            left_by_path = {item.path: item for item in left.constraints}
            disjoint = any(
                item.path in left_by_path
                and (type(item.value) is not type(left_by_path[item.path].value)
                     or item.value != left_by_path[item.path].value)
                and not (item.allow_omitted and left_by_path[item.path].allow_omitted)
                for item in right.constraints
            )
            disjoint = disjoint or any(item.value is None and item.path[0] in right.required
                                       for item in left.constraints) or any(
                item.value is None and item.path[0] in left.required for item in right.constraints
            )
            if not disjoint:
                problems.append(f"{left.action} has overlapping variants")
    for action, registered in modes.items():
        values = set(registered)
        carried = [variant for _, variant in variants if variant.action == action]
        paths = {item.path for variant in carried for item in variant.constraints
                 if isinstance(item.value, str) and item.value in values}
        if len(paths) != 1:
            problems.append(f"{action} has no single registry mode constraint")
            continue
        path = next(iter(paths))
        covered = set()
        for variant in carried:
            constraint = next((item for item in variant.constraints if item.path == path), None)
            if constraint is None or constraint.allow_omitted:
                problems.append(f"{action} disagrees with its registry mode constraint")
            elif constraint.value not in values:
                problems.append(f"{(action, constraint.value)} is not an operation")
            else:
                covered.add(constraint.value)
        problems += [f"{(action, value)} has no variant" for value in sorted(values - covered)]
    if problems:
        raise RuntimeError(
            f"operation entries for {command} do not cover its selector: " + "; ".join(problems)
        )


# --- Spelling a call --------------------------------------------------------------------


def _renderings_of(command: str) -> tuple[Rendering, ...]:
    operations = OPERATIONS.get(command) or register_operations(command)
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
    selector = renderings[0].selector
    kind = arguments.get(selector) if selector else None
    fields = {key: value for key, value in arguments.items() if key != selector}
    for rendering in renderings:
        variant = next((entry for entry in rendering.variants
                        if (selector is None or entry.action == kind)
                        and all(replace(item, implied=False).matches(fields)
                                for item in entry.constraints)), None)
        if variant is None:
            continue
        # The tool's own arguments sit beside `operation`, where the server reads them.
        tool_arguments = rendering.take_tool_arguments(fields)
        return rendering.name, {"operation": {"kind": variant.kind, **rendering.wire_fields(fields)}, **tool_arguments}
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
    by_tool = {r.name: list(dict.fromkeys(v.kind for action in actions for v in r.variants
                                        if v.action == action)) for r in renderings}
    missing = [a for a in actions if not any(v.action == a for r in renderings for v in r.variants)]
    if missing:
        raise ValueError(f"no rendering of {command} carries {', '.join(missing)}")
    return {"tools": by_tool, "selector": OPERATION_KIND}


def split_operation(body: Mapping[str, Any]) -> tuple[Any, dict[str, Any]] | None:
    """The operation kind and its fields from a rendering call's arguments, or None.

    `body` holds the arguments without the transport keys and the tool's own extra
    arguments. The call must carry one `operation` object and nothing else.
    """
    operation = body.get("operation")
    if set(body) != {"operation"} or not isinstance(operation, Mapping):
        return None
    return operation.get("kind"), {k: v for k, v in operation.items() if k != "kind"}


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
            if not any(entry.matches(kind, arguments) for entry in operations.entries):
                return None
            return command, dict(arguments)
        for rendering in _renderings_of(command):
            if rendering.name != tool:
                continue
            body = dict(arguments)
            tool_arguments = rendering.take_tool_arguments(body)
            parsed = split_operation(body)
            if parsed is None:
                return None
            kind, fields = parsed
            try:
                fields = rendering.leaf_fields(fields)
            except ValueError:
                continue
            if rendering.variant_for(kind, fields) is None:
                continue
            variant = rendering.variant_for(kind, fields)
            return command, {**rendering.selected_arguments(variant, fields), **tool_arguments}
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
        if not operations.capture_write:
            continue
        reads = {entry.action for entry in operations.entries if entry.effect == "read"}
        for entry in operations.entries:
            if entry.effect != "write":
                continue
            selectors.add(f"{entry.rendering}:{entry.kind}")
            if entry.action not in reads:
                selectors.add(f"{command}:{entry.action}")
    return tuple(sorted(selectors))
