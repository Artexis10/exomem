"""Governed, agent-authored curation plans and durable run evidence.

The active agent owns every semantic choice.  This module accepts only a
closed typed plan, seals deterministic bindings, and stores enough canonical
evidence to reconstruct progress without a machine-local coordinator.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from .kbdir import kb_dirname
from .vault import (
    BatchWriteError,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    batch_atomic_write,
    read_guarded_text,
)
from .vault import content_hash as text_content_hash

CURATION_ACTIONS: Final[tuple[str, ...]] = (
    "work-item",
    "propose",
    "preview",
    "status",
    "apply",
    "resume",
    "propose-compensation",
    "apply-compensation",
)
READ_ONLY_ACTIONS: Final[frozenset[str]] = frozenset({"work-item", "preview", "status"})
STEP_KINDS: Final[tuple[str, ...]] = (
    "create-note",
    "create-entity",
    "accept-relation",
    "edit",
    "supersede",
    "move",
    "delete",
    "recover",
)
MAX_STEPS: Final[int] = 64
MAX_PLAN_BYTES: Final[int] = 256 * 1024
MAX_TITLE_CHARS: Final[int] = 500
MAX_STEP_ID_CHARS: Final[int] = 80
MAX_WHY_CHARS: Final[int] = 500
MAX_WORK_ITEM_PAGES: Final[int] = 12
MAX_WORK_ITEM_CHARS: Final[int] = 24_000

_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX24 = re.compile(r"^[0-9a-f]{24}$")
_PLAN_FIELDS = frozenset({"version", "title", "steps"})
_STEP_FIELDS = frozenset({"step_id", "kind", "args"})
_SEALED_FIELDS = frozenset({"binding_manifest", "registry_ids", "plan_type", "forward_plan_id"})

_CREATE_NOTE_FIELDS = frozenset(
    {
        "content",
        "title",
        "slug",
        "note_type",
        "project",
        "projects",
        "sources",
        "tags",
        "status",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
        "project_category",
        "relation_disposition",
        "relation_review_reason",
    }
)
_CREATE_ENTITY_FIELDS = frozenset(
    {
        "entity_type",
        "name",
        "slug",
        "summary",
        "why_in_kb",
        "tags",
        "connections",
        "affiliation",
        "relationship",
        "domain",
        "language",
        "repo",
        "license",
        "used_in",
        "decided",
        "project",
        "decision_status",
    }
)
_ACCEPT_RELATION_FIELDS = frozenset({"ref", "expected_hash", "why", "expected_fingerprint"})
_EDIT_FIELDS = frozenset({"path", "why", "operation"})
_SUPERSEDE_FIELDS = frozenset(
    {
        "old_path",
        "content",
        "title",
        "slug",
        "note_type",
        "reason",
        "project",
        "projects",
        "sources",
        "tags",
        "status",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
        "project_category",
        "relation_disposition",
        "relation_review_reason",
    }
)
_MOVE_FIELDS = frozenset(
    {"old_path", "new_path", "update_wikilinks", "allow_curated", "promotion_reason"}
)
_DELETE_FIELDS = frozenset(
    {
        "path",
        "confirm",
        "recursive",
        "force_orphan",
        "force_superseded",
        "allow_curated",
        "expected_dead_inbound",
    }
)
_RECOVER_FIELDS = frozenset({"trash_path", "restore_path", "allow_curated"})
_ARG_FIELDS = {
    "create-note": _CREATE_NOTE_FIELDS,
    "create-entity": _CREATE_ENTITY_FIELDS,
    "accept-relation": _ACCEPT_RELATION_FIELDS,
    "edit": _EDIT_FIELDS,
    "supersede": _SUPERSEDE_FIELDS,
    "move": _MOVE_FIELDS,
    "delete": _DELETE_FIELDS,
    "recover": _RECOVER_FIELDS,
}
_REQUIRED_FIELDS = {
    "create-note": frozenset({"content", "title"}),
    "create-entity": frozenset({"entity_type", "name", "summary"}),
    "accept-relation": _ACCEPT_RELATION_FIELDS,
    "edit": _EDIT_FIELDS,
    "supersede": frozenset({"old_path", "content", "title"}),
    "move": frozenset({"old_path", "new_path"}),
    "delete": frozenset({"path", "confirm"}),
    "recover": frozenset({"trash_path"}),
}


@dataclass
class CurationError(ValueError):
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


def _error(code: str, reason: str) -> CurationError:
    return CurationError(code, reason)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise _error("INVALID_CURATION_PLAN", "plan must be finite canonical JSON") from error


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def plan_id(plan: Mapping[str, Any]) -> str:
    return _digest(plan)


def run_id(plan: Mapping[str, Any], *, today: dt.date | None = None) -> str:
    day = today or dt.date.today()
    return f"cur-{day:%Y%m%d}-{plan_id(plan)[:12]}"


def operation_id(plan_identity: str, ordinal: int, step_id: str) -> str:
    if not _HEX64.fullmatch(str(plan_identity)) or type(ordinal) is not int or ordinal < 0:
        raise _error("INVALID_OPERATION_ID", "operation identity inputs are invalid")
    return hashlib.sha256(f"{plan_identity}{ordinal}{step_id}".encode()).hexdigest()


def _require_relocation_history_capability(_vault_root: Path) -> NoReturn:
    """Refuse relocation until held-fs proves durable namespace lineage.

    Stable file identity and post-crash placement hashes cannot distinguish a
    completed rename from a crash followed by an external inverse rename.  The
    current held-fs ABI has no epoch-scoped monotonic namespace generation for
    both retained parents, so curation must not publish or authorize a rename
    transition on this base.
    """
    raise _error(
        "CURATION_RENAME_HISTORY_UNPROVABLE",
        "curation relocation requires durable retained-parent namespace lineage",
    )


def _require_plan_relocation_history(
    vault_root: Path, plan: Mapping[str, Any]
) -> None:
    if any(step.get("kind") in {"move", "delete", "recover"} for step in plan["steps"]):
        _require_relocation_history_capability(vault_root)


def plan_fingerprint(
    plan: Mapping[str, Any],
    binding_manifest: Iterable[Mapping[str, Any]],
    registry_ids: Mapping[str, str],
) -> str:
    return _digest(
        {
            "plan": plan,
            "binding_manifest": list(binding_manifest),
            "registry_ids": dict(registry_ids),
        }
    )


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error("CURATION_UNKNOWN_FIELD", f"{where} has unknown fields: {unknown}")


def _require_string(value: Any, field: str, *, max_chars: int = MAX_PLAN_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("INVALID_CURATION_PLAN", f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        if field.endswith("why"):
            raise _error("INVALID_CURATION_PLAN", f"{field} must be one line")
    if len(value) > max_chars:
        raise _error("CURATION_PLAN_TOO_LARGE", f"{field} exceeds its size cap")
    return value


def normalize_target_path(path: Any, *, field: str = "path", allow_trash: bool = False) -> str:
    raw = _require_string(path, field).replace("\\", "/").strip().lstrip("/")
    if (
        raw.endswith("/")
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise _error("INVALID_CURATION_PATH", f"{field} is not a confined vault path")
    prefix = f"{kb_dirname()}/"
    if not raw.startswith(prefix):
        raise _error("CURATION_TARGET_PROTECTED", f"{field} must target governed knowledge")
    relative = raw[len(prefix) :]
    first = relative.split("/", 1)[0].casefold()
    compact = relative.casefold().replace("_", "-")
    protected = {
        "planning",
        "records",
        "workflow-contract",
        "workflow-contracts",
        "_schema",
        "_governance",
        "_adoption",
        "sources",
        "evidence",
    }
    if first in protected or compact.startswith("workflow-contract/"):
        raise _error("CURATION_TARGET_PROTECTED", f"{field} belongs to another typed owner")
    if first == "_trash" and not allow_trash:
        raise _error("CURATION_TARGET_PROTECTED", f"{field} targets trash internals")
    return raw


def _validate_args(kind: str, raw: Any, ordinal: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].args must be an object")
    args = dict(raw)
    _unknown_fields(args, _ARG_FIELDS[kind], f"steps[{ordinal}].args")
    missing = sorted(name for name in _REQUIRED_FIELDS[kind] if name not in args)
    if missing:
        raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].args misses {missing}")

    if kind == "create-note":
        _require_string(
            args.get("title"), f"steps[{ordinal}].args.title", max_chars=MAX_TITLE_CHARS
        )
        _require_string(args.get("content"), f"steps[{ordinal}].args.content")
    elif kind == "create-entity":
        for field in ("entity_type", "name", "summary"):
            _require_string(args.get(field), f"steps[{ordinal}].args.{field}")
    elif kind == "accept-relation":
        for field in _ACCEPT_RELATION_FIELDS:
            _require_string(args.get(field), f"steps[{ordinal}].args.{field}")
        if not _HEX64.fullmatch(args["expected_hash"]) or not _HEX24.fullmatch(
            args["expected_fingerprint"]
        ):
            raise _error("INVALID_CURATION_PLAN", "relation bindings are invalid")
    elif kind == "edit":
        args["path"] = normalize_target_path(args.get("path"), field="path")
        _require_string(args.get("why"), f"steps[{ordinal}].args.why", max_chars=MAX_WHY_CHARS)
        if not isinstance(args.get("operation"), Mapping):
            raise _error("INVALID_CURATION_PLAN", "edit operation must be a closed object")
        from .edit_operations import EDIT_OPERATION_ADAPTER

        try:
            operation = EDIT_OPERATION_ADAPTER.validate_python(dict(args["operation"]))
        except Exception as error:
            raise _error("INVALID_CURATION_PLAN", "edit operation schema is invalid") from error
        normalized_operation = operation.model_dump(mode="python", exclude_none=True)
        expected_hash = normalized_operation.get("expected_hash")
        if not isinstance(expected_hash, str) or not _HEX64.fullmatch(expected_hash):
            raise _error("INVALID_CURATION_PLAN", "edit requires the reviewed expected_hash")
        args["operation"] = normalized_operation
    elif kind == "supersede":
        args["old_path"] = normalize_target_path(args.get("old_path"), field="old_path")
        _require_string(args.get("content"), f"steps[{ordinal}].args.content")
        _require_string(
            args.get("title"), f"steps[{ordinal}].args.title", max_chars=MAX_TITLE_CHARS
        )
    elif kind == "move":
        args["old_path"] = normalize_target_path(args.get("old_path"), field="old_path")
        args["new_path"] = normalize_target_path(args.get("new_path"), field="new_path")
    elif kind == "delete":
        args["path"] = normalize_target_path(args.get("path"), field="path")
        if args.get("confirm") is not True:
            raise _error("INVALID_CURATION_PLAN", "delete requires canonical confirm=true")
    elif kind == "recover":
        trash_path = normalize_target_path(
            args.get("trash_path"), field="trash_path", allow_trash=True
        )
        if not trash_path.casefold().startswith(f"{kb_dirname().casefold()}/_trash/"):
            raise _error(
                "INVALID_CURATION_PLAN", "recover requires an exact governed trash identity"
            )
        args["trash_path"] = trash_path
        if args.get("restore_path") is not None:
            args["restore_path"] = normalize_target_path(args["restore_path"], field="restore_path")
    return json.loads(canonical_json(args))


def validate_forward_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("INVALID_CURATION_PLAN", "plan must be an object")
    value = dict(raw)
    _unknown_fields(value, _PLAN_FIELDS, "plan")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise _error("INVALID_CURATION_PLAN", "plan version must be the integer 1")
    title = _require_string(value.get("title"), "plan.title", max_chars=MAX_TITLE_CHARS)
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _error("INVALID_CURATION_PLAN", "plan.steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise _error("CURATION_PLAN_TOO_LARGE", f"plan has more than {MAX_STEPS} steps")
    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, raw_step in enumerate(steps):
        if not isinstance(raw_step, Mapping):
            raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}] must be an object")
        step = dict(raw_step)
        _unknown_fields(step, _STEP_FIELDS, f"steps[{ordinal}]")
        step_identity = _require_string(
            step.get("step_id"), f"steps[{ordinal}].step_id", max_chars=MAX_STEP_ID_CHARS
        )
        if not _STEP_ID.fullmatch(step_identity):
            raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].step_id is invalid")
        if step_identity in seen:
            raise _error("DUPLICATE_STEP_ID", f"step id {step_identity!r} is duplicated")
        seen.add(step_identity)
        kind = step.get("kind")
        if kind not in STEP_KINDS:
            raise _error("INVALID_STEP_KIND", f"step kind must be one of {list(STEP_KINDS)}")
        normalized_steps.append(
            {
                "step_id": step_identity,
                "kind": kind,
                "args": _validate_args(kind, step.get("args"), ordinal),
            }
        )
    normalized = {"version": 1, "title": title, "steps": normalized_steps}
    if len(canonical_json_bytes(normalized)) > MAX_PLAN_BYTES:
        raise _error("CURATION_PLAN_TOO_LARGE", f"canonical plan exceeds {MAX_PLAN_BYTES} bytes")
    return normalized


def _validate_sealed_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("CURATION_PLAN_CORRUPT", "stored plan is not an object")
    base = {key: value for key, value in raw.items() if key in _PLAN_FIELDS}
    normalized = validate_forward_plan(base)
    _unknown_fields(dict(raw), _PLAN_FIELDS | _SEALED_FIELDS, "stored plan")
    manifest = raw.get("binding_manifest", [])
    registries = raw.get("registry_ids", {})
    if not isinstance(manifest, list) or not all(isinstance(item, Mapping) for item in manifest):
        raise _error("CURATION_PLAN_CORRUPT", "stored binding manifest is invalid")
    if not isinstance(registries, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in registries.items()
    ):
        raise _error("CURATION_PLAN_CORRUPT", "stored registry identities are invalid")
    return {
        **normalized,
        "binding_manifest": json.loads(canonical_json(manifest)),
        "registry_ids": dict(registries),
        **({"plan_type": raw["plan_type"]} if isinstance(raw.get("plan_type"), str) else {}),
        **(
            {"forward_plan_id": raw["forward_plan_id"]}
            if isinstance(raw.get("forward_plan_id"), str)
            else {}
        ),
    }


def _optional_model_call(*_args: Any, **_kwargs: Any) -> None:
    """Non-capability sentinel: curation never calls a reasoning model."""
    return None


def registry_identities(vault_root: Path) -> dict[str, str]:
    """Return deterministic identities for registries that affect executability."""
    from . import entity_types, relation_registry

    entities = entity_types.load_entity_types(vault_root)
    relations = relation_registry.load_registry(vault_root)
    schema_root = Path(vault_root) / kb_dirname() / "_Schema"
    schema_rows: list[dict[str, str]] = []
    schema_candidates: list[Path] = [schema_root / "semantic-language-registry.yaml"]
    contracts_root = schema_root / "contracts"
    if contracts_root.is_dir() and not contracts_root.is_symlink():
        schema_candidates.extend(sorted(contracts_root.glob("*.yaml")))
    if not schema_root.is_symlink():
        for path in schema_candidates:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(vault_root).as_posix()
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise _error(
                    "CURATION_REGISTRY_UNREADABLE", "schema registry changed while read"
                ) from error
            schema_rows.append({"path": relative, "sha256": digest})
    return {
        "entities": _digest(
            {"core_version": entities.core_version, "extension_hash": entities.extension_hash}
        ),
        "relations": _digest(
            {"core_version": relations.core_version, "extension_hash": relations.extension_hash}
        ),
        "schemas": _digest(schema_rows),
    }


def step_schemas() -> dict[str, dict[str, list[str]]]:
    return {
        kind: {
            "required": sorted(_REQUIRED_FIELDS[kind]),
            "allowed": sorted(_ARG_FIELDS[kind]),
        }
        for kind in STEP_KINDS
    }


def _guarded_text(vault_root: Path, path: str, *, stale: bool) -> tuple[str, str]:
    """Read a lexical vault path through the shared no-follow descriptor guard."""
    try:
        PathGuard.capture(vault_root, path, leaf_policy="stable")
        source, _guard = read_guarded_text(vault_root, vault_root / path)
    except PathGuardError as error:
        if error.code in {"PATH_GUARD_INVALID", "PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}:
            raise _error("CURATION_PATH_UNSAFE", f"target {path!r} crosses an unsafe path") from error
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_UNREADABLE"
        raise _error(code, f"target {path!r} is unavailable") from error
    except (FileNotFoundError, OSError, UnicodeDecodeError) as error:
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_NOT_FOUND"
        raise _error(code, f"target {path!r} is unavailable") from error
    return source, text_content_hash(source)


def _guarded_absent(vault_root: Path, path: str) -> bool:
    try:
        PathGuard.capture(vault_root, path, leaf_policy="absent")
        return True
    except PathGuardError as error:
        if error.code in {"PATH_GUARD_INVALID", "PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}:
            raise _error("CURATION_PATH_UNSAFE", f"target {path!r} crosses an unsafe path") from error
        return False


def _read_page(vault_root: Path, raw: str) -> tuple[str, str]:
    from . import memory_refs

    try:
        path = memory_refs.resolve_identifier_read_only(vault_root, raw)
    except memory_refs.ReferenceError as error:
        raise _error(error.code, error.reason) from error
    path = normalize_target_path(path, field="work_item.path")
    source, _digest_value = _guarded_text(Path(vault_root), path, stale=False)
    return path, source


def work_item(
    vault_root: Path,
    *,
    refs: list[str] | None = None,
    paths: list[str] | None = None,
    max_chars_per_page: int = 6_000,
) -> dict[str, Any]:
    """Assemble only explicitly named recorded context; never select or rank it."""
    selected = [*(refs or []), *(paths or [])]
    if not selected:
        raise _error("CURATION_WORK_ITEM_EMPTY", "work-item requires explicit refs or paths")
    if len(selected) > MAX_WORK_ITEM_PAGES:
        raise _error(
            "CURATION_WORK_ITEM_TOO_LARGE",
            f"work-item may contain at most {MAX_WORK_ITEM_PAGES} explicit pages",
        )
    if type(max_chars_per_page) is not int or not 1 <= max_chars_per_page <= MAX_WORK_ITEM_CHARS:
        raise _error("CURATION_WORK_ITEM_TOO_LARGE", "per-page character cap is invalid")
    pages: list[dict[str, Any]] = []
    truncated: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        path, source = _read_page(Path(vault_root), raw)
        if path in seen:
            continue
        seen.add(path)
        was_truncated = len(source) > max_chars_per_page
        if was_truncated:
            truncated.append(path)
        pages.append(
            {
                "path": path,
                "content": source[:max_chars_per_page],
                "content_hash": text_content_hash(source),
                "chars": len(source),
                "truncated": was_truncated,
            }
        )
    return {
        "action": "work-item",
        "mutated": False,
        "pages": pages,
        "truncation": {"pages": truncated, "disclosed": bool(truncated)},
        "registry_ids": registry_identities(Path(vault_root)),
        "step_schemas": step_schemas(),
    }


def _read_target(vault_root: Path, path: str) -> tuple[str, str]:
    return _guarded_text(Path(vault_root), path, stale=True)


def _prepare_step(vault_root: Path, step: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    """Run the existing leaf's read-only preparation or exact structural guards."""
    from . import (
        commands,
        relation_queue,
    )
    from . import delete_file as delete_module
    from . import (
        link as link_module,
    )
    from . import move_file as move_module
    from . import (
        note as note_module,
    )
    from . import (
        recover_from_trash as recover_module,
    )

    kind = str(step["kind"])
    args = dict(step["args"])
    prepared: dict[str, Any] = {}
    preimage: str | None = None
    before_hash: str | None = None
    expected_absent = False
    effect_before: list[dict[str, Any]] = []
    effect_after: list[dict[str, Any]] = []

    try:
        if kind == "create-note":
            review = {
                key: args.pop(key)
                for key in ("relation_disposition", "relation_review_reason")
                if key in args
            }
            validation = commands.op_remember(vault_root, validate_only=True, **args)
            path = normalize_target_path(validation["destination"], field="destination")
            if not _guarded_absent(vault_root, path):
                raise _error("CURATION_BINDING_STALE", f"create destination {path!r} exists")
            expected_absent = True
            prepared = {
                "draft_id": validation["draft_id"],
                "draft_hash": validation["draft_hash"],
                "draft_token": validation["draft_token"],
                "destination": validation["destination"],
                **review,
            }
            if review:
                prepared["relation_review_hash"] = validation["draft_hash"]
            exact_preflight = note_module.note(
                vault_root,
                validate_only=True,
                draft_id=validation["draft_id"],
                draft_hash=validation["draft_hash"],
                draft_token=validation["draft_token"],
                note_type=args.get("note_type", "insight"),
                **{key: value for key, value in args.items() if key != "note_type"},
            )
            after_hash = text_content_hash(exact_preflight.source)
            effect_before = [{"path": path, "absent": True}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == "edit":
            path = args["path"]
            preimage, before_hash = _read_target(vault_root, path)
            from .vault import parse_frontmatter

            frontmatter, _body, _raw = parse_frontmatter(preimage, strict=True)
            if str(frontmatter.get("type") or "") not in {
                "research-note",
                "insight",
                "failure",
                "pattern",
                "experiment",
                "production-log",
            }:
                raise _error(
                    "CURATION_COMPENSATION_UNAVAILABLE",
                    "edit target has no history-preserving compensation leaf",
                )
            expected_hash = args["operation"].get("expected_hash")
            if expected_hash != before_hash:
                raise _error("CURATION_BINDING_STALE", f"reviewed hash for {path!r} is stale")
            operation = {**args["operation"], "validate_only": True}
            validation = commands.op_edit_memory(
                vault_root, path=path, why=args["why"], operation=operation
            )
            semantic = dict(validation.get("semantic") or {})
            prepared = {
                "transition_token": semantic.get("transition_token"),
                "relation_review_hash": semantic.get("relation_review_hash"),
            }
            after_hash = semantic.get("after_hash")
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == "supersede":
            path = args["old_path"]
            preimage, before_hash = _read_target(vault_root, path)
            review = {
                key: args.pop(key)
                for key in ("relation_disposition", "relation_review_reason")
                if key in args
            }
            validation = commands.op_replace_memory(vault_root, validate_only=True, **args)
            prepared = {
                "draft_id": validation["draft_id"],
                "draft_hash": validation["draft_hash"],
                "draft_token": validation["draft_token"],
                "destination": validation["destination"],
                **review,
            }
            if review:
                prepared["relation_review_hash"] = validation["draft_hash"]
            after_hash = validation.get("content_fingerprint") or validation.get("draft_hash")
            destination = normalize_target_path(validation["destination"], field="destination")
            if not _guarded_absent(vault_root, destination):
                raise _error(
                    "CURATION_BINDING_STALE", f"supersession destination {destination!r} exists"
                )
            effect_before = [
                {"path": path, "content_hash": before_hash},
                {"path": destination, "absent": True},
            ]
            effect_after = [
                {"path": path, "exists": True},
                {"path": destination, "exists": True},
            ]
        elif kind == "create-entity":
            validation = link_module.link(vault_root, validate_only=True, **args)
            path = normalize_target_path(validation.path, field="destination")
            if not _guarded_absent(vault_root, path):
                raise _error("CURATION_BINDING_STALE", f"entity destination {path!r} exists")
            expected_absent = True
            prepared = {"destination": path}
            after_hash = None
            effect_before = [{"path": path, "absent": True}]
            effect_after = [{"path": path, "exists": True}]
        elif kind == "accept-relation":
            resolved = relation_queue.resolve_candidate(vault_root, args["ref"])
            if resolved.fingerprint != args["expected_fingerprint"]:
                raise _error("CURATION_BINDING_STALE", "relation fingerprint is stale")
            path = normalize_target_path(str(resolved.candidate.get("from") or ""), field="path")
            preimage, before_hash = _read_target(vault_root, path)
            if before_hash != args["expected_hash"]:
                raise _error("CURATION_BINDING_STALE", "relation target hash is stale")
            prepared = {"candidate": dict(resolved.candidate)}
            after_hash = None
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "exists": True}]
        elif kind == "move":
            validation = move_module.move_file(vault_root, validate_only=True, **args)
            if not isinstance(validation, move_module.MoveFileValidation):
                raise _error("CURATION_STEP_INVALID", "move leaf did not return a validation")
            if not validation.atomic_supported:
                raise _error(
                    "CURATION_ATOMICITY_UNAVAILABLE",
                    "curation move requires one unpaired Markdown page",
                )
            path = normalize_target_path(validation.result.old_path, field="old_path")
            preimage, before_hash = _read_target(vault_root, path)
            destination = normalize_target_path(validation.result.new_path, field="new_path")
            expected_absent = True
            prepared = {
                "destination": destination,
                "files_touched": list(validation.result.files_touched),
                "rename_after": list(validation.rename_after),
            }
            after_hash = before_hash
            effect_before = [dict(item) for item in validation.before]
            effect_after = [dict(item) for item in validation.after]
        elif kind == "delete":
            path = args["path"]
            preimage, before_hash = _read_target(vault_root, path)
            validation = delete_module.delete_file(vault_root, validate_only=True, **args)
            if not isinstance(validation, delete_module.DeleteFileValidation):
                raise _error("CURATION_STEP_INVALID", "delete leaf did not return a validation")
            after_hash = None
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "absent": True}]
        else:
            assert kind == "recover"
            path = args["trash_path"]
            preimage, before_hash = _read_target(vault_root, path)
            validation = recover_module.recover_from_trash(
                vault_root,
                trash_path=path,
                restore_path=args.get("restore_path"),
                allow_curated=bool(args.get("allow_curated", False)),
                validate_only=True,
            )
            destination = normalize_target_path(validation.restored_path, field="restore_path")
            if not _guarded_absent(vault_root, destination):
                raise _error(
                    "CURATION_BINDING_STALE",
                    f"recovery destination {destination!r} exists",
                )
            expected_absent = True
            prepared = {"destination": destination}
            after_hash = before_hash
            effect_before = [
                {"path": path, "content_hash": before_hash},
                {"path": destination, "absent": True},
            ]
            effect_after = [
                {"path": path, "absent": True},
                {"path": destination, "content_hash": before_hash},
            ]
    except CurationError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        reason = getattr(error, "reason", None)
        message = str(reason or error)
        if not isinstance(code, str):
            code = str(error).split(":", 1)[0] if ":" in str(error) else "CURATION_STEP_INVALID"
        raise _error(code, message) from error

    return {
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "kind": kind,
        "path": path,
        "before_hash": before_hash,
        "expected_absent": expected_absent,
        "prepared": prepared,
        "postcondition": {"path": prepared.get("destination", path), "content_hash": after_hash},
        "effect_before": effect_before,
        "effect_after": effect_after,
        "preimage": preimage,
    }


