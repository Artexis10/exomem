"""A case's vault: seeding it through the product, and reading its end state from files.

Seeding calls the product's own Records and Planning commands, so the vault
holds exactly what a real caller's writes leave, and it runs in a child process
whose every product location is pinned under a scratch directory (see
``executor.seed_case``). The end state is read from files alone, with no server
and no index, so reading it never changes it and never depends on the
server's answers.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from markdown_it import MarkdownIt

from .templates import PLAN_COMMAND, RECORD_COMMAND, SeedStep

#: Seed-step command to its action table: the product function, the argument that
#: names an item, and the row column that carries the item's identity.
_SEED_COMMANDS = {
    RECORD_COMMAND: ("exomem.record_memory", "record_memory", "item_key", "record_id"),
    PLAN_COMMAND: ("exomem.plan_memory", "plan_memory", "plan_id", "plan_id"),
}
SEED_WHY = "seed the evaluation vault"
#: The projector never reads a clock; any fixed instant names the snapshot.
_SNAPSHOT_TIME = "2026-01-01T00:00:00Z"


def _command(name: str):
    import importlib

    module, function, id_argument, id_column = _SEED_COMMANDS[name]
    return getattr(importlib.import_module(module), function), id_argument, id_column


def seed_vault(vault: Path, steps: Sequence[SeedStep]) -> None:
    """Initialise ``vault`` and run each step through the product command."""

    from exomem.init import init_vault

    vault.mkdir(parents=True, exist_ok=True)
    init_vault(vault)
    apply_steps(vault, steps)


def apply_steps(vault: Path, steps: Sequence[SeedStep]) -> None:
    """Run each step through the product command on an initialised vault."""

    for step in steps:
        for relative, text in step.originals.items():
            destination = vault / relative
            if not destination.resolve().is_relative_to(vault.resolve()) or destination.exists():
                raise ValueError("fixture originals must be new files inside the isolated vault")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(text.encode("utf-8"))
        if step.command not in _SEED_COMMANDS:
            _seed_product(vault, step)
            continue
        function, id_argument, id_column = _command(step.command)
        kwargs = {**step.kwargs, "why": SEED_WHY}
        if step.match is not None:
            collection = kwargs["collection"]
            rows = function(vault, "query", collection=collection)["rows"]
            found = [
                row for row in rows if all(row.get(name) == value for name, value in step.match.items())
            ]
            if len(found) != 1:
                raise RuntimeError(f"seed step matched {len(found)} items, not one: {dict(step.match)}")
            guard = function(vault, "inspect", collection=collection)["lifecycle_guards"]
            kwargs.update(
                {
                    id_argument: found[0][id_column],
                    "expected_item_version": found[0]["item_version"],
                    "expected_container_hash": guard["expected_container_hash"],
                }
            )
        function(vault, step.action, **kwargs)


def _seed_product(vault: Path, step: SeedStep) -> None:
    """Bind only fixture guards from product responses; authored facts come from the step."""
    from exomem import commands
    from exomem.command_surface import resolve_command
    from exomem.governance import egress
    from exomem.governance.principal import owner_principal, request_scope

    command = resolve_command(step.command, commands.PRODUCT_COMMANDS)
    if command is None:
        raise ValueError(f"unknown fixture command: {step.command}")
    # A leaf must not mutate the seed facts from which the independent key is computed.
    arguments = copy.deepcopy(dict(step.kwargs))
    selector = egress.selector_for_command(command.name)
    if selector is not None:
        arguments[selector] = step.action
    with request_scope(owner_principal(surface="mcp")):
        if command.name == "configure_memory":
            inspected = command.leaf(vault, action="inspect")
            arguments["expected_revision"] = inspected["revision"]
        elif command.name == "schema_memory":
            preview = command.leaf(vault, operation="propose", subject=arguments["subject"],
                                   proposal=arguments["proposal"])
            arguments.update(expected_hash=preview["expected_hash"], why=SEED_WHY)
        elif command.name == "remember":
            preview = command.leaf(vault, **arguments, validate_only=True)
            arguments.update({name: preview[name] for name in ("draft_id", "draft_hash", "draft_token")})
            if preview["reviewed_none_required"]:
                arguments.update(relation_disposition="reviewed_none", relation_review_hash=preview["draft_hash"],
                                 relation_review_reason="The invented fixture asserts no relation to another page.")
        elif command.name == "observe_memory":
            preview = command.leaf(vault, **{**arguments, "operation": "validate"})
            semantic = preview["semantic"]
            arguments["transition_token"] = semantic["transition_token"]
            if not semantic["contract_result"]["relation_disposition"]["satisfied"]:
                arguments.update(relation_disposition="reviewed_none", relation_review_hash=semantic["transition_hash"],
                                 relation_review_reason="The invented fixture asserts no relation to another page.")
        elif command.name == "adoption_studio" and step.match is not None:
            from exomem.adoption_run import AdoptionRunStore

            store = AdoptionRunStore(vault)
            runs = [store.load(entry["run_id"]) for entry in store.list_runs()]
            matches = [run for run in runs if all(run.get(name) == value for name, value in step.match.items())]
            if len(matches) != 1:
                raise ValueError("the adoption fixture must bind exactly one prepared run")
            arguments["run_id"] = matches[0]["run_id"]
        result = command.leaf(vault, **arguments)
    if result.get("success") is False or result.get("mutated") is False:
        raise RuntimeError(f"fixture command did not commit: {step.command}: {result}")


def adoption_runs(state: Mapping) -> dict[str, dict]:
    """Read already-projected run JSON at the canonical owner's paths, retaining every other file."""
    from exomem.adoption_run import AdoptionRunStore

    owner = AdoptionRunStore(Path("."))
    return {path: fact["value"] for path, fact in state.get("files", {}).items()
            if Path(path).parent.parent == owner.base and Path(path).name == "run.json"
            and isinstance(fact.get("value"), dict)}


