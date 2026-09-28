"""Shared contract for the close-memory-loop acceptance fixtures.

Every fixture in this package has three separately digested parts:

* the **actor input** -- the ordinary user turns an agent under test sees,
  with no save, recall, routing or page-creation nudge in them;
* the **pre-capture world** -- a declarative spec rendered through the
  product's own writers (entity types through ``schema_memory``, entities
  through the entity writer, aliases through ``edit_memory``, notes through
  the compiled-note writer, Records through ``record_memory``) in isolated
  state, frozen as a logical digest of the canonical pages it produced;
* the **evaluator expectations** -- positives and negatives the actor never
  sees, checked model-free against the vault the capture leaves behind.

The actor digest and the pre-capture digest are what a run binds before any
effect (``observation.NoNudgeObservation`` carries both); the evaluator digest
is frozen at the same time, so an expectation edited after a run voids that
run instead of rescoring it.

Expectations are small typed predicates over canonical readback: entity
counts by type and name tokens, an entity left intact, page text markers,
typed ``## Relations`` edges, and Records items. They read only what a
canonical page or Records item says, never a packet, a score or a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

KB = "Knowledge Base"
_CORPUS_DATE = "2026-09-01"


class FixtureError(ValueError):
    """A fixture is malformed, leaks, nudges, or its world failed to build."""


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def _is_default(item: Any, value: Any) -> bool:
    if item.default is not MISSING:
        return value == item.default
    if item.default_factory is not MISSING:
        return value == item.default_factory()
    return False


def _plain(value: Any) -> Any:
    """A digestable form. Fields still at their declared default are omitted,
    so adding an optional field to a contract type never moves a frozen digest;
    changing any value a fixture actually sets always does."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__kind__": type(value).__name__,
            **{
                f.name: _plain(getattr(value, f.name))
                for f in fields(value)
                if not _is_default(f, getattr(value, f.name))
            },
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def sha256_json(value: Any) -> str:
    encoded = json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Actor-input guards
# --------------------------------------------------------------------------- #

#: Words and phrases that would tell the actor to save, recall, route or make
#: a page. An actor turn containing any of them is not an ordinary turn.
NUDGE_PATTERNS: tuple[str, ...] = (
    r"\bsave\b",
    r"\bsaving\b",
    r"\bremember\b",
    r"\brecall\b",
    r"\bmemory\b",
    r"\bexomem\b",
    r"\bnote (?:this|that|it|down)\b",
    r"\bwrite (?:this|that|it) down\b",
    r"\blog (?:this|that|it)\b",
    r"\brecord (?:this|that|it)\b",
    r"\bfile (?:this|that|it)\b",
    r"\bcapture\b",
    r"\b(?:new|a|the) page\b",
    r"\bcreate an? (?:note|entity|page)\b",
    r"\blink (?:this|that|it|them)\b",
    r"\bknowledge base\b",
    r"\bvault\b",
    r"\bkeep track\b",
)


def find_nudges(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    return tuple(pattern for pattern in NUDGE_PATTERNS if re.search(pattern, lowered))


#: Clinical vocabulary no public fixture may carry: the fixtures describe
#: ordinary perception, products and places, never a diagnosis, symptom or
#: treatment.
MEDICAL_TERMS: tuple[str, ...] = (
    "diagnos",
    "symptom",
    "illness",
    "disease",
    "infection",
    "virus",
    "covid",
    "anosmia",
    "medication",
    "medicine",
    "doctor",
    "clinic",
    "hospital",
    "treatment",
    "therapy",
    "prescri",
    "condition",
    "disorder",
    "injury",
    "surgery",
)


def find_medical_terms(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    return tuple(term for term in MEDICAL_TERMS if term in lowered)


_CAPITALIZED = re.compile(r"\b[A-Z][a-z]+(?:['’][a-z]+)?\b")


def undeclared_capitalized_words(text: str, allowed: Iterable[str]) -> tuple[str, ...]:
    """Capitalized words that are neither declared invented names nor allowed.

    A structural guard: a proper name can enter a public fixture only by being
    declared in that fixture's invented-name list, where review sees it.
    Sentence-initial words are still checked, so the allowed set carries the
    handful of ordinary words the turns begin with.
    """

    allowed_set = {word.casefold() for word in allowed}
    found = []
    for match in _CAPITALIZED.finditer(text):
        word = match.group(0).replace("’", "'").split("'")[0]
        if word.casefold() not in allowed_set:
            found.append(word)
    return tuple(sorted(set(found)))


# --------------------------------------------------------------------------- #
# Pre-capture world spec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntityTypeSeed:
    type_id: str
    folder: str
    label: str
    aliases: tuple[str, ...]
    parent: str
    guidance: str


@dataclass(frozen=True)
class EntitySeed:
    key: str
    entity_type: str
    name: str
    summary: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoteSeed:
    key: str
    title: str
    slug: str
    observation: str
    category: str = "finding"
    note_type: str = "insight"
    extra_body: str = ""


@dataclass(frozen=True)
class RecordsSeed:
    key: str
    manifest_path: str
    exomem_id: str
    title: str
    claims: tuple[str, ...]
    natural_key: tuple[str, ...]
    #: ``(field, type, required, enum)`` rows of the item schema.
    fields: tuple[tuple[str, str, bool, tuple[str, ...]], ...]
    #: ``(item_key, {field: value})`` items appended in order.
    items: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    description: str = "Synthetic observed state for a close-memory-loop fixture."


@dataclass(frozen=True)
class RelationTypeSeed:
    """A governed relation extension the world registers before any edge uses it."""

    relation: str
    parent: str
    description: str
    direction: str = "directed"


@dataclass(frozen=True)
class RelationSeed:
    """A ``## Relations`` edge on ``source``'s page to ``target``'s page (world keys)."""

    source: str
    relation: str
    target: str


@dataclass(frozen=True)
class PreCapture:
    types: tuple[EntityTypeSeed, ...] = ()
    entities: tuple[EntitySeed, ...] = ()
    notes: tuple[NoteSeed, ...] = ()
    records: tuple[RecordsSeed, ...] = ()
    relation_types: tuple[RelationTypeSeed, ...] = ()
    relations: tuple[RelationSeed, ...] = ()

    def keys(self) -> tuple[str, ...]:
        return tuple(
            item.key for group in (self.entities, self.notes, self.records) for item in group
        )


def _spec_from_plain(data: Mapping[str, Any]) -> PreCapture:
    return PreCapture(
        types=tuple(EntityTypeSeed(**{**item, "aliases": tuple(item["aliases"])}) for item in data["types"]),
        entities=tuple(EntitySeed(**{**item, "aliases": tuple(item["aliases"])}) for item in data["entities"]),
        notes=tuple(NoteSeed(**item) for item in data["notes"]),
        records=tuple(
            RecordsSeed(
                key=item["key"],
                manifest_path=item["manifest_path"],
                exomem_id=item["exomem_id"],
                title=item["title"],
                claims=tuple(item["claims"]),
                natural_key=tuple(item["natural_key"]),
                fields=tuple(
                    (name, kind, required, tuple(enum)) for name, kind, required, enum in item["fields"]
                ),
                items=tuple(
                    (item_key, tuple((key, value) for key, value in values))
                    for item_key, values in item["items"]
                ),
                description=item["description"],
            )
            for item in data["records"]
        ),
        relation_types=tuple(RelationTypeSeed(**item) for item in data.get("relation_types", ())),
        relations=tuple(RelationSeed(**item) for item in data.get("relations", ())),
    )


def _records_manifest(seed: RecordsSeed) -> str:
    field_lines = []
    for name, kind, required, enum in seed.fields:
        field_lines.append(f"    {name}:\n      type: {kind}")
        if enum:
            field_lines.append(f"      enum: [{', '.join(enum)}]")
        if required:
            field_lines.append("      required: true")
    return (
        "---\n"
        "type: collection\n"
        f"exomem_id: {seed.exomem_id}\n"
        f"title: {seed.title}\n"
        "semantic_profile: records\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  source: Items\n"
        "  format_version: 1\n"
        "claims:\n"
        f"  terms: [{', '.join(seed.claims)}]\n"
        "item_schema:\n"
        f"  natural_key: [{', '.join(seed.natural_key)}]\n"
        "  fields:\n" + "\n".join(field_lines) + "\n"
        "---\n\n"
        f"{seed.description}\n"
    )


def _edit_frontmatter(root: Path, path: str, field_name: str, value: object, why: str) -> None:
    from exomem import commands, writer_lease
    from exomem.vault import content_hash

    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "edit_memory")
    text = (root / path).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        command,
        root,
        path=path,
        why=why,
        operation={
            "kind": "patch_frontmatter",
            "field": field_name,
            "value": value,
            "expected_hash": content_hash(text),
        },
    )