def propose(vault_root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(vault_root)
    validated = validate_forward_plan(plan)
    manifest = [
        _prepare_step(root, step, ordinal) for ordinal, step in enumerate(validated["steps"])
    ]
    _require_plan_relocation_history(root, validated)
    registries = registry_identities(root)
    return CurationStore(root).create_forward(
        validated, binding_manifest=manifest, registry_ids=registries
    )


def _binding_blockers(
    vault_root: Path, plan: Mapping[str, Any], current_registries: Mapping[str, str]
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for name, expected in plan["registry_ids"].items():
        if current_registries.get(name) != expected:
            blockers.append(
                {
                    "code": "CURATION_REGISTRY_CHANGED",
                    "registry": name,
                    "reason": "registry identity changed",
                }
            )
    for item in plan["binding_manifest"]:
        manifest = item.get("effect_before")
        if isinstance(manifest, list) and manifest:
            for expected_item in manifest:
                path = str(expected_item.get("path") or "")
                if expected_item.get("absent") is True:
                    if _guarded_absent(vault_root, path):
                        continue
                    blockers.append(
                        {
                            "code": "CURATION_BINDING_STALE",
                            "path": path,
                            "reason": "expected absence changed",
                        }
                    )
                    continue
                expected = expected_item.get("content_hash")
                try:
                    _source, actual = _read_target(vault_root, path)
                except CurationError as error:
                    if error.code == "CURATION_PATH_UNSAFE":
                        raise
                    actual = None
                if actual != expected:
                    blockers.append(
                        {
                            "code": "CURATION_BINDING_STALE",
                            "path": path,
                            "reason": "content hash changed",
                        }
                    )
            continue
        path = str(item["path"])
        if item.get("expected_absent"):
            destination = str(item.get("prepared", {}).get("destination") or path)
            if not _guarded_absent(vault_root, destination):
                blockers.append(
                    {
                        "code": "CURATION_BINDING_STALE",
                        "path": destination,
                        "reason": "expected absence changed",
                    }
                )
            continue
        expected = item.get("before_hash")
        if expected is None:
            continue
        try:
            _source, actual = _read_target(vault_root, path)
        except CurationError as error:
            if error.code == "CURATION_PATH_UNSAFE":
                raise
            actual = None
        if actual != expected:
            blockers.append(
                {"code": "CURATION_BINDING_STALE", "path": path, "reason": "content hash changed"}
            )
    return blockers


def preview(vault_root: Path, *, run_id: str) -> dict[str, Any]:
    root = Path(vault_root)
    store = CurationStore(root)
    plan = store.load_plan(run_id)
    identity, fingerprint = store.identities(run_id)
    blockers = _binding_blockers(root, plan, registry_identities(root))
    return {
        "action": "preview",
        "mutated": False,
        "run_id": run_id,
        "plan_id": identity,
        "plan_fingerprint": fingerprint,
        "title": plan["title"],
        "actions": plan["steps"],
        "binding_manifest": plan["binding_manifest"],
        "binding_health": "stale" if blockers else "current",
        "blockers": blockers,
        "compensation_classes": [compensation_kind(step["kind"]) for step in plan["steps"]],
    }


def compensation_kind(kind: str) -> str:
    return {
        "create-note": "delete",
        "create-entity": "delete",
        "accept-relation": "supersede",
        "edit": "supersede",
        "supersede": "supersede",
        "move": "move",
        "delete": "recover",
        "recover": "delete",
    }[kind]


class CurationFault(RuntimeError):
    """Test-only deterministic process-cut signal."""


def _fault_barrier(_name: str) -> None:
    return None


def _witness_basis(
    *,
    run_identity: str,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    operation_identity: str,
    binding: Mapping[str, Any],
    parent_compensation_plan_id: str | None = None,
) -> dict[str, Any]:
    sealed_before = binding.get("effect_before")
    before: list[dict[str, Any]] = (
        [dict(item) for item in sealed_before]
        if isinstance(sealed_before, list)
        else []
    )
    if not before and binding.get("before_hash") is not None:
        before.append({"path": binding["path"], "content_hash": binding["before_hash"]})
    postcondition = dict(binding.get("postcondition") or {})
    after_path = str(postcondition.get("path") or binding["path"])
    after_hash = postcondition.get("content_hash")
    sealed_after = binding.get("effect_after")
    exact_after = bool(
        isinstance(sealed_after, list)
        and sealed_after
        and not (step["kind"] == "delete" and len(sealed_after) == 1)
        and all(
            isinstance(item, Mapping)
            and (
                (set(item) == {"path", "absent"} and item.get("absent") is True)
                or (
                    set(item) == {"path", "content_hash"}
                    and _HEX64.fullmatch(str(item.get("content_hash") or "")) is not None
                )
            )
            for item in sealed_after
        )
    )
    if exact_after:
        after = [dict(item) for item in sealed_after]
    elif step["kind"] == "delete":
        after = [{"path": binding["path"], "absent": True}]
    elif after_hash is not None:
        after = [{"path": after_path, "content_hash": after_hash}]
    else:
        after = [{"path": after_path, "exists": True}]
    leaf_identity = {
        "create-note": "remember",
        "create-entity": "connect_memory:create-entity",
        "accept-relation": "connect_memory:accept-relation",
        "edit": "edit_memory",
        "supersede": "replace_memory",
        "move": "manage_memory_file:move",
        "delete": "manage_memory_file:delete",
        "recover": "manage_memory_file:recover",
    }[step["kind"]]
    return {
        "version": 1,
        "run_id": run_identity,
        "plan_id": plan_identity,
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "before": before,
        "after": after,
        "leaf_identity": leaf_identity,
        "parent_compensation_plan_id": parent_compensation_plan_id,
    }


def _witness_payload(**kwargs: Any) -> dict[str, Any]:
    basis = _witness_basis(**kwargs)
    return {
        **basis,
        "result_digest": _digest(basis),
        "committed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }


def _finalize_witness_batch(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    writes: list[Any],
    content: str,
) -> str | None:
    """Bind exact authored postimages from the leaf's own planned batch."""
    from . import curation_witness

    root = Path(vault_root).absolute()
    hashes: dict[str, str] = {}
    for write in writes:
        if not isinstance(getattr(write, "content", None), str):
            continue
        try:
            relative = Path(write.path).absolute().relative_to(root).as_posix()
        except ValueError:
            continue
        hashes[relative] = text_content_hash(write.content)
    kind = str(step["kind"])
    source = str(binding["path"])
    destination = str(binding.get("postcondition", {}).get("path") or source)
    prior_hash = binding.get("before_hash")
    if kind == "delete":
        return None
    if kind == "move":
        pending = curation_witness.pending_for(vault_root)
        if pending is None or not pending.ready:
            return None
        expected = binding.get("effect_after")
        after = []
        for item in expected if isinstance(expected, list) else []:
            row = dict(item)
            path = str(row.get("path") or "")
            if path in hashes:
                row = {"path": path, "content_hash": hashes[path]}
            after.append(row)
        if not after:
            after = [
                {"path": source, "absent": True},
                {"path": destination, "content_hash": hashes.get(destination) or prior_hash},
            ]
    elif kind == "recover":
        pending = curation_witness.pending_for(vault_root)
        if pending is None or not pending.ready:
            return None
        after = [
            {"path": source, "absent": True},
            {"path": destination, "content_hash": hashes.get(destination) or prior_hash},
        ]
    elif kind == "supersede":
        if source not in hashes or destination not in hashes:
            return None
        after = [
            {"path": path, "content_hash": hashes[path]}
            for path in (source, destination)
            if path in hashes
        ]
    else:
        exact_hash = hashes.get(destination)
        if exact_hash is None:
            return None
        after = [{"path": destination, "content_hash": exact_hash}]
    return curation_witness.render_with_after(content, after)


def _validate_witness(
    value: Any,
    *,
    run_identity: str,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    operation_identity: str,
    binding: Mapping[str, Any],
    parent_compensation_plan_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness is not an object")
    allowed = {
        "version",
        "run_id",
        "plan_id",
        "ordinal",
        "step_id",
        "operation_id",
        "before",
        "after",
        "leaf_identity",
        "parent_compensation_plan_id",
        "result_digest",
        "committed_at",
    }
    if set(value) != allowed:
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness schema is invalid")
    expected = _witness_basis(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=parent_compensation_plan_id,
    )
    actual_basis = {key: value[key] for key in expected}
    expected_without_after = {key: item for key, item in expected.items() if key != "after"}
    actual_without_after = {key: item for key, item in actual_basis.items() if key != "after"}
    after = actual_basis.get("after")
    if (
        actual_without_after != expected_without_after
        or not isinstance(after, list)
        or not after
        or value.get("result_digest") != _digest(actual_basis)
    ):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness identity is invalid")
    kind = str(step["kind"])
    source = str(binding["path"])
    destination = str(binding.get("postcondition", {}).get("path") or source)
    normalized: list[tuple[str, str]] = []
    for item in after:
        if not isinstance(item, Mapping):
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid")
        keys = set(item)
        if (
            keys == {"path", "content_hash"}
            and isinstance(item.get("content_hash"), str)
            and _HEX64.fullmatch(str(item["content_hash"]))
        ):
            shape = "content"
        elif keys == {"path", "absent"} and item.get("absent") is True:
            shape = "absent"
        else:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid")
        try:
            path = normalize_target_path(
                item.get("path"),
                field="witness.after.path",
                allow_trash=kind in {"delete", "recover"},
            )
        except CurationError as error:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid") from error
        normalized.append((path, shape))
    if len({path for path, _shape in normalized}) != len(normalized):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is duplicated")
    sealed_after = binding.get("effect_after")
    if (
        isinstance(sealed_after, list)
        and sealed_after
        and not (kind == "delete" and len(sealed_after) == 1)
        and all(
        isinstance(item, Mapping)
        and (
            set(item) == {"path", "content_hash"}
            or (set(item) == {"path", "absent"} and item.get("absent") is True)
        )
        for item in sealed_after
        )
    ):
        required = {
            (str(item["path"]), "absent" if item.get("absent") is True else "content")
            for item in sealed_after
        }
    elif kind in {"move", "recover"}:
        required = {(source, "absent"), (destination, "content")}
    elif kind == "supersede":
        required = {(source, "content"), (destination, "content")}
    elif kind == "delete":
        trash = [
            path
            for path, shape in normalized
            if path != source
            and shape == "content"
            and path.casefold().startswith(f"{kb_dirname().casefold()}/_trash/")
        ]
        required = {(source, "absent"), *((path, "content") for path in trash)}
        if len(trash) != 1:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "delete witness is invalid")
    else:
        required = {(destination, "content")}
    if set(normalized) != required or len(normalized) != len(required):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage scope is invalid")
    return dict(value)


def _verify_live_postcondition(vault_root: Path, witness: Mapping[str, Any]) -> None:
    for item in witness["after"]:
        relative = str(item["path"])
        if item.get("absent") is True:
            try:
                absent = _guarded_absent(vault_root, relative)
            except CurationError as error:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN", "witness target path is unsafe"
                ) from error
            if not absent:
                raise _error("CURATION_OUTCOME_UNCERTAIN", "witness says absent but target exists")
            continue
        expected = item.get("content_hash")
        if expected is not None:
            try:
                _source, actual = _read_target(vault_root, relative)
            except CurationError as error:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN", "witness target is unreadable"
                ) from error
            if actual != expected:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN", "witness and live postcondition disagree"
                )


