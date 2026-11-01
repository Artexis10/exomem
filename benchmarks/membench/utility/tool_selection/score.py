"""Scoring one session: task success from the end state, and the secondary readouts.

Primary: a write task succeeds when the vault's collections end exactly as the
case's key says (rows matched on the collection's natural key, every asserted
field equal, nothing else changed); a read task succeeds when its JSON answer
equals the answer computed from the seeded facts and nothing was written. Both
are exact checks on structured values. Nothing here reads the session's prose.

Secondary: the first call that resolved to a key-accepted operation, calls per
task, argument refusals, schema bytes, input tokens, and writes during read
tasks. Calls reach those readouts through the injected resolver (see
``resolve``), so the same code scores every tool shape.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from exomem.cli_ops import INVALID_OPERATION_ARGUMENTS
from membench.utility.switch_eval.tap import tool_payloads

from .cases import Case
from .resolve import Resolver, resolve_call
from .templates import READ, WRITE, CollectionKey, Key, ManifestKey
from .vault import authored_unit, markdown_facts

#: Refusal codes of the Records and Planning argument envelope: the product's
#: closed set for a call whose arguments the selected action does not accept.
# Closed refusal codes from the corresponding commands' argument-validation boundaries.
ARGUMENT_REFUSALS = frozenset({
    "INVALID_RECORD_ARGUMENTS", "INVALID_PLAN_ARGUMENTS", "INVALID_PREFERENCE_ARGUMENTS",
    "INVALID_REVIEW_ARGUMENTS", "INVALID_REVIEW_CONTEXT_ARGUMENTS", "INVALID_SCHEMA_ARGUMENT",
    "INVALID_SCHEMA_OPERATION", "INVALID_SCHEMA_SUBJECT", "INVALID_OBSERVE_OPERATION",
    "INVALID_FILE_OPERATION", INVALID_OPERATION_ARGUMENTS,
})

#: Outcomes whose end state is scorable: the session ran and the agent acted
#: (or failed to). A harness fault is retried and a struck session is not run.
SCORABLE_OUTCOMES = frozenset({"ok", "actor_failure"})


# --------------------------------------------------------------------------
# Answers.
# --------------------------------------------------------------------------


def parse_answer(text: str | None) -> Any:
    """R2-S1: one complete JSON root object, optionally inside one whole JSON fence."""
    if not text:
        return None
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError:
        tokens = MarkdownIt("commonmark").parse(text.strip())
        # `fence` and `json` are the maintained parser token and the declared output syntax.
        if len(tokens) != 1 or tokens[0].type != "fence" or tokens[0].info.strip() != "json":
            return None
        try:
            value = json.loads(tokens[0].content)
        except json.JSONDecodeError:
            return None
    return value.get("answer") if isinstance(value, dict) else None


def answer_matches(answer: Any, key: Key) -> bool:
    expected = key.answer
    if key.answer_mode == "number":
        return (
            isinstance(answer, (int, float))
            and not isinstance(answer, bool)
            and abs(float(answer) - float(expected)) < 1e-9
        )
    if key.answer_mode == "set":
        return (
            isinstance(answer, list)
            and all(isinstance(entry, str) for entry in answer)
            and len(answer) == len(expected)
            and {entry.strip() for entry in answer} == set(expected)
        )
    if key.answer_mode == "text":
        return isinstance(answer, str) and answer.strip().casefold() == str(expected).strip().casefold()
    if key.answer_mode == "structured":
        return type(answer) is type(expected) and json.dumps(answer, sort_keys=True) == json.dumps(expected, sort_keys=True)
    raise ValueError(f"unknown answer mode: {key.answer_mode!r}")


# --------------------------------------------------------------------------
# End state.
# --------------------------------------------------------------------------


def _same(actual: Any, expected: Any) -> bool:
    numeric = (int, float)
    if (
        isinstance(actual, numeric)
        and isinstance(expected, numeric)
        and not isinstance(actual, bool)
        and not isinstance(expected, bool)
    ):
        return abs(float(actual) - float(expected)) < 1e-9
    return str(actual) == str(expected)


def _identity(row: Mapping[str, Any], natural_key: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in natural_key)


def check_collection(key: CollectionKey, state: Mapping) -> list[str]:
    collection = state["collections"].get(key.manifest)
    if collection is None:
        return [f"collection {key.manifest} is missing"]
    actual: dict[tuple[str, ...], list[Mapping]] = {}
    for row in collection["rows"]:
        actual.setdefault(_identity(row, key.natural_key), []).append(row)
    reasons: list[str] = []
    expected_ids = set()
    for expected in key.rows:
        identity = _identity(expected, key.natural_key)
        expected_ids.add(identity)
        found = actual.get(identity, [])
        if len(found) != 1:
            reasons.append(f"{len(found)} rows for {identity}, expected 1")
            continue
        reasons.extend(
            f"{identity}.{name}: {found[0].get(name)!r} != {value!r}"
            for name, value in expected.items()
            if not _same(found[0].get(name), value)
        )
    reasons.extend(f"unexpected row {identity}" for identity in actual if identity not in expected_ids)
    return reasons


def check_manifest(key: ManifestKey, state: Mapping) -> list[str]:
    collection = state["collections"].get(key.manifest)
    if collection is None:
        return [f"collection {key.manifest} is missing"]
    reasons = [
        f"{name}: {collection[name]!r} != {expected!r}"
        for name, expected in (
            ("profile", key.profile),
            ("title", key.title),
            ("natural_key", list(key.natural_key)),
        )
        if collection[name] != expected
    ]
    for name, types in key.fields.items():
        declared = collection["fields"].get(name)
        if declared is None:
            reasons.append(f"field {name} is not declared")
        elif declared["type"] not in types:
            reasons.append(f"field {name} has type {declared['type']!r}, not one of {list(types)}")
        elif name in key.required and not declared["required"]:
            reasons.append(f"field {name} is not required")
    return reasons


def check_end_state(key: Key, before: Mapping, after: Mapping) -> list[str]:
    """Why the end state differs from the key; an empty list means it matches.

    A collection the key does not name must end as it began, so a write to the
    wrong collection fails the task.
    """

    named = {collection.manifest for collection in key.collections}
    named |= {manifest.manifest for manifest in key.manifests}
    reasons: list[str] = []
    for collection in key.collections:
        reasons.extend(check_collection(collection, after))
    for manifest in key.manifests:
        reasons.extend(check_manifest(manifest, after))
    for path in sorted((set(before["collections"]) | set(after["collections"])) - named):
        if before["collections"].get(path) != after["collections"].get(path):
            reasons.append(f"collection {path} changed but the task did not name it")
    reasons.extend(_canonical_checks(key, before, after, final=True))
    return reasons


def state_changed(before: Mapping, after: Mapping) -> bool:
    return canonical_data(before) != canonical_data(after)


def canonical_data(state: Mapping) -> dict:
    return {name: value for name, value in state.items() if name not in ("evidence", "unknown")}


def _read_audit_ids(previous: Mapping, current: Mapping, call: Mapping, resolved) -> set[tuple]:
    """Match one verified observational receipt to this served read and its source-defined scrubber marker."""
    from exomem.governance.principal import OWNER_AUDIENCE
    from exomem.governance.scrubber import NOTICE

    if call["outcome"] != "served" or resolved.read_only is not True:
        return set()
    prior = previous.get("governance", {}).get("records", [])
    actual = current.get("governance", {}).get("records", [])
    if any(record not in actual for record in prior):
        return set()
    added = [record for record in actual if record not in prior]
    if len(added) != 1:
        return set()
    record = added[0]
    result = call.get("result") or {}
    response = result.get("structuredContent", result)
    # NOTICE is the scrubber's wire intervention marker, not a judgement about returned prose.
    if (record.get("event_type") != "credential_block" or record.get("phase") != "recorded"
            or record.get("durable") is not False or record.get("command") != resolved.command
            or record.get("principal") != OWNER_AUDIENCE or record.get("audience") != OWNER_AUDIENCE
            or NOTICE not in json.dumps(response, ensure_ascii=False)):
        return set()
    return {(record["instance_id"], record["seq"], record["hash"])}


def _quality_state(state: Mapping, baseline: Mapping, allowed_read_audit: set[tuple]) -> Mapping:
    """Separate only matched, append-only read audit from data effects; retain the raw snapshot unchanged."""
    from exomem.governance import receipts, store

    prior, actual = baseline.get("governance", {}), state.get("governance", {})
    if actual.get("status") != "known" or not allowed_read_audit:
        return state
    old_records, new_records = prior.get("records", []), actual.get("records", [])
    if any(record not in new_records for record in old_records):
        return state
    additions = [record for record in new_records if record not in old_records]
    if not additions or any((record["instance_id"], record["seq"], record["hash"]) not in allowed_read_audit
                            for record in additions):
        return state
    old_tables, new_tables = prior.get("tables", {}), actual["tables"]
    # These three tables are the receipt owner's bookkeeping schema; all authorization/lifecycle rows stay visible.
    bookkeeping = {"meta", "receipt_instance", "receipts_head"}
    if any(old_tables.get(table, []) != new_tables.get(table, [])
           for table in (set(old_tables) | set(new_tables)) - bookkeeping):
        return state
    if prior.get("status") == "known":
        if prior["schema_version"] != actual["schema_version"] or prior["schema"] != actual["schema"]:
            return state
        if old_tables.get("meta", []) != new_tables.get("meta", []):
            return state
    else:
        # A first read receipt may initialize only the source-validated v3 store, never a v4 migration.
        if actual["schema_version"] != store.SCHEMA_USER_VERSION:
            return state
        meta = new_tables.get("meta", [])
        if (len(meta) != 1 or set(meta[0]) != {"key", "value"}
                or meta[0]["key"] != "instance" or type(meta[0]["value"]) is not int):
            return state
    instance = additions[-1]["instance_id"]
    old_instance = old_tables.get("receipt_instance", [])
    if new_tables.get("receipt_instance") != (old_instance or [{"singleton": 1, "instance_id": instance}]):
        return state
    heads = {row["instance_id"]: dict(row) for row in old_tables.get("receipts_head", [])}
    for record in additions:
        identity = record["instance_id"]
        head = heads.setdefault(identity, {"instance_id": identity, "durable_seq": 0,
                                          "durable_hash": receipts.GENESIS_HASH, "path": "", "byte_offset": 0})
        head.update(observed_seq=record["seq"], observed_hash=record["hash"])
    if heads != {row["instance_id"]: row for row in new_tables.get("receipts_head", [])}:
        return state
    files = dict(state["files"])
    for path, records in actual["event_paths"].items():
        if records == prior.get("event_paths", {}).get(path):
            continue
        previous = baseline.get("evidence", {}).get("files", {}).get(path, {})
        current = state.get("evidence", {}).get("files", {}).get(path, {})
        if "base64" not in current or not base64.b64decode(current["base64"]).startswith(
            base64.b64decode(previous.get("base64", ""))
        ):
            return state
        if path in baseline["files"]:
            files[path] = baseline["files"][path]
        else:
            files.pop(path, None)
    return {**state, "files": files, "governance": prior}


def _source(state: Mapping, path: str) -> str:
    return base64.b64decode(state["evidence"]["files"][path]["base64"]).decode("utf-8")


def _review_without_registry_promotion(key: Key, before: Mapping, after: Mapping) -> Mapping:
    """Admit only the source-owned promotion for the independently requested registry keys."""
    from exomem import promotions, review_state
    from exomem.vocabulary import registry_specs

    actual = after.get("review", {})
    if key.registry is None:
        return actual
    prior = before.get("review", {}).get("promotions", {})
    ledger = actual.get("promotions", {})
    added = {ident: row for ident, row in ledger.items() if ident not in prior}
    subject = key.registry["subject"]
    spec = registry_specs()[subject]
    old_versions = before.get("registry_history", {}).get(subject, [])
    versions = [row for row in after.get("registry_history", {}).get(subject, []) if row not in old_versions]
    if len(added) != 1 or len(versions) != 1:
        return actual
    ident, row = next(iter(added.items()))
    version = versions[0]
    requested = key.registry["delta"]["upsert"]
    overlay = before["registry_paths"][subject]["overlay"]
    existing = before.get("files", {}).get(overlay, {}).get("value", {}).get(spec.name, {})
    keys = sorted(set(requested) - set(existing))
    if not keys or sorted(version.get("added", {}).get(spec.name, [])) != keys:
        return actual
    subjects = [{"registry": spec.name, "key": name, "introduced_version": version["version"],
                 "after_hash": after["evidence"]["files"][overlay]["sha256"]} for name in keys]
    # These ledger fields and the registry kind are fixed by promotions.record_registry/record.
    if (set(row) != {"kind", "subjects", "omitted", "new_family", "write", "creator", "created", "created_at"}
            or row.get("kind") != "registry" or row.get("subjects") != subjects or row.get("omitted") != 0
            or row.get("new_family") != ("parent" in spec.fields and any(requested[name].get("parent") is None for name in keys))
            or not isinstance(row.get("write"), str)
            or ident != review_state.item_id(f"{promotions.FAMILY}:registry:{row['write']}")):
        return actual
    trimmed = json.loads(json.dumps(actual))
    trimmed["promotions"].pop(ident)
    return trimmed


def _canonical_checks(key: Key, before: Mapping, after: Mapping, *, final: bool) -> list[str]:
    """Check declared changes and every undeclared canonical domain, including intermediate effects."""
    if "files" not in before and "files" not in after:
        return []  # Archived collection-only recordings remain readable under their original instrument.
    from exomem import (
        compiled_history,
        memory_refs,
        record_history,
        relation_review,
        semantic_contract,
    )
    from exomem.kbdir import kb_dirname

    reasons = []
    allowed = {item.path for item in key.files}
    log_targets = set(allowed)
    if key.adoption is not None:
        from .vault import adoption_runs

        prior_runs, actual_runs = adoption_runs(before), adoption_runs(after)
        if len(prior_runs) != key.adoption["run_count"] or set(prior_runs) != set(actual_runs):
            reasons.append("adoption planning replaced, removed or added a run")
        for path, prior in prior_runs.items():
            actual = actual_runs.get(path, {})
            allowed.add(path)
            # Only these source-owned plan fields change; inventory, selection and outcomes stay frozen.
            # The run's seal of filed Source names is apply bookkeeping that planning may refresh.
            changed_fields = {"updated", "phase", "plan", "plan_draft", "sources_seal"}
            if ({name: value for name, value in prior.items() if name not in changed_fields}
                    != {name: value for name, value in actual.items() if name not in changed_fields}):
                reasons.append("adoption planning changed the prepared run outside its plan")
            selection = actual.get("selection")
            if (not isinstance(selection, Mapping) or selection.get("paths") != key.adoption["selection_paths"]
                    or actual.get("outcomes") != key.adoption["outcomes"]):
                reasons.append("adoption planning changed the selected files or imported them")
            if final or actual != prior:
                from exomem.adoption_plan import item_digest
                from exomem.adoption_run import plan_id_for

                plan = actual.get("plan") or {}
                if not isinstance(plan, Mapping):
                    reasons.append("adoption plan is not a structured plan")
                    continue
                expected = key.adoption["plan"]
                selection_hash = prior.get("selection", {}).get("selection_hash")
                if (actual.get("phase") != key.adoption["phase"] or actual.get("plan_draft") is not None
                        or {name: value for name, value in plan.items()
                            if name not in ("created", "plan_id", "selection_hash")} != expected
                        or plan.get("selection_hash") != selection_hash
                        or plan.get("plan_id") != plan_id_for(selection_hash, [item_digest(item) for item in expected["items"]])):
                    reasons.append("adoption plan differs from the independent selection and item facts")
        for path, body in key.adoption["originals"].items():
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if any(state.get("evidence", {}).get("files", {}).get(path, {}).get("sha256") != digest
                   for state in (before, after)):
                reasons.append(f"adoption original {path} changed bytes")
    named = {item.manifest for item in (*key.collections, *key.manifests)}
    allowed_empty = set()
    for state in (before, after):
        for path in named:
            collection = state.get("collections", {}).get(path, {})
            allowed.add(path)
            if collection.get("source_path"):
                log_targets.add(collection["source_path"])
                allowed_empty.add(collection["source_path"])
    for key_collection in key.collections:
        prior_collection = before.get("collections", {}).get(key_collection.manifest, {})
        actual_collection = after.get("collections", {}).get(key_collection.manifest, {})
        prior_items = prior_collection.get("items", {})
        actual_items = actual_collection.get("items", {})
        for attribute in ("profile", "title", "natural_key", "fields"):
            if prior_collection.get(attribute) != actual_collection.get(attribute):
                reasons.append(f"collection {key_collection.manifest} changed its {attribute}")
        for item_key, item in {**prior_items, **actual_items}.items():
            desired = next((row for row in key_collection.rows if _identity(row, key_collection.natural_key)
                            == _identity(item["values"], key_collection.natural_key)), None)
            prior = prior_items.get(item_key)
            if desired is None or prior is not None and all(_same(prior["values"].get(name), value) for name, value in desired.items()):
                continue
            allowed.add(item["path"])
            log_targets.add(item["path"])
            if prior is not None:
                directory = record_history.history_dir(prior_collection["id"], item_key)
                for path, fact in after.get("files", {}).items():
                    if path.startswith(directory + "/") and path not in before.get("files", {}):
                        entry = fact.get("value", {})
                        if (entry.get("path") == prior["path"] and entry.get("prior", {}).get("values") == prior["values"]):
                            allowed.add(path)
    for item in key.files:
        prior = before.get("files", {}).get(item.path)
        actual = after.get("files", {}).get(item.path)
        if actual is None or prior is None:
            reasons.append(f"authored page {item.path} is missing")
            continue
        if final or actual != prior:
            if item.units is not None and actual.get("units") != list(item.units):
                reasons.append(f"semantic units in {item.path} do not match the requested facts")
            if item.units is not None and actual.get("remainder") != prior.get("remainder"):
                reasons.append(f"page remainder in {item.path} changed")
            if item.body is not None and actual.get("body") != markdown_facts(item.body):
                reasons.append(f"authored body in {item.path} does not match the requested facts")
            # The writer owns updated; every other frontmatter fact must survive this task.
            if {k: v for k, v in actual["frontmatter"].items() if k != "updated"} != {
                k: v for k, v in prior["frontmatter"].items() if k != "updated"
            }:
                reasons.append(f"frontmatter in {item.path} changed outside the request")
        identity = prior.get("frontmatter", {}).get("exomem_id")
        if identity:
            parent = memory_refs.memory_ref(identity)
            history = compiled_history.history_dir(parent)
            for path, fact in after.get("files", {}).items():
                if not path.startswith(history + "/") or path in before.get("files", {}):
                    continue
                entry = fact.get("value", {})
                if (entry.get("path") == item.path and entry.get("prior", {}).get("source") == _source(before, item.path)
                        and entry.get("before_hash") == hashlib.sha256(_source(before, item.path).encode()).hexdigest()
                        and entry.get("after_hash") == hashlib.sha256(_source(after, item.path).encode()).hexdigest()):
                    allowed.add(path)
            fingerprint = semantic_contract.review_content_fingerprint(identity, _source(after, item.path))
            for path in (relation_review.lifecycle_prepared_path(Path("."), identity),
                         relation_review.lifecycle_decision_path(Path("."), identity, fingerprint)):
                value = after.get("files", {}).get(path.as_posix(), {}).get("value", {})
                if value.get("page_identity") == identity and value.get("after_fingerprint") == fingerprint:
                    allowed.add(path.as_posix())
    if key.preference is not None:
        preference = after.get("preferences", {})
        if final or preference != before.get("preferences"):
            if any(preference.get(name) != key.preference[name] for name in ("stored", "contexts")):
                reasons.append("saved preferences differ from the requested context clear")
            try:
                receipt = uuid.UUID(preference.get("receipt_id", ""))
                if (receipt.version != 4 or preference["revision"] == before["preferences"]["revision"]
                        or preference["receipt_id"] == before["preferences"]["receipt_id"]):
                    reasons.append("preference change identity did not advance")
            except (ValueError, TypeError, AttributeError):
                reasons.append("preference change identity is invalid")
    elif before.get("preferences") != after.get("preferences"):
        reasons.append("preferences changed outside the request")
    if key.question is not None:
        prior = before.get("review", {})
        actual = after.get("review", {})
        old = prior.get("vocabulary", {}).get("items", {})
        new = actual.get("vocabulary", {}).get("items", {})
        added = [value for ref, value in new.items() if ref not in old]
        if final or actual != prior:
            if len(added) != key.question["new_exact_count"]:
                reasons.append("meaning-question count differs from the request")
            for value in added:
                question = key.question
                path = question["anchor_path"]
                digest = before["evidence"]["files"][path]["sha256"]
                if (any(value.get(name) != question[name] for name in ("family", "signal", "question"))
                        or set(value.get("paths", {}).values()) != {path}
                        or set(value.get("target_versions", {}).values()) != {digest}
                        or any(entry.get("version") != digest or entry.get("origin") != question["signal"]
                               for entry in value.get("evidence", []))):
                    reasons.append("meaning-question facts or page evidence differ from the seed")
            trimmed = json.loads(json.dumps(actual))
            trimmed.get("vocabulary", {})["items"] = {ref: value for ref, value in new.items() if ref in old}
            if trimmed != prior:
                reasons.append("review decisions or unrelated review state changed")
    elif before.get("review") != _review_without_registry_promotion(key, before, after):
        reasons.append("review state changed outside the request")
    if key.registry is not None:
        # RegistrySpec owns the selected overlay and history paths; no vocabulary keys live in the scorer.
        owner = after.get("registry_paths", {}).get(key.registry["subject"], {})
        overlay = owner.get("overlay", "unavailable")
        allowed.add(overlay)
        log_targets.add(overlay)
        history = owner.get("history", "unavailable")
        prior = before.get("files", {}).get(overlay, {}).get("value", {})
        actual = after.get("files", {}).get(overlay, {}).get("value", {})
        expected = json.loads(json.dumps(prior))
        expected.setdefault(key.registry["subject"], {}).update(key.registry["delta"]["upsert"])
        if (final or actual != prior) and actual != expected:
            reasons.append("registry delta changed facts outside the authored proposal")
        additions = [path for path in after.get("files", {}) if path.startswith(history + "/") and path not in before.get("files", {})]
        if (final or additions) and len(additions) != 1:
            reasons.append("registry save did not retain exactly one previous overlay")
        for path in additions:
            if after["files"][path].get("value") != prior:
                reasons.append("registry history does not preserve the previous overlay")
            allowed.add(path)
        prior_versions = before.get("registry_history", {}).get(key.registry["subject"], [])
        versions = after.get("registry_history", {}).get(key.registry["subject"], [])
        new_versions = [version for version in versions if version not in prior_versions]
        if (final or new_versions) and (len(new_versions) != 1 or new_versions[0].get("why") != key.registry["reason"]):
            reasons.append("registry history does not retain the requested save reason")
    for path in set(before.get("files", {})) | set(after.get("files", {})):
        if path in allowed or before.get("files", {}).get(path) == after.get("files", {}).get(path):
            continue
        if key.kind == WRITE and path == f"{kb_dirname()}/log.md" and _allowed_log_change(
            before.get("files", {}).get(path, {}), after.get("files", {}).get(path, {}), log_targets
        ):
            continue
        reasons.append(f"authored file {path} changed outside the request")
    if (set(before.get("empty_directories", [])) - allowed_empty) != (set(after.get("empty_directories", [])) - allowed_empty):
        reasons.append("empty directories changed outside the request")
    for domain in ("media", "machine", "registry_paths", "other_preferences", "governance"):
        if before.get(domain) != after.get(domain):
            reasons.append(f"{domain} changed outside the request")
    for subject in set(before.get("registry_history", {})) | set(after.get("registry_history", {})):
        if key.registry is not None and subject == key.registry["subject"]:
            continue
        if before.get("registry_history", {}).get(subject) != after.get("registry_history", {}).get(subject):
            reasons.append(f"registry history for {subject} changed outside the request")
    return reasons


def _allowed_log_change(before: Mapping, after: Mapping, targets: set[str]) -> bool:
    """Admit one source-defined operation-log entry for an intended target, preserving prior entries."""
    from exomem.kbdir import kb_prefix

    def sections(tokens):
        starts = [index for index, token in enumerate(tokens)
                  if token["type"] == "heading_open" and token["tag"] == "h2"]
        if not starts:
            return tokens, []
        return tokens[:starts[0]], [
            tokens[start:end] for start, end in zip(starts, [*starts[1:], len(tokens)], strict=True)
        ]

    if before.get("frontmatter") != after.get("frontmatter"):
        return False
    old_prefix, old = sections(before.get("body", []))
    new_prefix, new = sections(after.get("body", []))
    added = len(new) - len(old)
    if old_prefix != new_prefix or added != 1 or new[added:] != old:
        return False
    header = "".join(child.get("content") or "" for child in new[0][1].get("children", []))
    _, separator, target = header.partition(" | ")
    expected = {path.removeprefix(kb_prefix()).removesuffix(".md") for path in targets}
    return bool(separator) and target in expected


# --------------------------------------------------------------------------
# Calls.
# --------------------------------------------------------------------------


def compact_calls(tap_events: Sequence[Mapping]) -> list[dict]:
    """Each call the session made, as the tap saw it, without the result payloads.

    ``errored`` is the tool's own error flag; ``codes`` are the structured error
    codes in the result envelope (read as JSON, never from prose).
    """

    from exomem import mutation_terminal

    calls = []
    for event in tap_events:
        if event.get("event") != "call":
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        envelope = {"result": event.get("result"), "error": event.get("error")}
        payloads = list(tool_payloads(envelope))
        for value in list(payloads):
            if isinstance(value, Mapping):
                payloads.extend(value.get(name) for name in ("receipt", "leaf_result", "details")
                                if isinstance(value.get(name), Mapping))
        codes = sorted(
            {
                str(value[name])
                for value in payloads
                if isinstance(value, dict)
                for name in ("code", "error_code")
                if isinstance(value.get(name), str)
            }
        )
        calls.append(
            {
                "ordinal": event.get("ordinal"),
                "name": event.get("name"),
                "arguments": event.get("arguments"),
                "outcome": event.get("outcome"),
                "errored": bool(event.get("error") or result.get("isError")),
                "codes": codes,
                "canonical_state": event.get("canonical_state"),
                "terminals": [
                    {name: value[name] for name in ("_terminal", "version", "state", "mutated", "receipt_id") if name in value}
                    for value in payloads
                    # The marker, version and boolean field are the source-defined terminal protocol.
                    if isinstance(value, dict) and value.get("_terminal") == "exomem.mutation-terminal"
                    and type(value.get("version")) is int and value["version"] == 1
                    and type(value.get("mutated")) is bool
                ],
                "receipts": [value for value in payloads if isinstance(value, Mapping) and (
                    mutation_terminal.valid_collection_receipt(value)
                    or mutation_terminal.valid_structured_files_receipt(value)
                    or mutation_terminal.valid_control_receipt(value)
                )],
                "result": event.get("result"),
            }
        )
    return calls


@dataclass
class CaseResult:
    case_id: str
    template: str
    kind: str
    arm: str
    repeat: int
    outcome: str
    scored: bool
    success: bool = False
    reasons: list[str] = field(default_factory=list)
    #: Read tasks only: the vault changed, or a write-capable call was served.
    wrong_write: bool = False
    state_changed: bool | None = None
    write_calls_served: int = 0
    write_attempts: int = 0
    committed_effects: int = 0
    unexpected_mutations: int = 0
    effect_unknown_calls: int = 0
    read_audit_receipts: int = 0
    calls: int = 0
    unresolved_calls: int = 0
    argument_refusals: int = 0
    #: 1-based ordinal of the first call that resolved to an operation the key accepts.
    first_correct_ordinal: int | None = None
    schema_bytes: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    wall_seconds: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def score_session(
    case: Case, session: Mapping, calls: Sequence[Mapping], resolver: Resolver
) -> CaseResult:
    """Score one finished session from its record, its calls and the injected resolver.

    ``session`` is the session record the run loop writes: the outcome, the
    answer, the turn records, and the facts with the end states.
    """

    key = case.key
    outcome = session["outcome"]
    result = CaseResult(
        case.case_id, case.template_id, case.kind, session["arm"], session["repeat"], outcome,
        scored=outcome in SCORABLE_OUTCOMES,
    )
    facts = session.get("facts") or {}
    turn = (session.get("turns") or [{}])[0]
    result.schema_bytes = (facts.get("surface") or {}).get("schema_bytes")
    result.input_tokens = turn.get("input_tokens")
    result.output_tokens = turn.get("output_tokens")
    result.wall_seconds = turn.get("wall_seconds")
    result.calls = len(calls)
    before = facts.get("end_state_before") or {}
    previous = before
    previous_raw = before
    allowed_read_audit: set[tuple] = set()
    for call in calls:
        resolved = resolve_call(resolver, str(call["name"]), call.get("arguments"), session["arm"])
        served = call["outcome"] == "served"
        if resolved.read_only is False:
            result.write_attempts += 1
        if not resolved.resolved:
            result.unresolved_calls += 1
        if resolved.resolved and result.first_correct_ordinal is None and (
            any((resolved.command, resolved.action, resolved.mode) == (op.command, op.action, op.mode)
                and all(type(resolved.arguments.get(name)) is type(value) and resolved.arguments.get(name) == value
                        for name, value in op.constraints.items()) for op in key.target_ops)
        ):
            result.first_correct_ordinal = int(call["ordinal"])
        raw_snapshot = call.get("canonical_state")
        snapshot = raw_snapshot
        if snapshot is not None:
            allowed_read_audit.update(_read_audit_ids(previous_raw, snapshot, call, resolved))
            result.read_audit_receipts = len(allowed_read_audit)
            snapshot = _quality_state(snapshot, before, allowed_read_audit)
        known_state = snapshot is not None and not snapshot.get("unknown") and not previous.get("unknown")
        changed = known_state and state_changed(previous, snapshot)
        terminals = call.get("terminals") or []
        committed = any(terminal.get("mutated") is True for terminal in terminals) or any(
            receipt.get("outcome") == "committed" for receipt in call.get("receipts") or []
        )
        settled_noop = any(terminal.get("mutated") is False for terminal in terminals)
        refused_arguments = bool(ARGUMENT_REFUSALS.intersection(call["codes"]))
        if served and resolved.read_only is False and (committed or settled_noop or not call["codes"] and not call["errored"]):
            result.write_calls_served += 1
        if served and (committed or changed):
            result.committed_effects += 1
        if served and (changed or committed) and (
            key.kind == READ or known_state and _canonical_checks(key, before, snapshot, final=False)
        ):
            result.unexpected_mutations += 1
        if served and (resolved.read_only is not True) and (call["codes"] or call["errored"]) and not (
            committed or known_state or refused_arguments or settled_noop
        ):
            result.effect_unknown_calls += 1
        if snapshot is not None:
            if snapshot.get("unknown"):
                result.effect_unknown_calls += 1
            previous = snapshot
            previous_raw = raw_snapshot
        if refused_arguments:
            result.argument_refusals += 1
    if key.kind == READ:
        result.wrong_write = result.write_calls_served > 0 or result.unexpected_mutations > 0
    if not result.scored:
        result.reasons.append(f"session outcome {outcome}")
        return result

    before = facts["end_state_before"]
    after = _quality_state(facts["end_state_after"], before, allowed_read_audit)
    result.state_changed = None if before.get("unknown") or after.get("unknown") else state_changed(before, after)
    if key.kind == READ and result.state_changed is True:
        result.wrong_write = True
    if (before.get("unknown") or after.get("unknown") or result.effect_unknown_calls
            or case.template_id == "source-derived" and ("files" not in before or "files" not in after)):
        result.scored = False
        result.reasons.append("canonical state or a write effect is unresolved")
        result.reasons.extend(f"canonical read unavailable: {finding}" for finding in [
            *before.get("unknown", []), *after.get("unknown", [])
        ])
        return result
    if key.kind == READ:
        if not answer_matches(parse_answer(session.get("answer")), key):
            result.reasons.append("answer does not match the seeded facts")
        if key.preview is not None and not _preview_observed(key, calls, resolver, session["arm"]):
            result.reasons.append("the requested preview has no successful structured evidence")
        if result.wrong_write:
            result.reasons.append("a read task wrote")
    elif key.kind == WRITE:
        result.reasons.extend(check_end_state(key, before, after))
        if result.unexpected_mutations:
            result.reasons.append("an intermediate mutation changed facts outside the request")
    if not result.unexpected_mutations and result.state_changed and (
        key.kind == READ or _canonical_checks(key, before, after, final=False)
    ):
        result.unexpected_mutations = 1
    result.success = not result.reasons
    return result


def _preview_observed(key: Key, calls: Sequence[Mapping], resolver: Resolver, shape: str) -> bool:
    expected = key.preview
    for call in calls:
        if call["outcome"] != "served" or call["errored"] or call["codes"]:
            continue
        resolved = resolve_call(resolver, str(call["name"]), call.get("arguments"), shape)
        if not any((resolved.command, resolved.action) == (op.command, op.action) for op in key.target_ops):
            continue
        payloads = [value for value in tool_payloads({"result": call.get("result")}) if isinstance(value, Mapping)]
        if "delta" in expected:
            if resolved.arguments.get("proposal") == expected["delta"] and any(value.get("valid") is True for value in payloads):
                return True
        elif "proposed_content" in expected:
            if (resolved.arguments.get("content") == expected["proposed_content"]
                    and any(value.get("mutated") is False and authored_unit(value.get("unit", {})) == expected["unit"]
                            for value in payloads)):
                return True
        elif "contains_proposed_paragraph" in expected:
            if (markdown_facts(resolved.arguments.get("content") or "") == markdown_facts(expected["contains_proposed_paragraph"])
                    and any(value.get("mutated") is False and value.get("path") == expected["path"] for value in payloads)):
                return True
    return False