def _compiled_note(root: Path, seed: NoteSeed) -> str:
    import datetime as dt

    from exomem import note

    body = f"## Observations\n\n- [{seed.category}] {seed.observation}\n"
    if seed.extra_body.strip():
        body += f"\n{seed.extra_body.strip()}\n"
    arguments = {
        "vault_root": root,
        "content": body,
        "note_type": seed.note_type,
        "title": seed.title,
        "slug": seed.slug,
        "sources": [],
        "tags": [],
        "today": dt.date.fromisoformat(_CORPUS_DATE),
    }
    validation = note.note(validate_only=True, **arguments)
    reviewed: dict[str, object] = {}
    if getattr(validation.creation_validation, "reviewed_none_required", False):
        reviewed = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation.draft_hash,
            "relation_review_reason": "No relation is part of this synthetic pre-capture state.",
        }
    result = note.note(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    )
    return result.path


def build_world_in_process(root: Path, spec: PreCapture) -> dict[str, str]:
    """Render ``spec`` under ``root`` through the product's own writers."""

    import datetime as dt

    from exomem import commands, link
    from exomem.init import init_vault

    root = Path(root)
    init_vault(root)
    key_to_path: dict[str, str] = {}
    if spec.types:
        proposal = {
            "schema_version": 1,
            "entity_types": {
                seed.type_id: {
                    "folder": seed.folder,
                    "label": seed.label,
                    "aliases": list(seed.aliases),
                    "capture_guidance": seed.guidance,
                    "status": "active",
                    "parent": seed.parent,
                }
                for seed in spec.types
            },
        }
        saved = commands.op_schema_memory(
            root,
            operation="save-entity-types",
            proposal=proposal,
            why="register the fixture's governed entity types",
        )
        if saved.get("valid") is not True:
            raise FixtureError(f"entity type registration was refused: {saved}")
    for seed in spec.entities:
        created = link.link(
            root,
            entity_type=seed.entity_type,
            name=seed.name,
            summary=seed.summary,
            today=dt.date.fromisoformat(_CORPUS_DATE),
        )
        key_to_path[seed.key] = created.path
        if seed.aliases:
            _edit_frontmatter(root, created.path, "aliases", list(seed.aliases), "record the name it is also called by")
    for seed in spec.notes:
        key_to_path[seed.key] = _compiled_note(root, seed)
    for seed in spec.records:
        commands.op_record_memory(
            root,
            action="create",
            manifest_path=seed.manifest_path,
            manifest_text=_records_manifest(seed),
            scaffold=True,
            why="seed the fixture's Records collection",
        )
        for item_key, values in seed.items:
            snapshot = commands.op_record_memory(root, action="inspect", collection=seed.manifest_path)["snapshot"]
            commands.op_record_memory(
                root,
                action="append",
                collection=seed.manifest_path,
                item=dict(values),
                item_key=item_key,
                expected_container_hash=snapshot,
                why="seed one prior observed item",
            )
        key_to_path[seed.key] = seed.manifest_path
    if spec.relation_types:
        from exomem import relation_registry

        commands.op_schema_memory(
            root,
            subject="relations",
            operation="save-relations",
            proposal={
                "upsert": {
                    seed.relation: {
                        "parent": seed.parent,
                        "description": seed.description,
                        "direction": seed.direction,
                        "aliases": [],
                    }
                    for seed in spec.relation_types
                }
            },
            expected_hash=relation_registry.load_registry(root).extension_hash,
            why="register the fixture's governed relation meanings",
        )
    for seed in spec.relations:
        _add_relation(root, key_to_path[seed.source], seed.relation, Path(key_to_path[seed.target]).stem)
    return key_to_path


def _add_relation(root: Path, path: str, relation: str, target: str) -> None:
    """Add one ``## Relations`` bullet through the reviewed body edit."""

    from exomem import commands
    from exomem.vault import content_hash

    text = (root / path).read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1].rstrip()
    bullet = f"- {relation} [[{target}]]"
    if "\n## Relations\n" in f"\n{body}\n":
        body = body.replace("## Relations\n", f"## Relations\n\n{bullet}", 1)
    else:
        body = f"{body}\n\n## Relations\n\n{bullet}"
    operation = {"kind": "replace_body", "new_body": body + "\n", "expected_hash": content_hash(text)}
    preview = commands.op_edit_memory(
        root, path=path, why="seed the fixture's relation", operation={**operation, "validate_only": True}
    )["semantic"]
    commands.op_edit_memory(
        root,
        path=path,
        why="seed the fixture's relation",
        operation={**operation, "transition_token": preview["transition_token"]},
    )


_VOLATILE_LINES = (
    re.compile(r"(?m)^exomem_id:\s*[^\n]+\n"),
    re.compile(r"(?m)^(?:created|updated):\s*[^\n]+\n"),
    re.compile(r"(?m)^(?:plan|record)_audit:\s*[^\n]+\n"),
    re.compile(r"(?m)^# exomem-(?:plan|record)-audit:\s*[^\n]+\n"),
)