def _dispatch_step(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    runtime_args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from . import commands

    kind = str(step["kind"])
    args = json.loads(canonical_json(step["args"]))
    prepared = dict(binding.get("prepared") or {})
    if kind == "create-note":
        relation = {
            key: prepared[key]
            for key in (
                "relation_disposition",
                "relation_review_hash",
                "relation_review_reason",
            )
            if prepared.get(key) is not None
        }
        for key in ("relation_disposition", "relation_review_reason"):
            args.pop(key, None)
        return commands.op_remember(
            vault_root,
            **args,
            draft_id=prepared["draft_id"],
            draft_hash=prepared["draft_hash"],
            draft_token=prepared["draft_token"],
            **relation,
        )
    if kind == "create-entity":
        return commands.op_connect_memory(vault_root, operation="create-entity", **args)
    if kind == "accept-relation":
        return commands.op_connect_memory(vault_root, operation="accept-relation", **args)
    if kind == "edit":
        operation = dict(args["operation"])
        operation.pop("validate_only", None)
        operation["transition_token"] = prepared["transition_token"]
        operation["relation_review_hash"] = prepared["relation_review_hash"]
        return commands.op_edit_memory(
            vault_root, path=args["path"], why=args["why"], operation=operation
        )
    if kind == "supersede":
        relation = {
            key: prepared[key]
            for key in (
                "relation_disposition",
                "relation_review_hash",
                "relation_review_reason",
            )
            if prepared.get(key) is not None
        }
        for key in ("relation_disposition", "relation_review_reason"):
            args.pop(key, None)
        return commands.op_replace_memory(
            vault_root,
            **args,
            draft_id=prepared["draft_id"],
            draft_hash=prepared["draft_hash"],
            draft_token=prepared["draft_token"],
            **relation,
        )
    if kind == "move":
        return commands.op_manage_memory_file(vault_root, operation="move", **args)
    if kind == "delete":
        from . import delete_file as delete_module

        now_value = (runtime_args or {}).get("now")
        now = dt.datetime.fromisoformat(now_value) if isinstance(now_value, str) else None
        try:
            result = delete_module.delete_file(vault_root, now=now, **args)
        except delete_module.DeleteFileError as error:
            raise _error(error.code, error.reason) from error
        if isinstance(result, delete_module.DeleteFileValidation):
            raise _error("CURATION_STEP_FAILED", "delete unexpectedly remained validate-only")
        return result.as_dict()
    assert kind == "recover"
    return commands.op_manage_memory_file(vault_root, operation="recover", **args)


def _runtime_execution_binding(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    execution = json.loads(canonical_json(binding))
    effect = {
        "kind": step["kind"],
        "path": str(binding["postcondition"]["path"]),
    }
    runtime_args: dict[str, Any] = {}
    if step["kind"] == "delete":
        from . import delete_file as delete_module

        now = dt.datetime.now()
        try:
            validation = delete_module.delete_file(
                vault_root, validate_only=True, now=now, **dict(step["args"])
            )
        except delete_module.DeleteFileError as error:
            raise _error(error.code, error.reason) from error
        if not isinstance(validation, delete_module.DeleteFileValidation):
            raise _error("CURATION_STEP_INVALID", "delete leaf did not seal its destination")
        source = str(binding["path"])
        trash = validation.result.trash_path
        execution["effect_after"] = [
            {"path": source, "absent": True},
            {"path": trash, "content_hash": validation.content_hash},
        ]
        runtime_args["now"] = now.isoformat()
        effect.update(
            {
                "path": source,
                "trash_path": trash,
                "trash_meta_path": validation.result.trash_meta_path,
            }
        )
    elif step["kind"] == "move":
        effect.update(
            {
                "old_path": step["args"]["old_path"],
                "new_path": step["args"]["new_path"],
                "path": step["args"]["new_path"],
            }
        )
    elif step["kind"] == "recover":
        effect.update(
            {
                "trash_path": step["args"]["trash_path"],
                "restored_path": binding["postcondition"]["path"],
                "path": binding["postcondition"]["path"],
            }
        )
    return execution, effect, runtime_args


def _evidence_path(store: CurationStore, run_identity: str, operation_identity: str) -> Path:
    return store.evidence_dir(run_identity) / f"{operation_identity}.json"


def _load_rename_preparation(
    store: CurationStore,
    run_identity: str,
    prepared: Mapping[str, Any],
    *,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    compensation: bool,
) -> dict[str, Any] | None:
    digest = prepared.get("preparation_digest")
    if digest is None:
        return None
    root = store.vault_root
    del run_identity, plan_identity, ordinal, step, binding, compensation
    _require_relocation_history_capability(root)


def _prepared_placement(vault_root: Path, preparation: Mapping[str, Any]) -> str:
    del preparation
    _require_relocation_history_capability(vault_root)


def _roll_forward_prepared(
    vault_root: Path,
    store: CurationStore,
    run_identity: str,
    *,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
    preparation: Mapping[str, Any],
    compensation: bool,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    del (
        store,
        run_identity,
        plan_identity,
        ordinal,
        step,
        binding,
        operation_identity,
        preparation,
        compensation,
    )
    _require_relocation_history_capability(vault_root)


def _committed_step_ids(state: Mapping[str, Any]) -> set[str]:
    return set(str(item) for item in state.get("committed_steps", []))


def _next_step(
    plan: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[int, Mapping[str, Any], Mapping[str, Any]] | None:
    committed = _committed_step_ids(state)
    for ordinal, step in enumerate(plan["steps"]):
        if step["step_id"] not in committed:
            if state.get("failed_step") == step["step_id"] and not state.get("retryable", False):
                return None
            return ordinal, step, plan["binding_manifest"][ordinal]
    return None


def _blockers_for_uncommitted(
    vault_root: Path, plan: Mapping[str, Any], committed: set[str]
) -> list[dict[str, Any]]:
    filtered = {
        **plan,
        "binding_manifest": [
            item for item in plan["binding_manifest"] if item["step_id"] not in committed
        ],
    }
    return _binding_blockers(vault_root, filtered, registry_identities(vault_root))


def _record_blocked_state(
    store: CurationStore, run_identity: str, *, operation_identity: str, step_id: str
) -> None:
    store.write_state(
        run_identity,
        {
            "version": 1,
            "run_id": run_identity,
            "phase": "blocked",
            "error_code": "CURATION_OUTCOME_UNCERTAIN",
            "prepared": {"operation_id": operation_identity, "step_id": step_id},
        },
    )


def _compensation_phase(result: dict[str, Any], *, compensation: bool) -> dict[str, Any]:
    if not compensation:
        return result
    phase = {
        "proposed": "proposed",
        "approved": "compensating",
        "executing": "compensating",
        "partial": "compensation-partial",
        "failed": "compensation-partial",
        "completed": "compensated",
        "blocked": "blocked",
    }.get(str(result.get("phase")), str(result.get("phase")))
    return {**result, "phase": phase}


def _effect_projection(
    kind: str, result: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = {
        key: result[key]
        for key in (
            "path",
            "new_path",
            "old_path",
            "trash_path",
            "trash_meta_path",
            "restored_path",
            "ref",
        )
        if isinstance(result.get(key), str)
    }
    if "path" not in allowed:
        allowed["path"] = str(binding["postcondition"]["path"])
    allowed["kind"] = kind
    return allowed


def _next_attempt(store: CurationStore, run_identity: str, step_id: str) -> int:
    attempts = [
        int(receipt["attempt"])
        for receipt in store._receipt_records(run_identity)
        if receipt.get("step_id") == step_id and type(receipt.get("attempt")) is int
    ]
    return max(attempts, default=0) + 1


def _record_failed_attempt(
    store: CurationStore,
    run_identity: str,
    *,
    ordinal: int,
    step_id: str,
    operation_identity: str,
    code: str,
    retryable: bool,
) -> dict[str, Any]:
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, step_id),
        "ordinal": ordinal,
        "step_id": step_id,
        "operation_id": operation_identity,
        "outcome": "failed",
        "error_code": code,
        "retryable": retryable,
    }
    store.create_receipt(run_identity, ordinal, step_id, receipt)
    state = store.reconstruct(run_identity)
    store.write_state(run_identity, state)
    return receipt


def _execute_next(
    vault_root: Path,
    run_identity: str,
    plan_identity: str,
    *,
    store: CurationStore | None = None,
    compensation: bool = False,
) -> dict[str, Any]:
    from . import curation_witness

    store = store or CurationStore(vault_root)
    plan = store.load_plan(run_identity)
    expected_identity, _fingerprint = store.identities(run_identity)
    if plan_identity != expected_identity:
        raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "resume cannot switch plans")
    _require_plan_relocation_history(vault_root, plan)
    reconstructed = store.reconstruct(run_identity)
    if reconstructed.get("phase") == "blocked":
        raise _error("CURATION_OUTCOME_UNCERTAIN", "run evidence is invalid")
    next_item = _next_step(plan, reconstructed)
    if next_item is None:
        return _compensation_phase(
            {**reconstructed, "outcome": "replayed", "mutated": False},
            compensation=compensation,
        )
    ordinal, step, binding = next_item
    operation_identity = operation_id(plan_identity, ordinal, step["step_id"])
    evidence_path = _evidence_path(store, run_identity, operation_identity)
    projection: Mapping[str, Any] = {}
    if store.state_path(run_identity).exists():
        raw_projection = store._read_json(store.state_path(run_identity))
        projection = raw_projection if isinstance(raw_projection, Mapping) else {}
    prepared_projection = projection.get("prepared")
    preparation: dict[str, Any] | None = None
    if (
        isinstance(prepared_projection, Mapping)
        and prepared_projection.get("ordinal") == ordinal
        and prepared_projection.get("step_id") == step["step_id"]
        and prepared_projection.get("operation_id") == operation_identity
    ):
        preparation = _load_rename_preparation(
            store,
            run_identity,
            prepared_projection,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            binding=binding,
            compensation=compensation,
        )
    execution_binding = dict(binding)
    recovered_effect = (
        dict(preparation.get("effect") or {})
        if preparation is not None
        else _effect_projection(str(step["kind"]), {}, execution_binding)
    )
    if evidence_path.exists():
        try:
            witness = _validate_witness(
                store._read_json(evidence_path),
                run_identity=run_identity,
                plan_identity=plan_identity,
                ordinal=ordinal,
                step=step,
                operation_identity=operation_identity,
                binding=execution_binding,
                parent_compensation_plan_id=plan_identity if compensation else None,
            )
            _verify_live_postcondition(vault_root, witness)
        except CurationError:
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        receipt = {
            "version": 1,
            "attempt": _next_attempt(store, run_identity, step["step_id"]),
            "ordinal": ordinal,
            "step_id": step["step_id"],
            "operation_id": operation_identity,
            "outcome": "recovered-committed",
            "result_digest": witness["result_digest"],
            "effect": recovered_effect,
        }
        store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
        final = store.reconstruct(run_identity)
        store.write_state(run_identity, final)
        return _compensation_phase(
            {
                **final,
                "mutated": True,
                "step": {
                    **receipt,
                    "path": witness["after"][0]["path"],
                },
            },
            compensation=compensation,
        )

    if preparation is not None:
        try:
            recovered = _roll_forward_prepared(
                vault_root,
                store,
                run_identity,
                plan_identity=plan_identity,
                ordinal=ordinal,
                step=step,
                binding=binding,
                operation_identity=operation_identity,
                preparation=preparation,
                compensation=compensation,
            )
        except CurationError:
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        if recovered is not None:
            witness, recovered_effect = recovered
            receipt = {
                "version": 1,
                "attempt": _next_attempt(store, run_identity, step["step_id"]),
                "ordinal": ordinal,
                "step_id": step["step_id"],
                "operation_id": operation_identity,
                "outcome": "recovered-committed",
                "result_digest": witness["result_digest"],
                "effect": recovered_effect,
            }
            store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
            final = store.reconstruct(run_identity)
            store.write_state(run_identity, final)
            return _compensation_phase(
                {
                    **final,
                    "mutated": True,
                    "step": {
                        **receipt,
                        "path": str(recovered_effect.get("path") or witness["after"][0]["path"]),
                    },
                },
                compensation=compensation,
            )

    blockers = _blockers_for_uncommitted(vault_root, plan, _committed_step_ids(reconstructed))
    if blockers:
        blocker = blockers[0]
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=blocker["code"],
            retryable=False,
        )
        raise _error(blocker["code"], blocker["reason"])
    execution_binding, prepared_effect, runtime_args = _runtime_execution_binding(
        vault_root, step, binding
    )
    prepared_state = {
        "version": 1,
        "run_id": run_identity,
        "plan_id": plan_identity,
        "phase": "executing",
        "prepared": {
            "ordinal": ordinal,
            "step_id": step["step_id"],
            "operation_id": operation_identity,
        },
    }
    store.write_state(run_identity, prepared_state)
    _fault_barrier("after-prepared-state")
    witness = _witness_payload(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=plan_identity if compensation else None,
    )
    try:
        with curation_witness.atomic_witness(
            vault_root,
            evidence_path,
            canonical_json(witness),
            finalize=lambda writes, content: _finalize_witness_batch(
                vault_root, step, execution_binding, writes, content
            ),
        ) as pending:
            leaf_result = _dispatch_step(
                vault_root, step, execution_binding, runtime_args=runtime_args
            )
            if not pending.consumed:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN",
                    f"{step['kind']} did not commit its witness with the leaf effect",
                )
        witness = _validate_witness(
            store._read_json(evidence_path),
            run_identity=run_identity,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            operation_identity=operation_identity,
            binding=execution_binding,
            parent_compensation_plan_id=plan_identity if compensation else None,
        )
        _verify_live_postcondition(vault_root, witness)
    except CurationError as error:
        if error.code == "CURATION_OUTCOME_UNCERTAIN":
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=error.code,
            retryable=False,
        )
        raise
    except Exception as error:
        stable_code = getattr(error, "code", None)
        if not isinstance(stable_code, str) and ":" in str(error):
            candidate = str(error).split(":", 1)[0]
            stable_code = candidate if re.fullmatch(r"[A-Z][A-Z0-9_]+", candidate) else None
        code = stable_code or (
            "CURATION_RETRYABLE_FAILURE" if isinstance(error, OSError) else "CURATION_STEP_FAILED"
        )
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=code,
            retryable=isinstance(error, OSError),
        )
        raise _error(code, str(error)) from error
    _fault_barrier("after-compensation-leaf-witness" if compensation else "after-leaf-witness")
    effect = _effect_projection(str(step["kind"]), leaf_result, execution_binding)
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, step["step_id"]),
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "outcome": "committed",
        "result_digest": witness["result_digest"],
        "effect": effect,
    }
    store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
    _fault_barrier("after-terminal-receipt")
    final = store.reconstruct(run_identity)
    store.write_state(run_identity, final)
    path = str(
        leaf_result.get("path")
        or leaf_result.get("new_path")
        or leaf_result.get("restored_path")
        or binding["postcondition"]["path"]
    )
    return _compensation_phase(
        {
            **final,
            "mutated": True,
            "step": {**receipt, "path": path, "leaf_result": leaf_result},
        },
        compensation=compensation,
    )


