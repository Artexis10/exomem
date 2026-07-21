"""Shared result/error envelope + argument coercion for the non-MCP surfaces.

The REST facade and the CLI (`--json` mode) speak ONE envelope shape — a single
command contract — so a script gets the same machine-readable result whichever
door it knocks on:

    success: {"success": true,  "data": <result>}
    failure: {"success": false, "error": {"code", "message", "remediation"}}

`coerce` turns a raw arg mapping (a REST JSON body or CLI strings) into the leaf's
kwargs using the registry `Param` specs — rejecting unknown params and re-running
the base64 binary-blob guard on text fields, the same boundary the MCP middleware
enforces. MCP keeps its own native error path (a raised `ValueError`); this module
is only for REST + CLI.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from . import guards

if TYPE_CHECKING:
    from .commands import Param


# A leaf-contract error string is "CODE: human reason …" (uppercase code prefix).
_CODE_PREFIX = re.compile(r"^([A-Z][A-Z0-9_]+):\s*(.*)$", re.DOTALL)

# Stable, machine-readable codes → a one-line remediation. Only the codes worth a
# pointer are listed; everything else carries `remediation: null`.
_REMEDIATION: dict[str, str] = {
    "UNKNOWN_PARAM": (
        "Remove the unknown field; run with --help or see /api/openapi.json for the "
        "accepted params."
    ),
    "MISSING_ARGUMENT": "Provide the required argument; run the subcommand with --help.",
    "BAD_INT": "Pass an integer.",
    "BAD_BOOL": "Pass a boolean (true/false).",
    "BAD_JSON": "Pass valid JSON for this field.",
    "BINARY_BLOB_REJECTED": "Don't push binaries through text fields; use the /upload endpoint.",
    "NOT_FOUND": "Check the path; try `ask_memory` to locate it.",
    "ENTITY_AMBIGUOUS": (
        "Review the returned active candidates and reconcile the identity before creating."
    ),
    "STALE_PARENT_HASH": "Re-read the parent page and retry with its current content hash.",
    "STALE_UNIT_REFERENCE": (
        "Re-read the exact unit and retry with its current reference/fingerprint."
    ),
    "AMBIGUOUS_UNIT_REFERENCE": (
        "Re-read the parent and select one exact current unit reference."
    ),
    "COMPACT_RELATIONS_REQUIRE_RICH_KIND": (
        "Select an explicit governed rich kind or author a canonical note-level relation."
    ),
    "DRIFT_GUARDS_REQUIRED": (
        "Re-read the parent and exact unit, then pass both expected_hash and "
        "expected_fingerprint."
    ),
    "WRITER_LEASE_REQUIRED": (
        "Send the mutation to the current writer or retry after its lease expires."
    ),
    "WRITER_COORDINATOR_UNAVAILABLE": (
        "Check the coordinator URL, credentials, and service health; reads remain "
        "available."
    ),
    "WRITER_FENCED": "Retry the mutation on the current writer.",
    "MUTATION_BUSY": "Retry after the active vault mutation finishes.",
    "MUTATION_ACKNOWLEDGEMENT_PENDING": (
        "Retry with the same mutation identity; do not submit a revised payload."
    ),
    "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN": (
        "Do not rerun with a new identity; reconcile the committed mutation and retry "
        "only with the same identity."
    ),
    "MUTATION_LOCK_UNAVAILABLE": (
        "Check that the runtime state root is writable and supports host file locking."
    ),
    "INVALID_MEDIA_OPERATION": "Use operation=process, operation=status, or operation=retry.",
    "UNSUPPORTED_MEDIA": "Use a supported audio, video, image, document, or text-media extension.",
    "MEDIA_NOT_FOUND": "Check the governed Knowledge Base path and original filename.",
    "MEDIA_PATH_OUTSIDE_KB": "Choose a media artifact inside the governed Knowledge Base.",
    "MEDIA_PATH_ACCESS_DENIED": "Move the artifact to a writable governed subtree or update _access.yaml policy.",
}

# Error codes whose HTTP status is NOT the default 400.
_NOT_FOUND_CODES = frozenset(
    {"NOT_FOUND", "OLD_NOT_FOUND", "SOURCES_NOT_FOUND", "NOT_IN_TRASH"}
)
_CONFLICT_CODES = frozenset(
    {
        "ARTIFACT_EXISTS",
        "FILE_EXISTS",
        "DEST_EXISTS",
        "ENTITY_EXISTS",
        "ENTITY_AMBIGUOUS",
        "ALREADY_SUPERSEDED",
        "ALREADY_TRASHED",
        "STALE_EDIT",
        "STALE_PARENT_HASH",
        "STALE_UNIT_REFERENCE",
        "AMBIGUOUS_UNIT_REFERENCE",
        "STALE_CONTRACT",
        "CONTRACT_EXISTS",
        "WRITER_LEASE_REQUIRED",
        "WRITER_FENCED",
        "IDEMPOTENCY_KEY_REUSED",
        "IDEMPOTENCY_IN_PROGRESS",
        "BATCH_ROLLBACK_INCOMPLETE",
        "BATCH_CLEANUP_INCOMPLETE",
        "MUTATION_BUSY",
        "MUTATION_ACKNOWLEDGEMENT_PENDING",
        "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
        # Adoption Studio drift codes (add-adoption-studio): a stale plan/apply or a
        # proposal whose bound content changed since review is a conflict, not a
        # plain bad-request — the client's honest recourse is to re-scan/re-read,
        # not to resubmit the same payload.
        "ADOPTION_SOURCE_CHANGED",
        "PLAN_STALE",
        "REVIEW_ITEM_CHANGED",
    }
)


class OpError(Exception):
    """A structured operation error carrying a stable code + remediation.

    `str(OpError)` is "CODE: message" so it reads the same as the leaf-contract
    `ValueError`s when surfaced as plain text.
    """

    def __init__(
        self,
        code: str,
        message: str,
        remediation: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.remediation = remediation or _REMEDIATION.get(code)
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")

    def as_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }
        if self.details:
            payload.update(ok=False, error_code=self.code)
        payload.update(self.details)
        return payload

    def __str__(self) -> str:
        if self.details:
            return json.dumps(
                self.as_public_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return f"{self.code}: {self.message}"


def envelope(success: bool, data: Any = None, error: dict | None = None) -> dict:
    """Build the shared envelope. Success carries `data`; failure carries `error`."""
    if success:
        return {"success": True, "data": data}
    return {"success": False, "error": error or {}}


def error_dict(exc: Exception) -> dict:
    """Convert any raised error into the envelope's `error` block.

    `OpError` carries its fields directly; a leaf-contract `ValueError`
    ("CODE: reason") is parsed into {code, message}; anything else is `INTERNAL`.
    """
    public_dict = getattr(exc, "as_public_dict", None)
    if callable(public_dict):
        try:
            public_payload = public_dict()
        except Exception:  # noqa: BLE001 - fall through to the shared parser
            public_payload = None
        if isinstance(public_payload, dict):
            return public_payload
    if isinstance(exc, OpError):
        return {"code": exc.code, "message": exc.message, "remediation": exc.remediation}
    text = str(exc)
    if isinstance(exc, (ValueError, TypeError, RuntimeError)):
        m = _CODE_PREFIX.match(text)
        if m:
            code, message = m.group(1), m.group(2).strip()
            return {"code": code, "message": message, "remediation": _REMEDIATION.get(code)}
        return {"code": "OP_ERROR", "message": text, "remediation": None}
    return {"code": "INTERNAL", "message": text, "remediation": None}


def http_status_for(code: str) -> int:
    """HTTP status for an error code (REST). Defaults to 400."""
    if code in _NOT_FOUND_CODES or code.endswith("_NOT_FOUND"):
        return 404
    if code in _CONFLICT_CODES or code.endswith("_EXISTS"):
        return 409
    if code in {"WRITER_COORDINATOR_UNAVAILABLE", "MUTATION_LOCK_UNAVAILABLE"}:
        return 503
    return 400


# --------------------------------------------------------------------------- #
# Argument coercion
# --------------------------------------------------------------------------- #
def _coerce_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
    raise OpError("BAD_BOOL", f"`{name}` must be a boolean, got {value!r}")


def _coerce_int(value: Any, name: str) -> int:
    if isinstance(value, bool):  # bool is an int subclass — reject the confusion
        raise OpError("BAD_INT", f"`{name}` must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise OpError("BAD_INT", f"`{name}` must be an integer, got {value!r}") from None


def _coerce_list(value: Any, name: str) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        # CLI convenience: comma-separated → list (empty string → empty list).
        return [item.strip() for item in value.split(",") if item.strip()]
    raise OpError("BAD_TYPE", f"`{name}` must be a list, got {value!r}")


def _coerce_json(value: Any, name: str, *, cli: bool = False) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            if cli:
                # CLI convenience: a bare unquoted string for a union/json field is
                # itself (`kb edit --value hello`), not malformed JSON. REST stays
                # strict-JSON — this fallback only fires on the CLI surface.
                return value
            raise OpError("BAD_JSON", f"`{name}` must be valid JSON: {e}") from None
    return value


def _coerce_dict(value: Any, name: str) -> dict:
    out = _coerce_json(value, name)
    if not isinstance(out, dict):
        raise OpError("BAD_JSON", f"`{name}` must be a JSON object")
    return out


def coerce(
    params: tuple[Param, ...],
    raw: dict,
    *,
    guarded_fields: tuple[str, ...] = (),
    tool: str = "",
    cli: bool = False,
) -> dict:
    """Coerce a raw arg mapping into leaf kwargs using the `Param` specs.

    - Drops keys whose value is `None` (treated as "not supplied" — let the leaf
      default apply), matching the previous REST `body.get(...)` behaviour.
    - Rejects any key that is not a declared param (`UNKNOWN_PARAM`).
    - Coerces each value by its declared type tag (works for JSON-native values
      from REST and for CLI strings alike).
    - Re-runs the base64 binary-blob guard on the declared text fields.

    `cli=True` relaxes one thing only: a union/`json` field whose raw string isn't
    valid JSON falls back to that string (so `kb edit --value hello` works without
    `--value '"hello"'`). REST keeps `cli=False` and stays strict-JSON.
    """
    spec = {p.name: p for p in params}
    unknown = [k for k in raw if k not in spec]
    if unknown:
        raise OpError(
            "UNKNOWN_PARAM",
            f"unknown parameter(s) for `{tool}`: {', '.join(sorted(unknown))}",
        )

    # Binary-blob guard FIRST (before any value is shipped to a leaf).
    for fld in guarded_fields:
        if fld in raw:
            guards.guard_text_content(raw.get(fld), tool=tool, field=fld)
    # `edit`'s batch mode carries each write payload in edits[].new_string — guard
    # those too (the top-level guarded_fields only covers new_body/new_string),
    # mirroring the MCP middleware so no surface lets a blob in through the nest.
    if tool in ("edit", "edit_memory") and isinstance(raw.get("edits"), list):
        for item in raw["edits"]:
            if isinstance(item, dict):
                guards.guard_text_content(
                    item.get("new_string"), tool=tool, field="edits[].new_string"
                )
    if tool == "edit_memory" and isinstance(raw.get("operation"), dict):
        operation = raw["operation"]
        for field in ("new_body", "new_string"):
            guards.guard_text_content(
                operation.get(field), tool=tool, field=f"operation.{field}"
            )
        for item in operation.get("edits") or []:
            if isinstance(item, dict):
                guards.guard_text_content(
                    item.get("new_string"),
                    tool=tool,
                    field="operation.edits[].new_string",
                )

    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        if value is None:
            continue
        p = spec[name]
        if p.type == "bool":
            kwargs[name] = _coerce_bool(value, name)
        elif p.type == "int":
            kwargs[name] = _coerce_int(value, name)
        elif p.type == "list[str]":
            kwargs[name] = _coerce_list(value, name)
        elif p.type == "dict":
            kwargs[name] = _coerce_dict(value, name)
        elif p.type == "json":
            kwargs[name] = _coerce_json(value, name, cli=cli)
        else:  # "str"
            kwargs[name] = value if isinstance(value, str) else str(value)
    return kwargs