_WORLD_REGISTRIES = frozenset(
    {f"{KB}/_Schema/entity-types.yaml", f"{KB}/_Schema/relation-registry.yaml"}
)


def logical_state_sha256(root: Path, key_to_path: Mapping[str, str], *, world_id: str) -> str:
    """Digest the canonical pages a world build produced, minus minted ids and clocks.

    Writers mint ``exomem_id`` values, stamp wall-clock ``updated`` times on
    frontmatter edits and chain Records audit receipts; none of those are
    fixture semantics, so they are dropped. Paths, titles, types, aliases,
    bodies, Records manifests and item fields all remain covered.
    """

    pages: list[tuple[str, str]] = []
    kb = Path(root) / KB
    for path in sorted(kb.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".yaml", ".yml", ".json"}:
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(kb).parts
        if path.name in {"index.md", "log.md"} or parts[0] == "_Governance":
            continue
        if any(part.startswith(".") for part in parts):
            continue
        # From the schema folder only the registries the world declares count:
        # scaffold defaults are product content, and review receipts are
        # minted under random names.
        if parts[0] == "_Schema" and relative not in _WORLD_REGISTRIES:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _VOLATILE_LINES:
            text = pattern.sub("", text)
        text = re.sub(r"(?m)^record_audit:\s*\{[^\n]*\}\n", "", text)
        pages.append((relative, text))
    return sha256_json({"world_id": world_id, "key_to_path": dict(key_to_path), "pages": pages})


@dataclass(frozen=True)
class BuiltWorld:
    world_id: str
    spec_sha256: str
    key_to_path: dict[str, str]
    logical_sha256: str


def _run_isolated_world_request(request_path: str, result_path: str) -> None:
    """Child-process entry point: build one world under private state."""

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    spec = _spec_from_plain(request["spec"])
    root = Path(request["root"])
    key_to_path = build_world_in_process(root, spec)
    payload = {
        "key_to_path": key_to_path,
        "logical_sha256": logical_state_sha256(root, key_to_path, world_id=request["world_id"]),
    }
    Path(result_path).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _plain_spec(spec: PreCapture) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(spec)))