def apply(
    vault_root: Path,
    *,
    run_id: str,
    plan_id: str,
    expected_plan_fingerprint: str,
    why: str,
) -> dict[str, Any]:
    root = Path(vault_root)
    store = CurationStore(root)
    plan = store.load_plan(run_id)
    current_id, current_fingerprint = store.identities(run_id)
    if plan_id != current_id or expected_plan_fingerprint != current_fingerprint:
        raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "apply identity does not match stored plan")
    _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
    _require_plan_relocation_history(root, plan)
    blockers = _blockers_for_uncommitted(root, plan, set())
    if blockers:
        raise _error(blockers[0]["code"], blockers[0]["reason"])
    store.create_approval(
        run_id,
        plan_id=plan_id,
        fingerprint=expected_plan_fingerprint,
        why=why,
    )
    return _execute_next(root, run_id, plan_id)


def resume(vault_root: Path, *, run_id: str, plan_id: str) -> dict[str, Any]:
    root = Path(vault_root)
    store = CurationStore(root)
    forward_identity, _ = store.identities(run_id)
    compensation = plan_id != forward_identity
    active_store: CurationStore = (
        CompensationStore(root, run_id, plan_id) if compensation else store
    )
    _require_plan_relocation_history(root, active_store.load_plan(run_id))
    if not active_store.approval_path(run_id).exists():
        raise _error("CURATION_APPROVAL_REQUIRED", "resume requires an approved immutable plan")
    approval = active_store.validated_approval(run_id, required=True)
    assert approval is not None
    if approval.get("plan_id") != plan_id:
        raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "resume cannot switch plans")
    return _execute_next(
        root,
        run_id,
        plan_id,
        store=active_store,
        compensation=compensation,
    )