def _manifest_fields(vault: Path, manifest: str) -> dict[str, dict]:
    """The manifest's declared fields, from its YAML frontmatter."""

    from exomem.vault import parse_frontmatter

    header, _, _ = parse_frontmatter((vault / manifest).read_text(encoding="utf-8"), strict=True)
    declared = header.get("item_schema", {}).get("fields", {})
    return {
        name: {"type": spec.get("type"), "required": bool(spec.get("required", False))}
        for name, spec in declared.items()
        if isinstance(spec, Mapping)
    }


def _canonical(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def markdown_facts(text: str) -> list[dict]:
    """CommonMark structure without byte offsets or choice of Markdown delimiters."""
    def fact(token):
        return {"type": token.type, "tag": token.tag, "nesting": token.nesting,
                "attrs": token.attrs, "info": token.info,
                "content": token.content if not token.children else None,
                "children": [fact(child) for child in token.children or []]}

    return [fact(token) for token in MarkdownIt().parse(text)]


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def authored_unit(value: Mapping | None) -> dict:
    """The authored portion of the product's semantic-unit result schema."""
    if not isinstance(value, Mapping):
        return {}
    return {"id": value.get("anchor"), **{name: value.get(name) for name in (
        "kind", "category", "content", "tags", "context", "relations", "verdict", "check_by"
    )}}


def _file_fact(path: Path, relative: str, evidence: dict, unknown: list, *, opaque: bool = False) -> dict:
    from exomem import semantic_units
    from exomem.vault import parse_frontmatter

    try:
        if path.is_symlink():
            return {"kind": "symlink", "target": os.readlink(path)}
        raw = path.read_bytes()
        evidence[relative] = {"sha256": hashlib.sha256(raw).hexdigest(),
                              "bytes": len(raw), "base64": base64.b64encode(raw).decode("ascii")}
        if opaque:
            return {"kind": "opaque", "sha256": evidence[relative]["sha256"]}
        # File extensions are the closed storage formats the product writers use.
        if path.suffix == ".md":
            text = raw.decode("utf-8")
            frontmatter, body, _ = parse_frontmatter(text, strict=True)
            document = semantic_units.parse_semantic_units(text, path=relative)
            units = [authored_unit(unit.to_dict()) for unit in document.units]
            remainder = text
            for unit in reversed(document.units):
                remainder = remainder[:unit.span.start_offset] + remainder[unit.span.end_offset:]
            _, remainder, _ = parse_frontmatter(remainder, strict=True)
            return {"kind": "markdown", "frontmatter": _json_value(frontmatter),
                    "body": markdown_facts(body), "units": units,
                    "remainder": markdown_facts(remainder)}
        if path.suffix == ".json":
            return {"kind": "json", "value": json.loads(raw)}
        if path.suffix in (".yaml", ".yml"):
            from exomem.vault import yaml_safe_load

            return {"kind": "yaml", "value": _json_value(yaml_safe_load(raw.decode("utf-8")))}
        return {"kind": "opaque", "sha256": evidence[relative]["sha256"]}
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        unknown.append({"path": relative, "error": type(error).__name__})
        return {"kind": "unreadable", "error": type(error).__name__}


def _media_rows(vault: Path) -> dict:
    from exomem.media_jobs import job_store_path

    path = job_store_path(vault)
    if not path.exists():
        return {"status": "absent", "tables": {}}
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            tables = {}
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                # The owner's runtime table and SQLite sequence are bookkeeping, not job facts.
                if table in ("runtime", "sqlite_sequence"):
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                tables[table] = sorted((_json_value(dict(row)) for row in connection.execute(f"SELECT * FROM {quoted}")), key=_canonical)
        return {"status": "present", "tables": tables}
    except (OSError, sqlite3.Error) as error:
        return {"status": "unreadable", "error": type(error).__name__}


def read_end_state(vault: Path) -> dict:
    """Canonical authored facts plus raw custody evidence, read without a server or index writes."""

    from epistemic.projectors.exomem_vault import VaultProjector

    from exomem import prominence_preferences as preferences
    from exomem import reserved_paths, review_state, state_paths
    from exomem.governance.principal import OWNER_AUDIENCE, owner_principal, request_scope

    unknown: list[dict] = []
    try:
        snapshot = VaultProjector(vault).project(phase="end", taken_at=_SNAPSHOT_TIME)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        snapshot = None
        unknown.append({"domain": "vault-projector", "error": type(error).__name__})
    collections = {}
    for collection in snapshot.collections if snapshot is not None else ():
        rows = sorted((dict(item.values) for item in collection.items), key=_canonical)
        collections[collection.manifest] = {
            "id": collection.id,
            "profile": collection.profile,
            "title": collection.title,
            "natural_key": list(collection.natural_key),
            "fields": _manifest_fields(vault, collection.manifest),
            "rows": rows,
            "paths": [collection.manifest, *(item.locator for item in collection.items)],
            "source_path": (Path(collection.manifest).parent / collection.storage_source).as_posix(),
            "items": {item.key: {"path": item.locator, "values": dict(item.values)} for item in collection.items},
        }
    evidence = {"files": {}, "machine": {}, "unavailable": [],
                "projector": snapshot.model_dump(mode="json") if snapshot is not None else None}
    descriptors = {descriptor.id: descriptor for descriptor in reserved_paths.internal_state_registry()}
    # These are registered owners of rebuildable indexes and coordination, not content vocabularies.
    derived_owners = {"embedding_index", "clip_index", "lexstore", "epistemic_graph", "claims",
                      "memory_refs", "references.legacy", "freshness.legacy", "deferred_index", "graph_sync"}

    def derived(relative):
        classified = reserved_paths.classify_logical(relative)
        descriptor = descriptors.get(classified.descriptor_id)
        return descriptor is not None and descriptor.owner in derived_owners

    files = {}
    directories = []
    for path in sorted(vault.rglob("*")):
        relative = path.relative_to(vault).as_posix()
        if derived(relative):
            continue
        if path.is_dir() and not path.is_symlink():
            if not any(path.iterdir()):
                directories.append(relative)
        else:
            files[relative] = _file_fact(path, relative, evidence["files"], unknown)
    for run in adoption_runs({"files": files}).values():
        inventory = run.get("inventory")
        for item in inventory if isinstance(inventory, list) else []:
            relative = item.get("path") if isinstance(item, Mapping) else None
            if relative in files and relative in evidence["files"]:
                # Adoption preserves original bytes, including Markdown whitespace, through every response snapshot.
                files[relative]["original_sha256"] = evidence["files"][relative]["sha256"]
    machine = {}
    state = state_paths.vault_state_dir(vault)
    owner_preference = preferences._preference_path(vault, OWNER_AUDIENCE)
    preference_root = owner_preference.parent
    review_path = review_state.state_path(vault)
    from exomem.media_jobs import job_store_path, worker_lock_path
    from exomem.working_set_heat import sidecar_path as heat_path

    # SQLite's closed family suffixes come from the owner's shared path contract.
    heat_files = {Path(str(heat_path(vault)) + suffix) for suffix in reserved_paths._SQLITE_SUFFIXES}
    media_files = {Path(str(job_store_path(vault)) + suffix) for suffix in reserved_paths._SQLITE_SUFFIXES}
    other_preferences = {}

    for path in sorted(state.rglob("*")) if state.exists() else ():
        if path.is_dir():
            continue
        relative = path.relative_to(state).as_posix()
        if (derived(f"Knowledge Base/{relative}") or path in heat_files or path in media_files
                or path == worker_lock_path(vault) or path == preference_root / ".lock"):
            # Rebuildable metadata can change concurrently; its raw evidence is separate from authored-state admission.
            _file_fact(path, relative, evidence["machine"], evidence["unavailable"], opaque=True)
            continue
        fact = _file_fact(path, relative, evidence["machine"], unknown)
        if path.is_relative_to(preference_root) and path.suffix == ".json":
            if path != owner_preference:
                try:
                    stored, contexts, revision, receipt = preferences._read(path)
                    other_preferences[path.name] = {"stored": stored, "contexts": contexts,
                                                    "revision": revision, "receipt_id": receipt}
                except Exception as error:  # noqa: BLE001 - another audience's failed read is not absence
                    unknown.append({"domain": "other-preferences", "path": relative, "error": type(error).__name__})
            continue
        if path == review_path:
            continue
        machine[relative] = fact
    with request_scope(owner_principal(surface="mcp")):
        try:
            preference = {"status": "known", **preferences.inspect(vault)}
        except Exception as error:  # noqa: BLE001 - unreadable canonical data stays visible
            preference = {"status": "unreadable", "error": type(error).__name__}
            unknown.append({"domain": "preferences", **preference})
        try:
            review = review_state.ReviewStateStore(vault).load()
        except Exception as error:  # noqa: BLE001 - never replace a failed read with an empty store
            review = {"status": "unreadable", "error": type(error).__name__}
            unknown.append({"domain": "review", **review})
        from exomem import registry_history
        from exomem.vocabulary import instances, registry_specs

        registry_paths = {}
        histories = {}
        for subject, declared in registry_specs().items():
            try:
                spec = instances.select(vault, declared)
                registry_paths[subject] = {
                    "overlay": spec.overlay(vault).relative_to(vault).as_posix(),
                    "history": registry_history.history_dir(vault, spec.stem).relative_to(vault).as_posix(),
                }
                histories[subject] = registry_history.history_view(vault, stem=spec.stem)["versions"]
            except Exception as error:  # noqa: BLE001 - a failed registry read is not a complete snapshot
                unknown.append({"domain": "registry", "subject": subject, "error": type(error).__name__})
    media = _media_rows(vault)
    if media["status"] == "unreadable":
        unknown.append({"domain": "media", **media})
    return {"collections": collections, "files": files, "empty_directories": directories,
            "preferences": preference, "review": review, "media": media, "machine": machine,
            "registry_paths": registry_paths, "registry_history": histories,
            "other_preferences": other_preferences,
            "evidence": evidence, "unknown": unknown}
