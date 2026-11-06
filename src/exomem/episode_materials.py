"""Bounded material bindings over retained inputs and native original recovery."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from . import artifact_recovery, originals, provenance, query_data, reserved_paths, retained_inputs
from . import episode_model as model
from .governance import egress
from .governance.principal import effective_principal

MAX_BYTES = 16 * 1024
MAX_ITEMS = 8
MAX_ROWS = 32

# These schemas fix the v1 wire and journal fields, never a knowledge vocabulary.
_REFERENCE = {"type": "string", "minLength": 1, "maxLength": 2048}
_ATTRIBUTION = {
    "producer": {"type": "string", "minLength": 1, "maxLength": 256},
    "producer_version": {"type": "string", "minLength": 1, "maxLength": 160},
}
_LOCATOR = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "record_path", "row_indices"],
    "properties": {
        "kind": {"const": "dataset_rows"},
        "record_path": {"type": ["string", "null"], "minLength": 1, "maxLength": 2048},
        "row_indices": {
            "type": "array", "minItems": 1, "maxItems": MAX_ROWS, "uniqueItems": True,
            "items": {"type": "integer", "minimum": 0, "maximum": query_data.MAX_PARSED_ROWS - 1},
        },
    },
}
# SHA-256 is the existing binary/material descriptor's fixed lowercase hex format.
_DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_BINARY = {"sha256": _DIGEST, "size": {"type": "integer", "minimum": 0}}
_ITEM_PROPERTIES = {"key": {"type": "string", "minLength": 1, "maxLength": 160}, **_ATTRIBUTION}


def _envelope_schema(properties: dict, alternatives: tuple[str, str]) -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["version", "items"],
        "properties": {
            "version": {"type": "integer", "const": 1},
            "items": {
                "type": "array", "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["key"],
                    "properties": {**_ITEM_PROPERTIES, **properties},
                    "anyOf": [{"required": [field]} for field in alternatives],
                },
            },
        },
    }


ARGUMENT_SCHEMA = _envelope_schema({
    "original_reference": _REFERENCE, "reading_reference": _REFERENCE, "locator": _LOCATOR,
}, ("original_reference", "reading_reference"))
_SEALED_SCHEMA = _envelope_schema({
    "original": {
        "type": "object", "additionalProperties": False,
        "required": ["reference", "sha256", "size"],
        "properties": {"reference": _REFERENCE, **_BINARY},
    },
    "reading": {
        "type": "object", "additionalProperties": False, "required": ["reference", "version"],
        "properties": {
            "reference": _REFERENCE, "version": _DIGEST, "unit_fingerprint": _DIGEST,
            "span": {
                "type": "object", "additionalProperties": False,
                "required": ["start_offset", "end_offset"],
                "properties": {
                    "start_offset": {"type": "integer", "minimum": 0},
                    "end_offset": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    "locator": {
        **_LOCATOR,
        "required": [*_LOCATOR["required"], "sha256", "size", "record_kind"],
        "properties": {
            **_LOCATOR["properties"], **_BINARY,
            "record_kind": {"type": "string", "minLength": 1, "maxLength": 80},
        },
    },
}, ("original", "reading"))


def _invalid(reason: str) -> model.EpisodeError:
    return model.EpisodeError("EPISODE_MATERIAL_INVALID", reason)


def normalize(value: Any, *, sealed: bool = False) -> dict[str, Any]:
    """Validate the closed envelope and normalize identity order, without I/O."""
    from jsonschema import Draft202012Validator, ValidationError

    try:
        if len(model._json(value).encode("utf-8")) > MAX_BYTES:  # noqa: SLF001
            raise _invalid("materials exceeds 16 KiB")
        Draft202012Validator(_SEALED_SCHEMA if sealed else ARGUMENT_SCHEMA).validate(value)
        result = model._copy(value)  # noqa: SLF001
        if type(result["version"]) is not int:
            raise _invalid("materials version must be integer 1")
        keys = [item["key"] for item in result["items"]]
        if any(not key.strip() for key in keys) or len(set(keys)) != len(keys):
            raise _invalid("materials needs unique nonempty keys")
        for item in result["items"]:
            for field in (("original", "reading") if sealed else ("original_reference", "reading_reference")):
                if field not in item:
                    continue
                reference = item[field]["reference"] if sealed else item[field]
                _, unit = retained_inputs.parse_reference(reference)
                if field.startswith("original") and unit is not None:
                    raise _invalid("original_reference must name a whole companion page")
                if sealed and field == "reading":
                    reading = item[field]
                    if any((unit is not None) != (name in reading) for name in ("unit_fingerprint", "span")):
                        raise _invalid("reading unit binding is incomplete")
                    if "span" in reading and reading["span"]["start_offset"] >= reading["span"]["end_offset"]:
                        raise _invalid("reading span is invalid")
                    if "span" in reading and any(type(offset) is not int for offset in reading["span"].values()):
                        raise _invalid("reading offsets must be integers")
                if sealed and field == "original" and type(item[field]["size"]) is not int:
                    raise _invalid("original size must be an integer")
            if "locator" in item:
                original = "original" if sealed else "original_reference"
                if original not in item:
                    raise _invalid("dataset_rows requires an original_reference")
                locator = item["locator"]
                if any(type(index) is not int for index in locator["row_indices"]):
                    raise _invalid("dataset row indices must be integers")
                locator["row_indices"].sort()
                if sealed and any(locator[field] != item[original][field] for field in _BINARY):
                    raise _invalid("dataset locator differs from its original")
        result["items"].sort(key=lambda item: item["key"])
        return result
    except (ValidationError, retained_inputs.RetainedInputError) as error:
        raise _invalid("materials fields or references are invalid") from error


def _reading(snapshot: retained_inputs.RetainedInput) -> dict[str, Any]:
    result = {
        "reference": snapshot.unit_ref or snapshot.canonical,
        "version": provenance.evidence_version(snapshot.page.content),
    }
    if snapshot.unit is not None:
        result.update(unit_fingerprint=snapshot.unit.fingerprint, span={
            "start_offset": snapshot.unit.span.start_offset,
            "end_offset": snapshot.unit.span.end_offset,
        })
    return result


def _dataset(held: reserved_paths.GenericFileCopy, descriptor: dict, locator: dict) -> dict:
    if held.size > query_data.MAX_FILE_BYTES:
        raise _invalid("dataset exceeds the parser byte limit")
    raw = reserved_paths.read_held(held, 0, held.size)
    proof: dict[str, Any] = {}
    try:
        _, rows, _, _ = query_data.load_rows_bytes(
            raw, Path(descriptor["path"]).suffix, locator["record_path"], provenance=proof,
        )
    except query_data.QueryDataError as error:
        raise _invalid(f"dataset locator cannot be verified: {error.reason}") from error
    if any(index >= len(rows) for index in locator["row_indices"]):
        raise _invalid("dataset row selection is outside the parsed records")
    return {"kind": "dataset_rows", **proof, "row_indices": locator["row_indices"]}


def seal(vault_root: Path, value: Any) -> dict[str, Any]:
    result = normalize(value)
    for item in result["items"]:
        for field in ("original_reference", "reading_reference"):
            if field not in item:
                continue
            snapshot = retained_inputs.resolve_retained_input(vault_root, item.pop(field))
            if snapshot.released is None or snapshot.released.get("frontmatter") != snapshot.page.frontmatter:
                raise retained_inputs.RetainedInputError("RETAINED_INPUT_UNAVAILABLE", "input is unavailable")
            if field == "original_reference":
                binary = originals.binary_binding(snapshot.page.frontmatter)
                found = originals.bound_companion(vault_root, binary.path) if binary is not None else None
                if (
                    binary is None or found is None or found[0] != snapshot.page.path
                    or found[1] != snapshot.page.frontmatter
                ):
                    raise _invalid("original_reference needs a canonical binary companion")
                item["original"] = {"reference": snapshot.canonical, "sha256": binary.sha256, "size": binary.size}
                if "locator" in item:
                    who = replace(effective_principal(), administrative_ingress=False)
                    with artifact_recovery.stage(vault_root, snapshot.page.path, who) as (held, descriptor):
                        item["locator"] = _dataset(held, descriptor, item["locator"])
                        artifact_recovery.require_current(vault_root, held, who)
            else:
                item["reading"] = _reading(snapshot)
    return normalize(result, sealed=True)


def handles(vault_root: Path, state: dict) -> list[dict]:
    binding = model.material_binding(state)
    if binding is None:
        return []
    revision, envelope = binding
    references = [
        item[field]["reference"].partition("#")[0]
        for item in envelope["items"] for field in ("original", "reading") if field in item
    ]
    visible = egress.visible_memory_refs(vault_root, references, principal=effective_principal())
    return [
        {"input_revision": revision, "material_key": item["key"], "recovery_current": "unchecked"}
        for item in envelope["items"]
        if any(item[field]["reference"].partition("#")[0] in visible for field in ("original", "reading") if field in item)
    ]


def recover_original(vault_root: Path, item: dict) -> dict:
    original = item.get("original")
    if original is None:
        return {"status": "unavailable"}
    try:
        snapshot = retained_inputs._read_snapshot(vault_root, original["reference"])  # noqa: SLF001
        who = replace(effective_principal(), administrative_ingress=False)
        with artifact_recovery.stage(vault_root, snapshot.page.path, who) as (held, descriptor):
            if any(descriptor[field] != original[field] for field in _BINARY):
                return {"status": "stale"}
            result = {"status": "available", "reference": original["reference"], **descriptor}
            if "locator" in item:
                locator = _dataset(held, descriptor, item["locator"])
                if locator != item["locator"]:
                    return {"status": "stale"}
                result["locator"] = locator
            artifact_recovery.require_current(vault_root, held, who)
            return result
    except (artifact_recovery.RecoveryUnavailable, retained_inputs.RetainedInputError,
            query_data.QueryDataError, reserved_paths.ReservedPathLeafError, model.EpisodeError):
        return {"status": "unavailable"}


def recover_reading(vault_root: Path, item: dict, *, max_bytes: int) -> dict:
    reading = item.get("reading")
    if reading is None:
        return {"status": "unavailable"}
    try:
        canonical, unit_ref = retained_inputs.parse_reference(reading["reference"])
        snapshot = retained_inputs.resolve_retained_input(vault_root, canonical)
        if snapshot.released is None or snapshot.released.get("frontmatter") != snapshot.page.frontmatter:
            return {"status": "unavailable"}
        if provenance.evidence_version(snapshot.page.content) != reading["version"]:
            return {"status": "stale"}
        if unit_ref is not None:
            snapshot = retained_inputs._release_snapshot(vault_root, replace(snapshot, unit_ref=unit_ref))  # noqa: SLF001
        if _reading(snapshot) != reading:
            return {"status": "stale"}
        value = snapshot.unit.span.text if snapshot.unit is not None else snapshot.page.body
        if len(value.encode("utf-8")) > max_bytes or not retained_inputs.exact_text_visible(vault_root, value):
            return {"status": "unavailable"}
        retained_inputs.recheck_retained_inputs(vault_root, ((snapshot, value),))
        field, representation = ("text", "semantic_unit_span") if snapshot.unit is not None else ("body", "page_body")
        egress.record_direct_text_release(
            value, stable_ref=reading["reference"], representation=representation,
            principal=effective_principal(), authorization=snapshot.authorization,
        )
        return {"status": "available", **reading, "representation": representation, field: value}
    except (retained_inputs.RetainedInputError, ValueError, OSError):
        return {"status": "unavailable"}