def status(vault_root: Path, *, run_id: str) -> dict[str, Any]:
    """Inspect recovery state without writing or repairing any artifact."""
    root = Path(vault_root)
    forward_store = CurationStore(root)
    forward_store.load_plan(run_id)
    forward_store.identities(run_id)
    store: CurationStore = forward_store
    compensation = False
    compensation_root = forward_store.compensation_root(run_id)
    forward_store._assert_safe(compensation_root)
    if compensation_root.exists():
        candidates = [
            path.name
            for path in sorted(compensation_root.iterdir())
            if path.is_dir() and not path.is_symlink() and _HEX64.fullmatch(path.name)
        ]
        if len(candidates) > 1:
            return {
                "action": "status",
                "mutated": False,
                "run_id": run_id,
                "phase": "blocked",
                "error_code": "CURATION_OUTCOME_UNCERTAIN",
                "recovery": "blocked",
            }
        if candidates:
            store = CompensationStore(root, run_id, candidates[0])
            compensation = True
    plan = store.load_plan(run_id)
    identity, _fingerprint = store.identities(run_id)
    reconstructed = store.reconstruct(run_id)
    projection: Mapping[str, Any] = {}
    if store.state_path(run_id).exists():
        try:
            raw = store._read_json(store.state_path(run_id))
            projection = raw if isinstance(raw, Mapping) else {}
        except CurationError:
            projection = {}
    if projection.get("phase") == "blocked":
        return {
            **reconstructed,
            "action": "status",
            "mutated": False,
            "phase": "blocked",
            "error_code": "CURATION_OUTCOME_UNCERTAIN",
            "recovery": "blocked",
        }
    if reconstructed["phase"] in {"completed", "partial", "failed", "blocked"}:
        result = {
            **reconstructed,
            "action": "status",
            "mutated": False,
            "recovery": "blocked" if reconstructed["phase"] == "blocked" else None,
        }
        return _compensation_phase(result, compensation=compensation)
    prepared = projection.get("prepared")
    if isinstance(prepared, Mapping):
        ordinal = prepared.get("ordinal")
        if type(ordinal) is int and 0 <= ordinal < len(plan["steps"]):
            step = plan["steps"][ordinal]
            binding = plan["binding_manifest"][ordinal]
            operation_identity = operation_id(identity, ordinal, step["step_id"])
            evidence_path = _evidence_path(store, run_id, operation_identity)
            preparation: dict[str, Any] | None = None
            try:
                preparation = _load_rename_preparation(
                    store,
                    run_id,
                    prepared,
                    plan_identity=identity,
                    ordinal=ordinal,
                    step=step,
                    binding=binding,
                    compensation=compensation,
                )
            except CurationError as error:
                return {
                    **reconstructed,
                    "action": "status",
                    "mutated": False,
                    "phase": "blocked",
                    "error_code": error.code,
                    "recovery": "blocked",
                }
            execution_binding = binding
            if evidence_path.exists():
                try:
                    witness = _validate_witness(
                        store._read_json(evidence_path),
                        run_identity=run_id,
                        plan_identity=identity,
                        ordinal=ordinal,
                        step=step,
                        operation_identity=operation_identity,
                        binding=execution_binding,
                        parent_compensation_plan_id=identity if compensation else None,
                    )
                    _verify_live_postcondition(root, witness)
                except CurationError:
                    return {
                        **reconstructed,
                        "action": "status",
                        "mutated": False,
                        "phase": "blocked",
                        "error_code": "CURATION_OUTCOME_UNCERTAIN",
                        "recovery": "blocked",
                    }
                recovery = "receipt-required"
            elif preparation is not None:
                placement = _prepared_placement(root, preparation)
                recovery = {
                    "prior": "retry-uncommitted",
                    "target": "witness-required",
                    "rename-transition": "roll-forward-required",
                    "ambiguous": "blocked",
                }[placement]
                if recovery == "blocked":
                    return {
                        **reconstructed,
                        "action": "status",
                        "mutated": False,
                        "phase": "blocked",
                        "error_code": "CURATION_OUTCOME_UNCERTAIN",
                        "recovery": "blocked",
                    }
            else:
                recovery = "retry-uncommitted"
            result = {
                **reconstructed,
                "action": "status",
                "mutated": False,
                "phase": "executing",
                "recovery": recovery,
                "active_step": step["step_id"],
            }
            return _compensation_phase(result, compensation=compensation)
    result = {**reconstructed, "action": "status", "mutated": False, "recovery": None}
    return _compensation_phase(result, compensation=compensation)