def build_world(root: Path, spec: PreCapture, *, world_id: str) -> BuiltWorld:
    """Build ``spec`` in a child with private state, leases, config and logs.

    Mirrors the context-activation corpus build: the caller's environment and
    Exomem singletons are never rebound, and any writer refusal fails the
    build instead of falling back to hand-written fixture bytes.
    """

    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="exomem-memory-loop-") as runtime_raw:
        runtime = Path(runtime_raw)
        paths = {
            name: runtime / name
            for name in ("state", "xdg_state", "logs", "call_ledger", "writer_lease", "tmp")
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        request = runtime / "request.json"
        result = runtime / "result.json"
        request.write_text(
            json.dumps({"root": str(root), "world_id": world_id, "spec": _plain_spec(spec)}, sort_keys=True),
            encoding="utf-8",
        )
        environment = {key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")}
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    item
                    for item in (
                        str(repository / "src"),
                        str(repository / "benchmarks"),
                        environment.get("PYTHONPATH", ""),
                    )
                    if item
                ),
                "EXOMEM_STATE_ROOT": str(paths["state"]),
                "EXOMEM_CONFIG_PATH": str(runtime / "config.json"),
                "EXOMEM_LOG_DIR": str(paths["logs"]),
                "EXOMEM_CALL_LEDGER_DIR": str(paths["call_ledger"]),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(paths["writer_lease"]),
                "EXOMEM_LEASE_COORDINATOR_DB": str(runtime / "lease-coordinator.sqlite"),
                "EXOMEM_VAULT_PATH": str(root),
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_GRAPH_DRAIN": "1",
                "EXOMEM_DISABLE_GRAPH_SCHEDULING": "1",
                "XDG_STATE_HOME": str(paths["xdg_state"]),
                "TMPDIR": str(paths["tmp"]),
            }
        )
        code = (
            "from epistemic.memory_loop.contract import _run_isolated_world_request; "
            f"_run_isolated_world_request({str(request)!r}, {str(result)!r})"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=240,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise FixtureError("isolated world build exceeded 240 seconds") from error
        if completed.returncode != 0 or not result.is_file():
            detail = completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()
            raise FixtureError(f"isolated world build failed with exit {completed.returncode}: {detail}")
        payload = json.loads(result.read_text(encoding="utf-8"))
    return BuiltWorld(
        world_id=world_id,
        spec_sha256=sha256_json(spec),
        key_to_path=dict(payload["key_to_path"]),
        logical_sha256=payload["logical_sha256"],
    )


# --------------------------------------------------------------------------- #
# Canonical readback
# --------------------------------------------------------------------------- #

def _split_page(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    try:
        data = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), body


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


#: Folders that retain input rather than hold compiled knowledge: Sources
#: (episode recaps included) and Evidence. They are never a cluster home and
#: never count toward co-location or fragmentation; attribution and hedging
#: checks still read them.
RETAINED_INPUT_ROOTS: tuple[str, ...] = (f"{KB}/Sources/", f"{KB}/Evidence/")


def is_retained_input(path: str) -> bool:
    return path.startswith(RETAINED_INPUT_ROOTS)


@dataclass(frozen=True)
class PageView:
    path: str
    page_type: str
    title: str
    status: str
    entity_type: str
    aliases: tuple[str, ...]
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def names(self) -> tuple[str, ...]:
        stem = Path(self.path).stem
        return tuple(dict.fromkeys((self.title, stem, *self.aliases)))


@dataclass(frozen=True)
class EdgeView:
    """One relation fact as the product derives it: Markdown relation sections,
    semantic-unit relations and frontmatter relations (``sources``,
    ``supersedes``, ``superseded_by`` and the rest), with the product's own
    wikilink resolution (path, stem, then title; never aliases)."""

    source: str
    relation: str
    target: str
    origin: str
    status: str
    #: The registry family: a core relation's own, an extension's parent's.
    family: str = ""

    @property
    def active(self) -> bool:
        return self.status in {"core", "extension", "alias"}

    @property
    def extension(self) -> bool:
        return self.active and self.relation not in _core_relations()


@dataclass(frozen=True)
class RecordView:
    collection: str
    item_key: str
    fields: dict[str, str]


@dataclass(frozen=True)
class VaultState:
    pages: dict[str, PageView]
    records: tuple[RecordView, ...]
    edges: tuple[EdgeView, ...] = ()

    def entities(self) -> tuple[PageView, ...]:
        return tuple(
            page for page in self.pages.values() if page.page_type == "entity" and page.status == "active"
        )


def _core_relations() -> frozenset[str]:
    from exomem import relation_registry

    return frozenset(relation_registry.core_registry().keys)


def _families(relations: Iterable[str]) -> frozenset[str]:
    """The core registry families of ``relations``."""

    from exomem import relation_registry

    registry = relation_registry.core_registry()
    return frozenset(
        definition.family for relation in relations if (definition := registry.definition(relation)) is not None
    )


def _product_edges(root: Path) -> tuple[EdgeView, ...]:
    from exomem import semantic_contract

    semantic_contract.reset_corpus_context_cache()
    context = semantic_contract.build_corpus_context(root)
    edges = []
    for fact in context.relation_facts:
        if fact.target_status != "resolved" or fact.origin == "wikilink":
            continue
        edges.append(
            EdgeView(
                source=fact.logical_source_path,
                relation=fact.canonical_relation or fact.raw_relation,
                target=fact.logical_target_path,
                origin=fact.origin,
                status=fact.registry_status,
                family=fact.family or "",
            )
        )
    return tuple(sorted(set(edges), key=lambda edge: (edge.source, edge.relation, edge.target, edge.origin)))


def read_state(root: Path) -> VaultState:
    """Read canonical pages, the product's relation facts and Records items."""

    root = Path(root)
    kb = root / KB
    pages: dict[str, PageView] = {}
    records: list[RecordView] = []
    for path in sorted(kb.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(kb).parts
        if parts[0].startswith((".", "_")) or path.name in {"index.md", "log.md"}:
            continue
        frontmatter, body = _split_page(path.read_text(encoding="utf-8"))
        page_type = str(frontmatter.get("type") or "")
        if page_type == "record":
            records.append(
                RecordView(
                    collection=path.parent.parent.joinpath("_collection.md").relative_to(root).as_posix(),
                    item_key=str(frontmatter.get("record_id") or path.stem),
                    fields={
                        str(key): "" if value is None else str(value)
                        for key, value in frontmatter.items()
                        if key not in {"type", "collection_id", "record_id", "schema_version"}
                    },
                )
            )
            continue
        pages[relative] = PageView(
            path=relative,
            page_type=page_type,
            title=str(frontmatter.get("title") or path.stem),
            status=str(frontmatter.get("status") or "active"),
            entity_type=str(frontmatter.get("entity_type") or ""),
            aliases=_as_tuple(frontmatter.get("aliases")),
            body=body,
            frontmatter=frontmatter,
        )
    return VaultState(pages=pages, records=tuple(records), edges=_product_edges(root))


# --------------------------------------------------------------------------- #
# Expectations
# --------------------------------------------------------------------------- #

#: Bump when any predicate's code changes meaning; it is part of every
#: evaluator digest, so a semantic change voids runs bound to the old one.
SEMANTICS_VERSION = 4

#: Core relations that record that two things are connected without saying
#: how. They are honest when nothing more precise is supported, and never
#: satisfy an expectation for a specific supported meaning.
GENERIC_RELATIONS: frozenset[str] = frozenset({"relates_to", "links_to", "mentions"})

#: The sentinel for "any active governed extension relation": the route for a
#: reusable meaning the core registry lacks. Its semantics are reviewed where
#: it is registered; a fixture can only require that one is used.
EXTENSION = "extension"

#: Parent families an extension may not carry to satisfy :data:`EXTENSION`.
#: An extension inherits its parent's family, so one parented on ``owns`` or
#: ``supersedes`` says ownership or supersession under a new name, not a
#: governed meaning the core registry lacks.
NON_GOVERNED_FAMILIES: frozenset[str] = frozenset(
    {"ownership", "composition", "supersession", "duplication", "contradiction", "causality"}
)


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'").casefold()).strip()


def _has_marker(text: str, marker: str) -> bool:
    """``marker`` as a whole word or phrase: bounded on both sides."""

    pattern = r"(?<![a-z0-9])" + re.escape(_fold(marker)) + r"(?![a-z0-9])"
    return re.search(pattern, _fold(text)) is not None


def _any(text: str, markers: Iterable[str]) -> bool:
    return any(_has_marker(text, marker) for marker in markers)


def _new_lines(before: VaultState, page: PageView) -> tuple[str, ...]:
    """Lines of ``page`` that its pre-capture version did not have."""

    old = before.pages.get(page.path)
    previous = set(old.body.splitlines()) if old is not None else set()
    return tuple(line for line in page.body.splitlines() if line.strip() and line not in previous)


def _new_lines_in_context(before: VaultState, page: PageView) -> tuple[tuple[str, str], ...]:
    """Each new line with the context a reader sees it in: the page title and
    the nearest heading above it. A source or a date carried by a section
    heading ("## The miller's report") qualifies the bullets beneath it."""

    old = before.pages.get(page.path)
    previous = set(old.body.splitlines()) if old is not None else set()
    heading = ""
    found = []
    for line in page.body.splitlines():
        if re.match(r"^#{1,6}\s", line):
            heading = line
        if line.strip() and line not in previous:
            found.append((line, f"{page.title}\n{heading}\n{line}"))
    return tuple(found)


def _knowledge_pages(state: VaultState) -> tuple[PageView, ...]:
    """Active pages that can be a home: retained input excluded."""

    return tuple(
        page for page in state.pages.values() if page.status == "active" and not is_retained_input(page.path)
    )


@dataclass(frozen=True)
class Select:
    """Which pages an expectation reads.

    ``key`` names one pre-capture page. Otherwise ``kind`` chooses active
    entities (optionally of ``entity_type``) or any active knowledge page
    (retained input excluded), whose names carry every token and, when given,
    one of ``any_tokens``; ``created_only`` keeps pages the capture created.
    """

    key: str | None = None
    entity_type: str | None = None
    tokens: tuple[str, ...] = ()
    kind: Literal["entity", "page"] = "entity"
    created_only: bool = False
    any_tokens: tuple[str, ...] = ()

    def matches(
        self, world: Mapping[str, str], state: VaultState, before: VaultState | None = None
    ) -> tuple[PageView, ...]:
        if self.key is not None:
            page = state.pages.get(world.get(self.key, ""))
            return (page,) if page is not None and page.status == "active" else ()
        pool = state.entities() if self.kind == "entity" else _knowledge_pages(state)
        return tuple(
            page
            for page in pool
            if (self.entity_type is None or page.entity_type == self.entity_type)
            and all(any(_has_marker(name, token) for name in page.names) for token in self.tokens)
            and (
                not self.any_tokens
                or any(_has_marker(name, token) for name in page.names for token in self.any_tokens)
            )
            and not (self.created_only and before is not None and page.path in before.pages)
        )


@dataclass(frozen=True)
class Result:
    key: str
    polarity: Literal["positive", "negative"]
    outcome: Literal["pass", "fail"]
    detail: str


def _result(key: str, polarity: str, ok: bool, detail: str) -> Result:
    return Result(key, polarity, "pass" if ok else "fail", detail)  # type: ignore[arg-type]


@dataclass(frozen=True)
class EntityCount:
    """Exactly ``exactly`` pages match (identity creation and no duplication)."""

    key: str
    polarity: Literal["positive", "negative"]
    select: Select
    exactly: int
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        found = self.select.matches(world, after, before)
        return _result(self.key, self.polarity, len(found) == self.exactly, f"{len(found)} match: {[p.path for p in found]}")


@dataclass(frozen=True)
class EntityIntact:
    """A pre-capture page keeps its path, status, type, title and alias set."""

    key: str
    polarity: Literal["positive", "negative"]
    entity: str
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        path = world.get(self.entity, "")
        old, new = before.pages.get(path), after.pages.get(path)
        if old is None:
            return _result(self.key, self.polarity, False, f"{self.entity} was not in the pre-capture world")
        if new is None or new.status != "active":
            return _result(self.key, self.polarity, False, f"{path} is gone or inactive")
        changes = [
            name
            for name, a, b in (
                ("entity_type", old.entity_type, new.entity_type),
                ("title", old.title, new.title),
                ("aliases", sorted(map(_fold, old.aliases)), sorted(map(_fold, new.aliases))),
            )
            if a != b
        ]
        return _result(self.key, self.polarity, not changes, f"changed: {changes}")


@dataclass(frozen=True)
class Distinct:
    """Two selections each resolve to one active page, and not the same one."""

    key: str
    polarity: Literal["positive", "negative"]
    first: Select
    second: Select
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        a, b = self.first.matches(world, after, before), self.second.matches(world, after, before)
        ok = len(a) == 1 and len(b) == 1 and a[0].path != b[0].path
        return _result(self.key, self.polarity, ok, f"{[p.path for p in a]} vs {[p.path for p in b]}")


@dataclass(frozen=True)
class Mentions:
    """Exactly one selected page carries at least one marker of every group.

    With ``new_lines`` only lines the capture added count, so text the page
    already had cannot satisfy it.
    """

    key: str
    polarity: Literal["positive", "negative"]
    select: Select
    groups: tuple[tuple[str, ...], ...]
    reason: str
    new_lines: bool = False

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        found = self.select.matches(world, after, before)
        if len(found) != 1:
            return _result(self.key, self.polarity, False, f"{len(found)} pages selected")
        text = "\n".join(_new_lines(before, found[0])) if self.new_lines else found[0].body
        missing = [group for group in self.groups if not _any(text, group)]
        return _result(self.key, self.polarity, not missing, f"{found[0].path} missing: {missing}")


_WIKILINK = re.compile(r"\[\[[^\[\]\n]*\]\]")


@dataclass(frozen=True)
class NoNewMention:
    """No selected knowledge page gained a line carrying any of ``markers``.

    With ``ignore_links`` a marker that appears only inside a wikilink does
    not count: a backlink to a new home is a scope-owned navigation update,
    while restating that home's content on the page is not.
    """

    key: str
    polarity: Literal["positive", "negative"]
    markers: tuple[str, ...]
    reason: str
    select: Select | None = None
    ignore_links: bool = False

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        pages = self.select.matches(world, after, before) if self.select is not None else _knowledge_pages(after)
        hits = [
            f"{page.path}: {marker}"
            for page in pages
            for line in _new_lines(before, page)
            for marker in self.markers
            if _has_marker(_WIKILINK.sub(" ", line) if self.ignore_links else line, marker)
        ]
        return _result(self.key, self.polarity, not hits, f"new mentions: {hits}")


@dataclass(frozen=True)
class NoMixedLines:
    """No new line of a knowledge page carries markers of two of ``groups``.

    Objects the decomposition keeps apart are never fused into one statement,
    whatever page that statement sits on.
    """

    key: str
    polarity: Literal["positive", "negative"]
    groups: tuple[tuple[str, ...], ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        mixed = [
            f"{page.path}: {line.strip()}"
            for page in _knowledge_pages(after)
            for line in _new_lines(before, page)
            if sum(1 for group in self.groups if _any(line, group)) > 1
        ]
        return _result(self.key, self.polarity, not mixed, f"mixed lines: {mixed}")


@dataclass(frozen=True)
class CoMention:
    """No new line of a knowledge page, read with its title and heading, names
    both a ``first`` and a ``second`` marker (e.g. an item and a resolved owner)."""

    key: str
    polarity: Literal["positive", "negative"]
    first: tuple[str, ...]
    second: tuple[str, ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        hits = [
            f"{page.path}: {line.strip()}"
            for page in _knowledge_pages(after)
            for line, context in _new_lines_in_context(before, page)
            if _any(context, self.first) and _any(context, self.second)
        ]
        return _result(self.key, self.polarity, not hits, f"co-mentions: {hits}")


@dataclass(frozen=True)
class NewPages:
    """Exactly ``exactly`` knowledge pages were created (retained input excluded).

    ``tolerate`` names exact titles a capture may also create without counting
    (a recurring product promoted to an entity is not fragmentation);
    ``exclude_types`` drops whole page types from the count.
    """

    key: str
    polarity: Literal["positive", "negative"]
    exactly: int
    reason: str
    exclude: tuple[str, ...] = ()
    exclude_types: tuple[str, ...] = ()
    tolerate: tuple[str, ...] = ()

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        tolerated = {_fold(title) for title in self.tolerate}
        created = sorted(
            page.path
            for page in _knowledge_pages(after)
            if page.path not in before.pages
            and page.page_type != "collection"
            and page.page_type not in self.exclude_types
            and not page.path.startswith(self.exclude)
            and _fold(page.title) not in tolerated
        )
        return _result(self.key, self.polarity, len(created) == self.exactly, f"new pages: {created}")


@dataclass(frozen=True)
class AttributedLines:
    """A reported or attributed claim keeps its source and its hedge.

    Every new line anywhere in the vault (retained input included) that
    carries a claim marker, read with its title and heading, also names its
    source and a reporting or uncertainty marker, so the claim is never
    restated as a direct fact. Unless ``allow_none``, at least one such line
    must exist (the claim was captured); with it, the check guards hearsay
    that may rightly go unwritten.
    """

    key: str
    polarity: Literal["positive", "negative"]
    claim: tuple[str, ...]
    source: tuple[str, ...]
    hedge: tuple[str, ...]
    reason: str
    allow_none: bool = False

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        lines = [
            (page.path, line, context)
            for page in after.pages.values()
            for line, context in _new_lines_in_context(before, page)
            if _any(line, self.claim)
        ]
        bare = [
            f"{path}: {line.strip()}"
            for path, line, context in lines
            if not (_any(context, self.source) and _any(context, self.hedge))
        ]
        ok = (bool(lines) or self.allow_none) and not bare
        return _result(self.key, self.polarity, ok, f"{len(lines)} claim lines; unattributed: {bare}")


@dataclass(frozen=True)
class LinesCarry:
    """At least one new knowledge line carries a claim marker, and every such
    line, read with its title and heading, carries a marker from every group
    (an old event keeps its own time; a choice names what was chosen).
    ``select`` narrows the pages read."""

    key: str
    polarity: Literal["positive", "negative"]
    claim: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    reason: str
    select: Select | None = None

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        pages = self.select.matches(world, after, before) if self.select is not None else _knowledge_pages(after)
        lines = [
            (page.path, line, context)
            for page in pages
            for line, context in _new_lines_in_context(before, page)
            if _any(line, self.claim)
        ]
        bare = [
            f"{path}: {line.strip()}"
            for path, line, context in lines
            if not all(_any(context, group) for group in self.groups)
        ]
        ok = bool(lines) and not bare
        return _result(self.key, self.polarity, ok, f"{len(lines)} claim lines; unqualified: {bare}")


@dataclass(frozen=True)
class HedgedLines:
    """New lines and Records values about ``subject`` that assert a cause keep
    their uncertainty, everywhere in the vault."""

    key: str
    polarity: Literal["positive", "negative"]
    subject: tuple[str, ...]
    causal: tuple[str, ...]
    hedge: tuple[str, ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        lines = [(page.path, line) for page in after.pages.values() for line in _new_lines(before, page)]
        old_items = {(item.collection, item.item_key): item.fields for item in before.records}
        lines += [
            (item.collection, value)
            for item in after.records
            if old_items.get((item.collection, item.item_key)) != item.fields
            for value in item.fields.values()
        ]
        bad = [
            f"{where}: {line.strip()}"
            for where, line in lines
            if _any(line, self.subject) and _any(line, self.causal) and not _any(line, self.hedge)
        ]
        return _result(self.key, self.polarity, not bad, f"unhedged causal lines: {bad}")


@dataclass(frozen=True)
class CoLocated:
    """The new knowledge lines carrying each marker group all sit on one page,
    an admissible home.

    A line is read with its page title and nearest heading, as a reader sees
    it. Facts that belong together (a label and a reported formulation of the
    same product) land in one canonical home rather than being spread or
    copied, and that home is one of ``homes`` rather than a related page
    whose scope does not own them. Retained input is never a home.
    """

    key: str
    polarity: Literal["positive", "negative"]
    groups: tuple[tuple[str, ...], ...]
    homes: tuple[Select, ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        holders: list[set[str]] = []
        for group in self.groups:
            holders.append(
                {
                    page.path
                    for page in _knowledge_pages(after)
                    for _line, context in _new_lines_in_context(before, page)
                    if _any(context, group)
                }
            )
        pages = set().union(*holders) if holders else set()
        admissible = {page.path for select in self.homes for page in select.matches(world, after, before)}
        ok = len(pages) == 1 and all(holder == pages for holder in holders) and pages <= admissible
        return _result(self.key, self.polarity, ok, f"holders {[sorted(h) for h in holders]}; admissible {sorted(admissible)}")


@dataclass(frozen=True)
class Admissible:
    """One truthful way an expected edge may be written.

    ``relation`` is a relation id (core or alias canonical) or
    :data:`EXTENSION` for any active governed extension. ``direction`` is
    ``forward`` (from the expectation's source to its target), ``reverse``
    or ``either``.
    """

    relation: str
    direction: Literal["forward", "reverse", "either"]


def _edge_admissible(edge: EdgeView, admissible: tuple[Admissible, ...], forward: bool) -> bool:
    if not edge.active or edge.relation in GENERIC_RELATIONS:
        return False
    for option in admissible:
        if option.direction != "either" and (option.direction == "forward") != forward:
            continue
        if option.relation == EXTENSION and edge.extension and edge.family not in NON_GOVERNED_FAMILIES:
            return True
        if option.relation == edge.relation:
            return True
    return False


def _edges_between(
    state: VaultState, sources: Iterable[PageView], targets: Iterable[PageView]
) -> list[tuple[EdgeView, bool]]:
    """Edges between the two page sets, each with whether it runs source to target."""

    source_paths = {page.path for page in sources}
    target_paths = {page.path for page in targets}
    found = []
    for edge in state.edges:
        if edge.source in source_paths and edge.target in target_paths:
            found.append((edge, True))
        elif edge.source in target_paths and edge.target in source_paths:
            found.append((edge, False))
    return found


@dataclass(frozen=True)
class TypedEdge:
    """A truthful typed edge connects the two selections.

    Only an active relation listed in ``admissible``, in its listed direction,
    satisfies it; a generic relation never does.
    """

    key: str
    polarity: Literal["positive", "negative"]
    source: Select
    target: Select
    admissible: tuple[Admissible, ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        found = _edges_between(
            after, self.source.matches(world, after, before), self.target.matches(world, after, before)
        )
        good = [edge for edge, forward in found if _edge_admissible(edge, self.admissible, forward)]
        return _result(self.key, self.polarity, bool(good), f"edges: {[(e.relation, f) for e, f in found]}")


@dataclass(frozen=True)
class EdgeCount:
    """At least ``at_least`` of ``targets`` have a truthful typed edge with ``source``."""

    key: str
    polarity: Literal["positive", "negative"]
    source: Select
    targets: tuple[Select, ...]
    admissible: tuple[Admissible, ...]
    at_least: int
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        sources = self.source.matches(world, after, before)
        linked = [
            target
            for target in self.targets
            if any(
                _edge_admissible(edge, self.admissible, forward)
                for edge, forward in _edges_between(after, sources, target.matches(world, after, before))
            )
        ]
        return _result(
            self.key, self.polarity, len(linked) >= self.at_least, f"{len(linked)}/{len(self.targets)} linked"
        )


@dataclass(frozen=True)
class NoEdgeBetween:
    """No edge (either direction) between the selections carries a forbidden
    meaning: a relation in ``forbidden``, or any relation of a forbidden
    relation's family (an extension inherits its parent's), when it is given;
    otherwise any relation not in ``tolerated``. It reads the whole vault after
    capture, so an edge the world already had must be retired too."""

    key: str
    polarity: Literal["positive", "negative"]
    first: Select
    second: Select
    reason: str
    forbidden: tuple[str, ...] = ()
    tolerated: tuple[str, ...] = ()

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        found = _edges_between(
            after, self.first.matches(world, after, before), self.second.matches(world, after, before)
        )
        families = _families(self.forbidden)
        bad = [
            (edge.source, edge.relation, edge.target)
            for edge, _forward in found
            if (
                edge.relation in self.forbidden or edge.family in families
                if self.forbidden
                else edge.relation not in self.tolerated
            )
        ]
        return _result(self.key, self.polarity, not bad, f"edges: {bad}")


@dataclass(frozen=True)
class NoNewEdge:
    """No new edge of ``relations`` (or of any relation) reaches ``target``."""

    key: str
    polarity: Literal["positive", "negative"]
    reason: str
    relations: tuple[str, ...] = ()
    target: Select | None = None

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        targets = (
            {page.path for page in self.target.matches(world, after, before)} if self.target else None
        )
        previous = set(before.edges)
        added = [
            f"{edge.source} {edge.relation} {edge.target}"
            for edge in after.edges
            if edge not in previous
            and (not self.relations or edge.relation in self.relations)
            and (targets is None or edge.target in targets or edge.source in targets)
        ]
        return _result(self.key, self.polarity, not added, f"new edges: {added}")


_PLANNING_TITLE = re.compile(r"(?i)\b(?:plan|plans|planning|todo|to-do|intentions?)\b")


@dataclass(frozen=True)
class NoNewPlanning:
    """No commitment appears for a mentioned possibility.

    Judged only where commitments live: a new page in the Planning tree, a
    Planning item (``plan_id`` or the planning profile) or a new collection
    titled as a plan, or a new item in such a collection. With ``markers`` it
    fails only on one that names a marker, so an unrelated plan is not this
    possibility. Records outside a planning collection are never read: a
    Records line that notes the possibility is tentative intent kept as
    history, and stays quiet.
    """

    key: str
    polarity: Literal["positive", "negative"]
    reason: str
    markers: tuple[str, ...] = ()

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        planning = {path for path, page in after.pages.items() if _is_planning(page)}
        created = [
            path
            for path in sorted(planning - set(before.pages))
            if not self.markers or _any(_page_text(after.pages[path]), self.markers)
        ]
        old_items = {(item.collection, item.item_key) for item in before.records}
        created += [
            f"{item.collection}#{item.item_key}"
            for item in after.records
            if item.collection in planning
            and (item.collection, item.item_key) not in old_items
            and (not self.markers or any(_any(value, self.markers) for value in item.fields.values()))
        ]
        return _result(self.key, self.polarity, not created, f"new planning: {created}")


def _is_planning(page: PageView) -> bool:
    return (
        "plan_id" in page.frontmatter
        or page.frontmatter.get("semantic_profile") == "planning"
        or page.path.startswith(f"{KB}/Planning/")
        or (page.page_type == "collection" and _PLANNING_TITLE.search(page.title) is not None)
    )


def _page_text(page: PageView) -> str:
    values = " ".join(str(value) for value in page.frontmatter.values())
    return f"{page.title}\n{values}\n{page.body}"


@dataclass(frozen=True)
class FieldIs:
    """A Records field matcher: exact values, all tokens, any marker, or honestly empty."""

    equals: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    empty: bool = False

    def matches(self, value: str) -> bool:
        folded = _fold(value)
        if self.empty and folded in _EMPTY_VALUES:
            return True
        if self.equals and folded in {_fold(item) for item in self.equals}:
            return True
        if self.tokens and all(_has_marker(value, token) for token in self.tokens):
            return True
        if self.any_of and folded not in _EMPTY_VALUES and _any(value, self.any_of):
            return True
        return False


#: What an honest "we do not know" looks like in a Records field.
_EMPTY_VALUES = frozenset({"", "unknown", "none", "null", "n/a", "-", "not known", "unattributed"})


def _records(world: Mapping[str, str], state: VaultState, collection: str, where) -> list[RecordView]:
    path = world.get(collection, collection)
    return [
        item
        for item in state.records
        if item.collection == path
        and all(matcher.matches(item.fields.get(name, "")) for name, matcher in where)
    ]


@dataclass(frozen=True)
class RecordItem:
    """Exactly one item of a Records collection matches ``where`` and every ``expect``."""

    key: str
    polarity: Literal["positive", "negative"]
    collection: str
    where: tuple[tuple[str, FieldIs], ...]
    expect: tuple[tuple[str, FieldIs], ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        items = _records(world, after, self.collection, self.where)
        if len(items) != 1:
            return _result(self.key, self.polarity, False, f"{len(items)} items match {self.where}")
        wrong = [
            (name, items[0].fields.get(name, ""))
            for name, matcher in self.expect
            if not matcher.matches(items[0].fields.get(name, ""))
        ]
        return _result(self.key, self.polarity, not wrong, f"wrong: {wrong}")


@dataclass(frozen=True)
class RecordsKept:
    """Every pre-capture item of a collection is still present with its fields."""

    key: str
    polarity: Literal["positive", "negative"]
    collection: str
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        path = world.get(self.collection, self.collection)
        old = {item.item_key: item.fields for item in before.records if item.collection == path}
        new = {item.item_key: item.fields for item in after.records if item.collection == path}
        lost = [key for key, values in old.items() if new.get(key) != values]
        return _result(self.key, self.polarity, not lost, f"changed or lost: {lost}")


@dataclass(frozen=True)
class LatestRecord:
    """The newest item by ``order_field`` among ``where`` matches ``expect``."""

    key: str
    polarity: Literal["positive", "negative"]
    collection: str
    where: tuple[tuple[str, FieldIs], ...]
    order_field: str
    expect: tuple[tuple[str, FieldIs], ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        items = _records(world, after, self.collection, self.where)
        if not items:
            return _result(self.key, self.polarity, False, "no matching item")
        latest = max(items, key=lambda item: item.fields.get(self.order_field, ""))
        wrong = [
            (name, latest.fields.get(name, ""))
            for name, matcher in self.expect
            if not matcher.matches(latest.fields.get(name, ""))
        ]
        return _result(self.key, self.polarity, not wrong, f"latest {latest.item_key}: {wrong}")


@dataclass(frozen=True)
class AnyOf:
    """Passes when any option passes: one meaning with several honest shapes."""

    key: str
    polarity: Literal["positive", "negative"]
    options: tuple[Any, ...]
    reason: str

    def evaluate(self, world: Mapping[str, str], before: VaultState, after: VaultState) -> Result:
        results = [option.evaluate(world, before, after) for option in self.options]
        ok = any(result.outcome == "pass" for result in results)
        return _result(self.key, self.polarity, ok, "; ".join(r.detail for r in results))


Expectation = (
    EntityCount
    | EntityIntact
    | Distinct
    | Mentions
    | NoNewMention
    | NoMixedLines
    | CoMention
    | NewPages
    | AttributedLines
    | LinesCarry
    | HedgedLines
    | CoLocated
    | TypedEdge
    | EdgeCount
    | NoEdgeBetween
    | NoNewEdge
    | NoNewPlanning
    | RecordItem
    | RecordsKept
    | LatestRecord
    | AnyOf
)


@dataclass(frozen=True)
class CaptureCheck:
    results: tuple[Result, ...]

    @property
    def accepted(self) -> bool:
        return bool(self.results) and all(result.outcome == "pass" for result in self.results)

    def failed(self) -> tuple[str, ...]:
        return tuple(result.key for result in self.results if result.outcome == "fail")

    def outcome(self, key: str) -> str:
        return next(result.outcome for result in self.results if result.key == key)


def check_expectations(
    expectations: Iterable[Expectation],
    *,
    world: Mapping[str, str],
    before: VaultState,
    after: VaultState,
) -> CaptureCheck:
    return CaptureCheck(tuple(item.evaluate(world, before, after) for item in expectations))


def selects_of(expectation: Any) -> tuple[Select, ...]:
    """Every selection an expectation reads, through ``AnyOf`` options too."""

    found: list[Select] = []
    for name in ("select", "first", "second", "source", "target"):
        value = getattr(expectation, name, None)
        if isinstance(value, Select):
            found.append(value)
    for name in ("homes", "targets"):
        found.extend(item for item in getattr(expectation, name, ()) if isinstance(item, Select))
    for option in getattr(expectation, "options", ()):
        found.extend(selects_of(option))
    return tuple(found)


@dataclass(frozen=True)
class LaterUse:
    """Blind-rubric anchors for the later fresh-session answer, written before any run.

    ``useful`` is what a useful answer conveys; ``wrong`` lists answers that
    would be poison (a hearsay asserted as fact, a seller presented as the
    producer). ``expected_status`` is pinned only where the specification
    fixes it: a bare shared name keeps its ambiguity. Elsewhere the packet's
    status is the compiler's business and usefulness is judged on the answer.
    """

    useful: str
    wrong: tuple[str, ...] = ()
    expected_status: str | None = None
    #: The principal the later fresh session runs as: the owner, never an
    #: unscoped or guest caller.
    audience: str = "owner"


# --------------------------------------------------------------------------- #
# Candidate destinations and provenance, frozen before any write
# --------------------------------------------------------------------------- #

PROVENANCE: tuple[str, ...] = (
    "direct",
    "reported",
    "comparison",
    "inference",
    "attributed",
    "event",
    "preference",
    "baseline",
    "none",
)


@dataclass(frozen=True)
class Partition:
    """The four axes a decomposition separates before any destination is chosen."""

    retrieval_question: str
    subject: str
    temporal_episode: str
    epistemic_role: str


@dataclass(frozen=True)
class Candidate:
    """One durable change the episode supports, and where it belongs.

    ``homes`` lists the admissible canonical homes, preferred first:
    ``existing:<world key>``, ``new:<home id>`` or ``none``. ``routes`` are
    the admissible episode routes (``episode_model`` route names).
    ``same_home_as`` names candidates that must share this one's home.
    ``checked_by`` names the expectations that enforce this destination and
    provenance in the vault a capture leaves.
    """

    key: str
    statement: str
    homes: tuple[str, ...]
    routes: tuple[str, ...]
    dispositions: tuple[str, ...]
    provenance: str
    checked_by: tuple[str, ...]
    attributed_to: str | None = None
    uncertain: bool = False
    same_home_as: tuple[str, ...] = ()
    partition: Partition | None = None


def validate_candidates(
    candidates: Iterable[Candidate],
    *,
    world_keys: Iterable[str],
    expectation_keys: Iterable[str],
) -> None:
    """Refuse a destination spec that names routes, homes or checks that do not exist."""

    from exomem import episode_model

    routes = set(episode_model._ROUTES)  # noqa: SLF001 - the product's own route vocabulary
    dispositions = set(episode_model._DISPOSITIONS)  # noqa: SLF001
    world = set(world_keys)
    checks = set(expectation_keys)
    items = tuple(candidates)
    keys = [item.key for item in items]
    if len(keys) != len(set(keys)):
        raise FixtureError("candidate keys must be unique")
    for item in items:
        if not item.homes or not item.routes or not item.dispositions:
            raise FixtureError(f"{item.key}: homes, routes and dispositions are required")
        if set(item.routes) - routes:
            raise FixtureError(f"{item.key}: unknown route {sorted(set(item.routes) - routes)}")
        if set(item.dispositions) - dispositions:
            raise FixtureError(f"{item.key}: unknown disposition")
        if item.provenance not in PROVENANCE:
            raise FixtureError(f"{item.key}: unknown provenance {item.provenance}")
        if item.provenance in {"reported", "attributed"} and not item.attributed_to:
            raise FixtureError(f"{item.key}: a {item.provenance} claim names its source")
        for home in item.homes:
            kind, _, name = home.partition(":")
            if kind == "existing" and name not in world:
                raise FixtureError(f"{item.key}: existing home {name} is not in the world")
            if kind not in {"existing", "new", "none"} or (kind != "none" and not name):
                raise FixtureError(f"{item.key}: malformed home {home}")
        if ("none" in item.homes) != ("routed" not in item.dispositions):
            raise FixtureError(f"{item.key}: a home of none pairs with a non-routed disposition")
        if not item.checked_by or set(item.checked_by) - checks:
            raise FixtureError(f"{item.key}: every candidate is enforced by named expectations")
        if set(item.same_home_as) - set(keys):
            raise FixtureError(f"{item.key}: same_home_as names an unknown candidate")


# --------------------------------------------------------------------------- #
# The evaluator semantics every fixture digest carries
# --------------------------------------------------------------------------- #


def _declared_defaults() -> dict[str, dict[str, Any]]:
    """Every contract type's field defaults.

    Digests omit fields left at their default, so a changed default would
    otherwise change meaning without moving any digest.
    """

    import sys

    module = sys.modules[__name__]
    defaults: dict[str, dict[str, Any]] = {}
    for name in sorted(vars(module)):
        value = getattr(module, name)
        if isinstance(value, type) and is_dataclass(value) and value.__module__ == __name__:
            declared = {}
            for item in fields(value):
                if item.default is not MISSING:
                    declared[item.name] = _plain(item.default)
                elif item.default_factory is not MISSING:
                    declared[item.name] = _plain(item.default_factory())
            if declared:
                defaults[name] = declared
    return defaults


def semantics_fingerprint() -> str:
    """The predicate semantics a fixture's evaluator digest is bound to:
    the semantics version, the evaluator constants, every declared default and
    the observation gate's own semantics (its gate functions and constants).
    Read at call time, so a changed constant moves every evaluator digest."""

    from . import observation

    return sha256_json(
        {
            "semantics_version": SEMANTICS_VERSION,
            "generic_relations": sorted(GENERIC_RELATIONS),
            "extension": EXTENSION,
            "non_governed_families": sorted(NON_GOVERNED_FAMILIES),
            "retained_input_roots": list(RETAINED_INPUT_ROOTS),
            "empty_values": sorted(_EMPTY_VALUES),
            "provenance": list(PROVENANCE),
            "planning_title": _PLANNING_TITLE.pattern,
            "defaults": _declared_defaults(),
            "observation_gate": observation.gate_semantics(),
        }
    )