def valid_replay_result(value: Any) -> bool:
    """Recognize only a durable, terminal curation no-op replay."""
    return (
        isinstance(value, Mapping)
        and value.get("outcome") == "replayed"
        and value.get("mutated") is False
        and value.get("phase") in {"completed", "compensated"}
        and isinstance(value.get("run_id"), str)
        and isinstance(value.get("plan_id"), str)
        and _HEX64.fullmatch(str(value["plan_id"])) is not None
    )


class CurationStore:
    """Canonical run store under the governed vault with create-only evidence."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.root = self.vault_root / kb_dirname() / "_Governance" / "curation" / "runs"

    def _assert_safe(self, target: Path) -> None:
        try:
            relative = target.absolute().relative_to(self.vault_root.absolute())
        except ValueError as error:
            raise _error("CURATION_PATH_UNSAFE", "curation artifact escapes the vault") from error
        current = self.vault_root.absolute()
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise _error("CURATION_PATH_UNSAFE", "curation artifact crosses a symlink")
            except OSError as error:
                raise _error(
                    "CURATION_PATH_UNSAFE", "curation path could not be inspected"
                ) from error

    def run_dir(self, run_identity: str) -> Path:
        if not re.fullmatch(r"cur-[0-9]{8}-[0-9a-f]{12}", str(run_identity)):
            raise _error("INVALID_RUN_ID", "run id has an invalid shape")
        return self.root / run_identity

    def plan_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "plan.json"

    def approval_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "approval.json"

    def identity_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "identity.json"

    def state_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "state.json"

    def receipts_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "receipts"

    def evidence_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "evidence"

    def prepared_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "prepared"

    def compensation_root(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "compensation"

    def _read_json(self, path: Path) -> Any:
        self._assert_safe(path)
        try:
            stat = path.lstat()
            if not path.is_file() or stat.st_size > MAX_PLAN_BYTES * 4:
                raise _error("CURATION_ARTIFACT_CORRUPT", "curation artifact is not a bounded file")
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except CurationError:
            raise
        except FileNotFoundError as error:
            raise _error("CURATION_RUN_NOT_FOUND", "curation artifact does not exist") from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise _error("CURATION_ARTIFACT_CORRUPT", "curation artifact is unreadable") from error

    def _write_json(self, path: Path, value: Any, *, create_only: bool) -> None:
        self._assert_safe(path)
        content = canonical_json(value)
        try:
            batch_atomic_write(
                [PlannedWrite(path=path, content=content, create_only=create_only)],
                vault_root=self.vault_root,
            )
        except (BatchWriteError, OSError, ValueError) as error:
            code = "CURATION_PLAN_COLLISION" if create_only else "CURATION_STORE_WRITE_FAILED"
            raise _error(code, "curation artifact write was refused") from error

    def _create_plan_at(self, path: Path, plan: Mapping[str, Any]) -> None:
        identity = plan_id(plan)
        base = {
            key: value
            for key, value in plan.items()
            if key not in {"binding_manifest", "registry_ids"}
        }
        fingerprint = plan_fingerprint(base, plan["binding_manifest"], plan["registry_ids"])
        anchor_path = path.parent / "identity.json"
        anchor = {
            "version": 1,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
        }
        if path.exists():
            if (
                anchor_path.exists()
                and canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(plan)
                and canonical_json_bytes(self._read_json(anchor_path))
                == canonical_json_bytes(anchor)
            ):
                return
            raise _error("CURATION_PLAN_COLLISION", "immutable plan bytes already differ")
        if anchor_path.exists():
            raise _error("CURATION_PLAN_COLLISION", "immutable plan identity already exists")
        self._assert_safe(path)
        self._assert_safe(anchor_path)
        try:
            batch_atomic_write(
                [
                    PlannedWrite(path=path, content=canonical_json(plan), create_only=True),
                    PlannedWrite(
                        path=anchor_path,
                        content=canonical_json(anchor),
                        create_only=True,
                    ),
                ],
                vault_root=self.vault_root,
            )
        except (BatchWriteError, OSError, ValueError) as error:
            raise _error("CURATION_PLAN_COLLISION", "curation plan seal was refused") from error

    def create_forward(
        self,
        plan: Mapping[str, Any],
        *,
        binding_manifest: Iterable[Mapping[str, Any]],
        registry_ids: Mapping[str, str],
        today: dt.date | None = None,
    ) -> dict[str, Any]:
        validated = validate_forward_plan(plan)
        sealed = {
            **validated,
            "binding_manifest": json.loads(canonical_json(list(binding_manifest))),
            "registry_ids": dict(registry_ids),
        }
        identity = plan_id(sealed)
        run_identity = f"cur-{(today or dt.date.today()):%Y%m%d}-{identity[:12]}"
        path = self.plan_path(run_identity)
        self._assert_safe(path)
        self._create_plan_at(path, sealed)
        fingerprint = plan_fingerprint(
            validated, sealed["binding_manifest"], sealed["registry_ids"]
        )
        projection = {
            "version": 1,
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": "proposed",
            "active_step": None,
        }
        if not self.state_path(run_identity).exists():
            self._write_json(self.state_path(run_identity), projection, create_only=True)
        return {**projection, "plan": sealed}

    def load_plan(self, run_identity: str) -> dict[str, Any]:
        plan = _validate_sealed_plan(self._read_json(self.plan_path(run_identity)))
        return plan

    def identities(self, run_identity: str) -> tuple[str, str]:
        stored = self.load_plan(run_identity)
        identity = plan_id(stored)
        base = {
            key: value
            for key, value in stored.items()
            if key not in {"binding_manifest", "registry_ids"}
        }
        fingerprint = plan_fingerprint(base, stored["binding_manifest"], stored["registry_ids"])
        anchor = self._read_json(self.identity_path(run_identity))
        if (
            not isinstance(anchor, Mapping)
            or set(anchor) != {"version", "plan_id", "plan_fingerprint"}
            or anchor.get("version") != 1
            or anchor.get("plan_id") != identity
            or anchor.get("plan_fingerprint") != fingerprint
        ):
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "stored plan no longer matches its immutable seal"
            )
        if not isinstance(self, CompensationStore) and not run_identity.endswith(identity[:12]):
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "run identity no longer matches stored plan"
            )
        return identity, fingerprint

    def create_approval(
        self,
        run_identity: str,
        *,
        plan_id: str,
        fingerprint: str,
        why: str,
        compensation_plan_id: str | None = None,
    ) -> dict[str, Any]:
        _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
        expected_id, expected_fingerprint = self.identities(run_identity)
        if plan_id != expected_id or fingerprint != expected_fingerprint:
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "approval does not bind current plan bytes"
            )
        approval = {
            "version": 1,
            "run_id": run_identity,
            "plan_id": plan_id,
            "plan_fingerprint": fingerprint,
            "why": why,
            "approved_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "compensation_plan_id": compensation_plan_id,
        }
        path = self.approval_path(run_identity)
        if path.exists():
            current = self.validated_approval(run_identity, required=True)
            assert current is not None
            if all(
                current.get(key) == approval[key] for key in ("plan_id", "plan_fingerprint", "why")
            ):
                return current
            raise _error("CURATION_APPROVAL_COLLISION", "a different approval already exists")
        self._write_json(path, approval, create_only=True)
        return approval

    def validated_approval(
        self, run_identity: str, *, required: bool = False
    ) -> dict[str, Any] | None:
        path = self.approval_path(run_identity)
        if not path.exists():
            if required:
                raise _error("CURATION_APPROVAL_REQUIRED", "approved plan evidence is missing")
            return None
        value = self._read_json(path)
        identity, fingerprint = self.identities(run_identity)
        expected_compensation = (
            identity if isinstance(self, CompensationStore) else None
        )
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "version",
                "run_id",
                "plan_id",
                "plan_fingerprint",
                "why",
                "approved_at",
                "compensation_plan_id",
            }
            or value.get("version") != 1
            or value.get("run_id") != run_identity
            or value.get("plan_id") != identity
            or value.get("plan_fingerprint") != fingerprint
            or value.get("compensation_plan_id") != expected_compensation
            or not isinstance(value.get("approved_at"), str)
        ):
            raise _error("CURATION_APPROVAL_INVALID", "approval does not bind the sealed plan")
        _require_string(value.get("why"), "approval.why", max_chars=MAX_WHY_CHARS)
        return dict(value)

    def create_evidence(
        self, run_identity: str, operation_identity: str, evidence: Mapping[str, Any]
    ) -> Path:
        if not _HEX64.fullmatch(operation_identity):
            raise _error("INVALID_OPERATION_ID", "witness operation id is invalid")
        path = self.evidence_dir(run_identity) / f"{operation_identity}.json"
        if path.exists():
            if canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(evidence):
                return path
            raise _error("CURATION_OUTCOME_UNCERTAIN", "a competing witness exists")
        self._write_json(path, dict(evidence), create_only=True)
        return path

    def create_receipt(
        self,
        run_identity: str,
        ordinal: int,
        step_id: str,
        receipt: Mapping[str, Any],
    ) -> Path:
        attempt = receipt.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise _error("CURATION_RECEIPT_INVALID", "receipt attempt must be positive")
        path = self.receipts_dir(run_identity) / f"{ordinal:03d}-{step_id}" / f"{attempt:04d}.json"
        if path.exists():
            if canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(receipt):
                return path
            raise _error("CURATION_OUTCOME_UNCERTAIN", "a competing receipt exists")
        self._write_json(path, dict(receipt), create_only=True)
        return path

    def write_state(self, run_identity: str, state: Mapping[str, Any]) -> None:
        self._write_json(self.state_path(run_identity), dict(state), create_only=False)

    def _receipt_records(self, run_identity: str) -> list[dict[str, Any]]:
        root = self.receipts_dir(run_identity)
        self._assert_safe(root)
        if not root.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(root.glob("*/*.json")):
            value = self._read_json(path)
            if not isinstance(value, Mapping):
                raise _error("CURATION_ARTIFACT_CORRUPT", "receipt is not an object")
            records.append(dict(value))
        return records

    def reconstruct(self, run_identity: str) -> dict[str, Any]:
        plan = self.load_plan(run_identity)
        identity, fingerprint = self.identities(run_identity)
        steps = plan["steps"]
        try:
            approval = self.validated_approval(run_identity)
        except CurationError:
            return {
                "run_id": run_identity,
                "plan_id": identity,
                "plan_fingerprint": fingerprint,
                "phase": "blocked",
                "error_code": "CURATION_OUTCOME_UNCERTAIN",
                "committed_steps": [],
                "receipts": [],
                "next_action": None,
            }
        receipts = self._receipt_records(run_identity)
        committed: dict[str, dict[str, Any]] = {}
        failed: dict[str, dict[str, Any]] = {}
        for receipt in receipts:
            ordinal = receipt.get("ordinal")
            if type(ordinal) is not int or not 0 <= ordinal < len(steps):
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
            step = steps[ordinal]
            step_identity = str(receipt.get("step_id") or "")
            expected_operation = operation_id(identity, ordinal, step["step_id"])
            outcome = receipt.get("outcome")
            common_valid = bool(
                receipt.get("version") == 1
                and type(receipt.get("attempt")) is int
                and receipt["attempt"] >= 1
                and step_identity == step["step_id"]
                and receipt.get("operation_id") == expected_operation
            )
            if not common_valid:
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
            if outcome in {"committed", "recovered-committed"}:
                if approval is None or set(receipt) != {
                    "version",
                    "attempt",
                    "ordinal",
                    "step_id",
                    "operation_id",
                    "outcome",
                    "result_digest",
                    "effect",
                }:
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                try:
                    binding = plan["binding_manifest"][ordinal]
                    witness = _validate_witness(
                        self._read_json(
                            self.evidence_dir(run_identity) / f"{expected_operation}.json"
                        ),
                        run_identity=run_identity,
                        plan_identity=identity,
                        ordinal=ordinal,
                        step=step,
                        operation_identity=expected_operation,
                        binding=binding,
                        parent_compensation_plan_id=(
                            identity if isinstance(self, CompensationStore) else None
                        ),
                    )
                except (CurationError, IndexError, KeyError, TypeError):
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                effect = receipt.get("effect")
                if (
                    receipt.get("result_digest") != witness["result_digest"]
                    or not isinstance(effect, Mapping)
                    or effect.get("kind") != step["kind"]
                ):
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                previous = committed.get(step_identity)
                if previous is not None and canonical_json(previous) != canonical_json(receipt):
                    return {
                        "run_id": run_identity,
                        "plan_id": identity,
                        "plan_fingerprint": fingerprint,
                        "phase": "blocked",
                        "error_code": "CURATION_OUTCOME_UNCERTAIN",
                        "committed_steps": sorted(committed),
                        "next_action": None,
                    }
                committed[step_identity] = receipt
            elif outcome == "failed":
                if set(receipt) != {
                    "version",
                    "attempt",
                    "ordinal",
                    "step_id",
                    "operation_id",
                    "outcome",
                    "error_code",
                    "retryable",
                } or not isinstance(receipt.get("error_code"), str) or type(
                    receipt.get("retryable")
                ) is not bool:
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                failed[step_identity] = receipt
            else:
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
        ordered_committed = [step["step_id"] for step in steps if step["step_id"] in committed]
        failed_step: str | None = None
        retryable = False
        for step in steps:
            if step["step_id"] in failed and step["step_id"] not in committed:
                failed_step = step["step_id"]
                retryable = bool(failed[failed_step].get("retryable", False))
                break
        if len(ordered_committed) == len(steps):
            phase = "completed"
            next_action = None
        elif failed:
            phase = "partial" if committed else "failed"
            next_action = "resume" if retryable else ("propose-compensation" if committed else None)
        elif approval is None:
            phase = "proposed"
            next_action = "apply"
        else:
            phase = "approved" if not committed else "executing"
            next_action = "resume"
        return {
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": phase,
            "committed_steps": ordered_committed,
            "failed_step": failed_step,
            "retryable": retryable,
            "receipts": receipts,
            "next_action": next_action,
        }

    @staticmethod
    def _blocked_reconstruction(
        run_identity: str,
        identity: str,
        fingerprint: str,
        receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": "blocked",
            "error_code": "CURATION_OUTCOME_UNCERTAIN",
            "committed_steps": [],
            "failed_step": None,
            "retryable": False,
            "receipts": receipts,
            "next_action": None,
        }


class CompensationStore(CurationStore):
    """Artifact view for one immutable compensation plan inside a forward run."""

    def __init__(self, vault_root: Path, run_identity: str, compensation_plan_id: str):
        super().__init__(vault_root)
        if not _HEX64.fullmatch(str(compensation_plan_id)):
            raise _error("INVALID_PLAN_ID", "compensation plan id is invalid")
        self.forward_run_id = run_identity
        self.compensation_plan_id = compensation_plan_id
        self.base = super().compensation_root(run_identity) / compensation_plan_id

    def plan_path(self, _run_identity: str) -> Path:
        return self.base / "plan.json"

    def approval_path(self, _run_identity: str) -> Path:
        return self.base / "approval.json"

    def identity_path(self, _run_identity: str) -> Path:
        return self.base / "identity.json"

    def state_path(self, _run_identity: str) -> Path:
        return self.base / "state.json"

    def receipts_dir(self, _run_identity: str) -> Path:
        return self.base / "receipts"

    def evidence_dir(self, _run_identity: str) -> Path:
        return self.base / "evidence"

    def prepared_dir(self, _run_identity: str) -> Path:
        return self.base / "prepared"

    def load_plan(self, run_identity: str) -> dict[str, Any]:
        stored = super().load_plan(run_identity)
        if stored.get("plan_type") != "compensation":
            raise _error("CURATION_PLAN_CORRUPT", "compensation plan type is invalid")
        if plan_id(stored) != self.compensation_plan_id:
            raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "compensation plan bytes changed")
        return stored


def compensation_descriptors(
    forward_steps: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "forward_step_id": str(step["step_id"]),
            "kind": compensation_kind(str(step["kind"])),
        }
        for step in reversed(list(forward_steps))
    ]


def _compensation_supersession_args(
    *, current_path: str, preimage: str, step_id: str
) -> dict[str, Any]:
    from .vault import parse_frontmatter

    frontmatter, body, _raw = parse_frontmatter(preimage, strict=True)
    note_type = str(frontmatter.get("type") or "")
    title = str(frontmatter.get("title") or "").strip()
    if (
        note_type
        not in {
            "research-note",
            "insight",
            "failure",
            "pattern",
            "experiment",
            "production-log",
        }
        or not title
    ):
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "sealed preimage cannot be restored through replace_memory",
        )
    args: dict[str, Any] = {
        "old_path": current_path,
        "content": body,
        "title": title,
        "slug": f"compensate-{hashlib.sha256(step_id.encode()).hexdigest()[:16]}",
        "note_type": note_type,
        "reason": f"History-preserving compensation for {step_id}.",
    }
    for key in (
        "project",
        "projects",
        "sources",
        "tags",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
    ):
        if frontmatter.get(key) is not None:
            args[key] = frontmatter[key]
    return args


def _compensation_args(
    forward_step: Mapping[str, Any],
    binding: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    kind = str(forward_step["kind"])
    inverse = compensation_kind(kind)
    effect = dict(receipt.get("effect") or {})
    if kind in {"create-note", "create-entity"}:
        path = str(effect.get("path") or binding["postcondition"]["path"])
        return inverse, {"path": path, "confirm": True}
    if kind == "delete":
        trash_path = effect.get("trash_path")
        if not isinstance(trash_path, str):
            raise _error(
                "CURATION_COMPENSATION_UNAVAILABLE",
                "delete receipt does not seal its exact trash identity",
            )
        return inverse, {"trash_path": trash_path, "restore_path": binding["path"]}
    if kind == "move":
        args = forward_step["args"]
        return inverse, {
            "old_path": args["new_path"],
            "new_path": args["old_path"],
            "update_wikilinks": args.get("update_wikilinks", True),
            "allow_curated": args.get("allow_curated", False),
        }
    if kind == "recover":
        path = effect.get("restored_path") or binding.get("prepared", {}).get("destination")
        if not isinstance(path, str):
            raise _error(
                "CURATION_COMPENSATION_UNAVAILABLE",
                "recovery receipt does not seal its restored path",
            )
        return inverse, {"path": path, "confirm": True}
    current_path = str(
        effect.get("new_path") or effect.get("path") or binding["postcondition"]["path"]
    )
    preimage = binding.get("preimage")
    if not isinstance(preimage, str):
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE", "authored-content step lacks a sealed preimage"
        )
    return inverse, _compensation_supersession_args(
        current_path=current_path,
        preimage=preimage,
        step_id=str(forward_step["step_id"]),
    )


def propose_compensation(vault_root: Path, *, run_id: str) -> dict[str, Any]:
    root = Path(vault_root)
    forward_store = CurationStore(root)
    forward_plan = forward_store.load_plan(run_id)
    forward_identity, _forward_fingerprint = forward_store.identities(run_id)
    state = forward_store.reconstruct(run_id)
    committed = _committed_step_ids(state)
    if not committed:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE", "no committed forward effect can be compensated"
        )
    receipts = {
        str(receipt.get("step_id")): receipt
        for receipt in state.get("receipts", [])
        if receipt.get("outcome") in {"committed", "recovered-committed"}
    }
    steps: list[dict[str, Any]] = []
    for ordinal in reversed(range(len(forward_plan["steps"]))):
        forward_step = forward_plan["steps"][ordinal]
        if forward_step["step_id"] not in committed:
            continue
        binding = forward_plan["binding_manifest"][ordinal]
        receipt = receipts.get(forward_step["step_id"])
        if receipt is None:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "committed step receipt is missing")
        operation_identity = operation_id(
            forward_identity, ordinal, forward_step["step_id"]
        )
        witness = _validate_witness(
            forward_store._read_json(
                forward_store.evidence_dir(run_id) / f"{operation_identity}.json"
            ),
            run_identity=run_id,
            plan_identity=forward_identity,
            ordinal=ordinal,
            step=forward_step,
            operation_identity=operation_identity,
            binding=binding,
        )
        if receipt.get("result_digest") != witness["result_digest"]:
            raise _error(
                "CURATION_OUTCOME_UNCERTAIN", "forward receipt no longer matches its witness"
            )
        _verify_live_postcondition(root, witness)
        inverse, args = _compensation_args(forward_step, binding, receipt)
        steps.append(
            {
                "step_id": f"comp-{len(steps):03d}-{forward_step['step_id']}",
                "kind": inverse,
                "args": args,
            }
        )
    plan = validate_forward_plan(
        {
            "version": 1,
            "title": f"Compensation for {run_id}",
            "steps": steps,
        }
    )
    manifest = [_prepare_step(root, step, ordinal) for ordinal, step in enumerate(plan["steps"])]
    _require_plan_relocation_history(root, plan)
    registries = registry_identities(root)
    sealed = {
        **plan,
        "plan_type": "compensation",
        "forward_plan_id": forward_identity,
        "binding_manifest": manifest,
        "registry_ids": registries,
    }
    identity = plan_id(sealed)
    store = CompensationStore(root, run_id, identity)
    store._create_plan_at(store.plan_path(run_id), sealed)
    fingerprint = plan_fingerprint(
        {
            key: value
            for key, value in sealed.items()
            if key not in {"binding_manifest", "registry_ids"}
        },
        manifest,
        registries,
    )
    projection = {
        "version": 1,
        "run_id": run_id,
        "plan_id": identity,
        "plan_fingerprint": fingerprint,
        "forward_plan_id": forward_identity,
        "phase": "proposed",
        "active_step": None,
    }
    if not store.state_path(run_id).exists():
        store._write_json(store.state_path(run_id), projection, create_only=True)
    return {**projection, "plan": sealed}


def apply_compensation(
    vault_root: Path,
    *,
    run_id: str,
    plan_id: str,
    expected_plan_fingerprint: str,
    why: str,
) -> dict[str, Any]:
    root = Path(vault_root)
    store = CompensationStore(root, run_id, plan_id)
    plan = store.load_plan(run_id)
    identity, fingerprint = store.identities(run_id)
    if identity != plan_id or fingerprint != expected_plan_fingerprint:
        raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "compensation approval identity is stale")
    _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
    _require_plan_relocation_history(root, plan)
    blockers = _blockers_for_uncommitted(root, plan, set())
    if blockers:
        raise _error(blockers[0]["code"], blockers[0]["reason"])
    store.create_approval(
        run_id,
        plan_id=plan_id,
        fingerprint=expected_plan_fingerprint,
        why=why,
        compensation_plan_id=plan_id,
    )
    return _execute_next(
        root,
        run_id,
        plan_id,
        store=store,
        compensation=True,
    )


__all__ = [
    "CURATION_ACTIONS",
    "READ_ONLY_ACTIONS",
    "STEP_KINDS",
    "MAX_PLAN_BYTES",
    "CurationError",
    "CurationStore",
    "canonical_json",
    "canonical_json_bytes",
    "normalize_target_path",
    "operation_id",
    "plan_fingerprint",
    "valid_replay_result",
    "plan_id",
    "run_id",
    "validate_forward_plan",
]
