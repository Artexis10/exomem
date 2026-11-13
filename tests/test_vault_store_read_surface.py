"""The registered read surface must answer from one store snapshot, without projections.

OpenSpec make-vault-store-canonical 3.1/3.2. Unlike a sampled graph check, the coverage
check fails when the command registry gains a reader without a representative invocation.
Mixed commands enter through their registered read selectors and the dispatcher's classifier.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_collection_store_writer import manifest_path, manifest_text
from test_deferred_write_advisory import _prepare_custody as prepare_advisory_custody
from test_episode_memory_command import KEY as EPISODE
from test_episode_memory_command import _record as record_episode
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import (
    CLAIM,
    LAMP,
    UNIT_PAGES,
    record_store_reads,
    write_pages,
)
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_readers import EXTERNAL, SENTINEL, restrict_external

from exomem import (
    attention,
    commands,
    curation,
    derived_receipts,
    epistemic_graph,
    find,
    freshness,
    lexstore,
    plan_progress,
    record_formats,
    schema,
    semantic_writes,
    temporal,
    working_set_runtime,
)
from exomem import vault as vaults
from exomem.cli_ops import OpError
from exomem.collection_store import import_modes
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance import egress, operations
from exomem.governance.principal import request_scope
from exomem.vault_store import mode, ownership, read
from exomem.writer_lease import invoke_command

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(180),
]

SOURCE = "Knowledge Base/Sources/Beacon Survey.md"
DATA = "Knowledge Base/Records/dataset/readings.csv"
SECRET = "Knowledge Base/Notes/Secret Log.md"
DATASET = "Knowledge Base/Records/dataset/_collection.md"
PLANNING = manifest_path("planning")

# Each key names one read-only invocation that the registry declares (see `invocation_key`).
# Each sample reaches that reader, rather than deliberately failing input validation. Media,
# operational status and lookups of absent control-plane state may legitimately refuse.
SAMPLES = {
    "served_context": {},
    "configure_memory:inspect": {"action": "inspect"},
    "coordination_status": {},
    "bootstrap": {},
    "search": {"query": "lighthouse"},
    "fetch": {"id": CLAIM},
    "find": {"query": "lighthouse", "rerank": False},
    "suggest_links": {"path": CLAIM},
    "graph_context": {"path": CLAIM, "depth": 2},
    "viewer_map": {},
    "suggest_relations": {"path": CLAIM},
    "audit": {},
    "attention": {},
    "review_item_context": {"ref": "$review"},
    "overview": {"path": "Knowledge Base/Notes"},
    "evolution": {"query": "lighthouse"},
    "provenance_report": {"path": CLAIM},
    "propose_compilation": {"sources": [SOURCE]},
    "get": {"path": CLAIM, "links": True},
    "observe_memory:validate": {
        "operation": "validate", "path": LAMP, "category": "rule",
        "content": "Keep the lighthouse lantern lit.",
    },
    "query_data": {"path": DATA},
    "list_directory": {"path": "Knowledge Base/Notes", "recursive": True},
    "list_trash": {},
    "list_inbound_links": {"target": CLAIM},
    "schema_memory:infer": {"operation": "infer", "subject": "categories"},
    "schema_memory:validate": {"operation": "validate", "subject": "categories"},
    "schema_memory:diff": {"operation": "diff", "subject": "categories"},
    "schema_memory:inventory": {"operation": "inventory", "subject": "workflow-contracts"},
    "schema_memory:inspect": {"operation": "inspect", "subject": "relations"},
    "schema_memory:resolve": {
        "operation": "resolve", "subject": "workflow-contracts",
        "context": {"project": "lighthouse", "domain": None, "activity": None},
    },
    "schema_memory:preview": {
        "operation": "preview", "subject": "workflow-contracts",
        "proposal": {
            "type": "workflow-contract", "contract_id": "6f1c2ec5-7f14-4ce8-a54e-f94c8c95c378",
            "schema_version": 1, "key": "lantern-watch", "title": "Lantern Watch",
            "lifecycle": "active", "scope": {"projects": [], "domains": [], "activities": []},
            "planning": {"mode": "standalone"}, "companions": [],
            "capture": {"durable_intent": "explicit", "observed_outcomes": "explicit"},
            "planning_transition": "explicit-only",
        },
    },
    "schema_memory:resolve-entity-type": {
        "operation": "resolve-entity-type", "subject": "entity-types",
        "query": "lighthouse", "requested_type": "Lighthouse",
    },
    "schema_memory:propose-relation": {
        "operation": "propose-relation", "subject": "relations",
        "proposal": {
            "requested_label": "illuminates", "parent": "relates_to",
            "description": "A light source that lights another thing.", "direction": "directed",
        },
    },
    "schema_memory:census": {"operation": "census", "subject": "relations"},
    "schema_memory:propose": {"operation": "propose", "subject": "categories", "proposal": {}},
    "schema_memory:history": {"operation": "history", "subject": "context-roles"},
    "record_memory:describe": {"action": "describe"},
    "record_memory:validate": {
        "action": "validate", "manifest_path": "Knowledge Base/Records/Harbour/_collection.md",
        "manifest_text": manifest_text("records").replace("markdown-items", "sqlite"),
    },
    "record_memory:inspect": {"action": "inspect", "collection": DATASET},
    "record_memory:query": {"action": "query", "collection": DATASET},
    "record_memory:import:preview": {
        "action": "import", "collection": DATASET, "import_request": {"mode": "preview"},
    },
    "record_memory:import:status": {
        "action": "import", "collection": DATASET, "import_request": {"mode": "status"},
    },
    "record_memory:history": {"action": "history", "collection": DATASET, "item_key": "r-001"},
    "plan_memory:inspect": {"action": "inspect"},
    "plan_memory:validate": {
        "action": "validate", "manifest_path": "Knowledge Base/Planning/Harbour/_collection.md",
        "manifest_text": manifest_text("planning").replace("markdown-items", "sqlite"),
    },
    "plan_memory:query": {"action": "query", "collection": PLANNING},
    "plan_memory:history": {"action": "history", "collection": PLANNING, "plan_id": "missing"},
    "get_video_frames": {"path": "Knowledge Base/Notes/diagram.bin"},
    # This is the whole recall_context operation, through its registered execution name.
    "activate_context": {"turn": "What do we know about the lighthouse lantern?", "max_chars": 8000},
    "episode_memory:inspect": {"action": "inspect", "episode": EPISODE},
    "episode_memory:material": {
        "action": "material", "episode": EPISODE, "input_revision": 1, "material_key": "missing",
    },
    "episode_memory:candidates": {"action": "candidates", "episode": EPISODE},
    "episode_memory:coverage": {"action": "coverage", "episode": EPISODE},
    "ask_memory": {"query": "lighthouse", "rerank": False},
    "read_memory": {"path": CLAIM, "links": True},
    "browse_memory": {"path": "Knowledge Base/Notes", "mode": "list", "limit": 5},
    "compile_source": {"sources": [SOURCE]},
    "process_media:status": {"operation": "status", "path": "Knowledge Base/Notes/diagram.bin"},
    "review_memory:attention": {"mode": "attention"},
    "review_memory:activation": {"mode": "activation"},
    "review_memory:item": {"mode": "item", "ref": "$review"},
    "review_memory:audit": {"mode": "audit"},
    "review_memory:dispositions": {"mode": "dispositions"},
    "review_memory:provenance": {"mode": "provenance", "path": CLAIM},
    "review_memory:evolution": {"mode": "evolution", "query": "lighthouse"},
    "review_memory:compilation": {"mode": "compilation", "sources": [SOURCE.removesuffix(".md")]},
    "review_memory:stale": {"mode": "stale"},
    "review_memory:contradiction": {"mode": "contradiction"},
    "review_memory:unprocessed-sources": {"mode": "unprocessed-sources"},
    "review_memory:relation-debt": {"mode": "relation-debt"},
    "review_memory:relation-queue": {"mode": "relation-queue"},
    "review_memory:adoption": {"mode": "adoption"},
    "review_memory:upkeep": {"mode": "upkeep"},
    "review_memory:plan-progress": {"mode": "plan-progress"},
    "review_memory:write-advisory-result": {
        "mode": "write-advisory-result", "ref": "$advisory",
    },
    "review_memory:procedure_approvals": {"mode": "procedure_approvals"},
    "review_memory:held": {"mode": "held"},
    "review_memory:vocabulary": {"mode": "vocabulary"},
    "connect_memory:suggest-links": {"operation": "suggest-links", "path": CLAIM},
    "connect_memory:suggest-relations": {"operation": "suggest-relations", "path": CLAIM},
    "connect_memory:context": {"operation": "context", "path": LAMP, "depth": 2},
    "connect_memory:graph-context": {"operation": "graph-context", "path": CLAIM, "depth": 2},
    "connect_memory:inbound-links": {"operation": "inbound-links", "target": CLAIM},
    "connect_memory:resolve-entity": {"operation": "resolve-entity", "name": "Tide"},
    "connect_memory:resolve-relation": {
        "operation": "resolve-relation", "path": CLAIM, "query": "supports",
    },
    "adopt_vault:scan-only": {"mode": "scan-only", "path": "Knowledge Base/Notes"},
    "adoption_studio:status": {"action": "status", "run_id": "missing"},
    "adoption_studio:work-item": {"action": "work-item", "run_id": "missing"},
    "maintain_memory:audit": {"mode": "audit"},
    "maintain_memory:fix": {"mode": "fix"},
    "maintain_memory:reconcile": {"mode": "reconcile", "dry_run": True},
    "maintain_memory:backfill-ids": {"mode": "backfill-ids"},
    "maintain_memory:maintenance-job": {"mode": "maintenance-job"},
    "maintain_memory:structured-files": {"mode": "structured-files", "collection": DATASET},
    "maintain_memory:tag-variants": {"mode": "tag-variants"},
    "maintain_memory:collections-store": {"mode": "collections-store"},
    "maintain_memory:collections-store-adopt-local": {"mode": "collections-store-adopt-local"},
    "maintain_memory:collections-store-export": {"mode": "collections-store-export"},
    "maintain_memory:collections-cutover": {"mode": "collections-cutover"},
    "maintain_memory:curation:work-item": {
        "mode": "curation", "curation_action": "work-item", "paths": [CLAIM],
    },
    "maintain_memory:curation:preview": {
        "mode": "curation", "curation_action": "preview", "run_id": "cur-20260101-000000000000",
    },
    "maintain_memory:curation:status": {
        "mode": "curation", "curation_action": "status", "run_id": "cur-20260101-000000000000",
    },
    "manage_memory_file:list": {"operation": "list", "path": "Knowledge Base/Notes"},
    "manage_memory_file:create": {
        "operation": "create", "validate_only": True, "path": "Knowledge Base/Notes/Harbour Plan.md",
        "content": "---\ntitle: Harbour Plan\ntype: note\n---\n# Harbour Plan\n\nLight the lantern.\n",
    },
    "manage_memory_file:append": {
        "operation": "append", "validate_only": True, "path": CLAIM,
        "content": "\nThe keeper adds oil at dusk.\n",
    },
    "manage_memory_file:trash-list": {"operation": "trash-list"},
    "manage_memory_file:propose-reclassification": {
        "operation": "propose-reclassification", "path": SOURCE, "source_kind": "article",
    },
    "query_dataset": {"path": DATA},
    "read_media": {"path": "Knowledge Base/Notes/diagram.bin"},
    "observe_transfer": {},
    "govern_memory:list": {"operation": "list"},
    "govern_memory:explain": {"operation": "explain", "audience": "external", "path": SECRET},
    "govern_memory:simulate": {"operation": "simulate", "audience": "external", "paths": [CLAIM, SECRET]},
    "govern_memory:vocabulary-request": {"operation": "vocabulary-request", "vocabulary_request_id": "missing"},
    "govern_memory:vocabulary-status": {"operation": "vocabulary-status"},
    "remember": {
        "validate_only": True, "title": "Lantern Routine", "slug": "lantern-routine",
        "content": "## Observations\n\n- [finding] Check the lantern before dusk.\n",
        "draft_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    },
    "edit_memory": {
        "validate_only": True, "path": CLAIM, "why": "Clarify the lantern observation",
        "operation": {"kind": "replace_string", "old_string": "burns all night", "new_string": "stays lit all night"},
    },
    "replace_memory": {
        "validate_only": True, "old_path": CLAIM, "title": "Lantern Claim Revised",
        "slug": "lantern-claim-revised",
        "content": "## Observations\n\n- [finding] The lantern burns until dawn.\n",
        "draft_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    },
}

# Path-only raw authorizers cannot resolve a tag scope without parsing. These command
# protocol names and invocation keys use a path policy so their successful reads run; both
# policies hide the same two pages. The other samples retain the tag policy's frontmatter-dependent decisions.
PATH_POLICY_SAMPLES = frozenset({
    "compile_source", "propose_compilation", "query_data", "query_dataset",
    "get_video_frames", "read_media", "process_media", "episode_memory",
    "record_memory", "plan_memory",
    "remember", "edit_memory", "replace_memory", "review_memory:compilation",
})

# These protocol errors are legitimate local reads of unavailable media/control-plane state.
# A test process serves no collection store and holds no verified authorization session, so
# store-routed collections and the vocabulary authority answer with their refusal here.
OWNER_REFUSALS = {
    "get_video_frames": "NOT_A_VIDEO:",
    "read_media": "NOT_A_VIDEO:",
    "adoption_studio:status": "RUN_NOT_FOUND:",
    "adoption_studio:work-item": "RUN_NOT_FOUND:",
    "maintain_memory:curation:preview": "CURATION_RUN_NOT_FOUND:",
    "maintain_memory:curation:status": "CURATION_RUN_NOT_FOUND:",
    "maintain_memory:structured-files": "COLLECTION_NOT_FOUND:",
    "record_memory:import:preview": "IMPORT_UNAVAILABLE:",
    "record_memory:import:status": "IMPORT_UNAVAILABLE:",
    "plan_memory:query": "COLLECTION_NOT_FOUND:",
    "plan_memory:history": "COLLECTION_NOT_FOUND:",
    "govern_memory:vocabulary-request": "VOCABULARY_AUTHORITY_UNAVAILABLE:",
    "govern_memory:vocabulary-status": "VOCABULARY_AUTHORITY_UNAVAILABLE:",
    "observe_transfer": "CLOUD_GATEWAY_REQUIRED:",
}

class ReadSurfaceViolation(AssertionError):
    """A recorded store contract failure, distinct from a bad sample or setup."""


def _sub_selector(command_name, selector_value, adapter):
    """A further registry that the classifier reads for one selector value, as (argument path, values).

    `invocation_is_read_only` reads three: governance operations, curation actions and import modes.
    """
    if command_name == "maintain_memory" and selector_value == "curation":
        return ("curation_action",), sorted(curation.READ_ONLY_ACTIONS)
    if adapter == "import-conditional":
        return ("import_request", "mode"), sorted(import_modes.READ_ONLY_MODES)
    return None


def registered_read_invocations():
    """Every read-only invocation that the registry declares, keyed as `invocation_key` names it.

    A command without a selector is one invocation when it reads. A mixed command contributes
    each selector value that is not a mutation, split by the classifier's further registries.
    """
    found = {}
    selectors = egress.selector_registry()
    for command in (*commands.COMMANDS, *commands.PRODUCT_COMMANDS):
        name = command.name
        selector = egress.selector_for_command(name)
        if name == "govern_memory":
            keys = [f"{name}:{value}" for value in operations.OPERATION_SPECS if operations.is_read_only(value)]
        elif selector is None:
            reads = command.read_only or commands.invocation_is_read_only(command, {"validate_only": True})
            keys = [name] if reads else []
        else:
            keys = []
            for value, adapter in selectors[(name, selector)].items():
                further = _sub_selector(name, value, adapter)
                if further is not None:
                    keys.extend(f"{name}:{value}:{sub}" for sub in further[1])
                elif adapter != "mutation":
                    keys.append(f"{name}:{value}")
        found.update(dict.fromkeys(keys, command))
    return found


def invocation_key(command, arguments):
    """The registry name of the invocation that `arguments` select."""
    name = command.name
    if name == "govern_memory":
        return f"{name}:{arguments['operation']}"
    selector = egress.selector_for_command(name)
    if selector is None:
        return name
    value = arguments[selector]
    further = _sub_selector(name, value, egress.selector_registry()[(name, selector)][value])
    if further is None:
        return f"{name}:{value}"
    sub = arguments
    for part in further[0]:
        sub = sub[part]
    return f"{name}:{value}:{sub}"


READ_INVOCATIONS = registered_read_invocations()


def test_every_registered_read_invocation_has_a_read_only_sample():
    """A reader that the registry gains without a sample fails here, before any store comparison."""
    assert set(READ_INVOCATIONS) == set(SAMPLES)
    for key, arguments in SAMPLES.items():
        command = READ_INVOCATIONS[key]
        assert invocation_key(command, arguments) == key
        assert commands.invocation_is_read_only(command, arguments), key


def clear_reader_caches(root):
    find.clear_cache()
    find.evict_resolver_caches(root)
    vaults.clear_inbound_index()
    working_set_runtime.reset_caches_for_tests()


@pytest.fixture
def validation_clock(monkeypatch):
    """Compare identical prospective writes, including their history and transition identity."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 1, 1, tzinfo=UTC)
            return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(temporal, "now", lambda: Clock.now(UTC))
    monkeypatch.setattr(semantic_writes, "dt", SimpleNamespace(**{**vars(dt), "datetime": Clock}))
    # A record query and a plan progress report date their answers.
    for module in (record_formats, plan_progress):
        monkeypatch.setattr(module, "dt", SimpleNamespace(**{**vars(dt), "datetime": Clock}))
    monkeypatch.setattr(semantic_writes, "uuid", SimpleNamespace(
        **{**vars(semantic_writes.uuid), "uuid4": lambda: UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")}
    ))


def answer(command, root, arguments, source_schema, *, store=False):
    injected = (root, source_schema) if command.needs_schema else (root,)
    try:
        value = invoke_command(command, *injected, **arguments)
    # A store refusal carries a stable code, as a protocol error does.
    except (ValueError, OpError, CollectionStoreError) as error:
        return {"refusal": type(error).__name__, "reason": str(error)}
    except Exception as error:
        if not store:
            raise  # A broken file-mode sample cannot establish the comparison.
        return {"exception": type(error).__name__, "reason": str(error)}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


def comparable(value, *, inbound_links=False):
    """Normalize response identity fields, retaining the content of a continuity token.

    The viewer's observed_at dates one observation. recall_context's
    served_context_id, generation.freshness_key and generation.index_generation identify
    backend snapshots; budget.serialized_chars includes their variable-length encoding.
    Only minted_ns, thread, thread_ns and mac vary per call in the decoded continuity.
    """
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result
    result.pop("observed_at", None)
    result.pop("served_context_id", None)
    token = result.get("continuity")
    if token:
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        for field in ("minted_ns", "thread", "thread_ns", "mac"):
            payload.pop(field, None)
        result["continuity"] = payload
    generation = result.get("generation")
    if isinstance(generation, dict):
        generation.pop("freshness_key", None)
        generation.pop("index_generation", None)
    if "budget" in result:
        result["budget"].pop("serialized_chars", None)
    if inbound_links:
        for container in (result, result.get("links")):
            if isinstance(container, dict) and isinstance(container.get("inbound"), list):
                # canonical-vault-store specifies path order for store links and walk order for files.
                container["inbound"] = sorted(
                    container["inbound"], key=lambda row: json.dumps(row, sort_keys=True),
                )
    return result


def test_comparison_ignores_only_inbound_order_and_keeps_duplicate_links():
    """Filesystem walk order must not produce false parity failures or hide a missing link."""
    first = {"path": "Notes/First.md", "line_number": 2}
    second = {"path": "Notes/Second.md", "line_number": 5}
    answer = {"links": {"inbound": [first, second, first]}, "hits": [first, second]}
    reordered = {"links": {"inbound": [second, first, first]}, "hits": [first, second]}
    assert comparable(answer, inbound_links=True) == comparable(reordered, inbound_links=True)
    reordered["links"]["inbound"].pop()
    assert comparable(answer, inbound_links=True) != comparable(reordered, inbound_links=True)
    reordered["links"]["inbound"].append(first)
    reordered["hits"].reverse()
    assert comparable(answer, inbound_links=True) != comparable(reordered, inbound_links=True)


def prepare_sample(root, monkeypatch, request, sample):
    """Write the shared pages and the sample's own state; answer (command, arguments, schema)."""
    name = sample.split(":", 1)[0]
    sample_arguments = SAMPLES[sample]
    selector = (
        'paths: ["Notes/Secret Log.md", "Notes/Hidden/**"]'
        if name in PATH_POLICY_SAMPLES or sample in PATH_POLICY_SAMPLES else 'tags: ["secret"]'
    )
    restrict_external(root, selector)
    dated = sample in ("record_memory:query", "review_memory:plan-progress")
    if sample_arguments.get("validate_only") or name == "observe_memory" or dated:
        request.getfixturevalue("validation_clock")
    if sample == "schema_memory:census":
        from exomem import relation_census
        monkeypatch.setattr(relation_census, "_cohort_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    write_pages(root)
    write_pages(root, UNIT_PAGES)
    write_pages(root, {
        SOURCE: "---\ntitle: Beacon Survey\ntype: source\ncreated: 2025-01-01\n---\n"
        "# Beacon Survey\n\nThe lighthouse lantern uses oil.\n",
    })
    # Give the provenance reader a real observation to lose when a projection disappears.
    with (root / CLAIM).open("a") as page:
        page.write("\nA lantern observation. <!-- survey:beacon -->\n")
    if name == "episode_memory":
        record_episode(root)
    if sample == "maintain_memory:tag-variants":
        write_pages(root, {
            f"Knowledge Base/Notes/Tag-{number}.md":
                f"---\ntitle: Tag {number}\ntags: [{tag}]\n---\n# Tag\n"
            for number, tag in enumerate(("harbors", "harbor", "harbor"))
        })
    build_indexes(root)
    source_schema = schema.load_source_schema(root)
    arguments = copy.deepcopy(sample_arguments)
    if arguments.get("ref") == "$review":
        items = attention.attention(
            root, categories=["unprocessed_source"], limit=0, record_surfacing=False,
        ).items
        arguments["ref"] = next(item.ref for item in items if item.path == SOURCE)
    if arguments.get("ref") == "$advisory":
        # A published advisory result names the claim page and that page's fingerprint.
        receipt, _fingerprint = prepare_advisory_custody(root, batch_id="b-surface", target_rel=CLAIM)
        arguments["ref"] = derived_receipts.advisory_result_ref(root, receipt)
    command = READ_INVOCATIONS[sample]
    assert commands.invocation_is_read_only(command, arguments)
    return command, arguments, source_schema


#: Each registered read that still breaks the store-mode read contract, and why. Its marker is strict,
#: so a fix that makes the read pass fails here until its entry goes.
STORE_READ_GAPS = {
    "maintain_memory:backfill-ids": (
        "the page walk stats projections; listing stored pages instead would release withheld names, "
        "because the terminal egress filter finds vault items on the disk (filter_withheld_entries)"
    ),
    "maintain_memory:fix": "the fix scan walks and reads page projections",
    "maintain_memory:reconcile": (
        "the dry run opens the lexical sidecar, stats projections and raises in store mode"
    ),
}


@pytest.mark.parametrize("sample", [
    pytest.param(key, marks=pytest.mark.xfail(
        strict=True, raises=ReadSurfaceViolation, reason=STORE_READ_GAPS[key]
    )) if key in STORE_READ_GAPS else key
    for key in sorted(READ_INVOCATIONS)
])
def test_each_registered_read_uses_the_store(vault, monkeypatch, record_property, request, sample):  # noqa: F811
    """Lost content, changed refusals, projection I/O, sidecars, extra snapshots and repairs."""
    assert sample in SAMPLES, f"registered read invocation {sample} has no sample"
    name = sample.split(":", 1)[0]
    sample_arguments = SAMPLES[sample]
    command, arguments, source_schema = prepare_sample(vault, monkeypatch, request, sample)
    links = bool(sample_arguments.get("links") or sample in ("list_inbound_links", "connect_memory:inbound-links"))
    callers = {"owner": nullcontext, "external": lambda: request_scope(EXTERNAL)}
    before = {}
    for caller, scope in callers.items():
        clear_reader_caches(vault)
        # Cache eviction also clears the watcher registry. Restore service readiness before
        # comparing bounded graph reads and complete link answers with the store's current rows.
        if sample_arguments.get("links") or name == "viewer_map":
            freshness.seed(vault, "vault", (
                (str(path), freshness.stat_signature(path)) for path in vaults.walk_vault_md(vault)
            ))
            assert epistemic_graph.EpistemicGraphIndex(vault).available()
        if sample_arguments.get("links"):
            assert vaults.prime_inbound_index(vault, eager=True)
        with scope():
            before[caller] = comparable(answer(command, vault, arguments, source_schema), inbound_links=links)
    if "refusal" in before["owner"]:
        assert sample in OWNER_REFUSALS, f"invalid file-mode sample: {before['owner']}"
        assert before["owner"]["reason"].startswith(OWNER_REFUSALS[sample])

    owned = {
        item.rel: (vault / item.rel).read_bytes()
        for item in ownership.iter_owned(vault)
        if item.ownership is ownership.Ownership.PAGE
    }
    assert migrate_apply(vault) == 0
    if name == "coordination_status" or sample == "maintain_memory:collections-store-adopt-local":
        # Migration takes a lease and changes contention counters, and it publishes a new vault
        # replica that adopt-local previews. Observe the same operational state through file
        # mode before comparing backend-specific diagnostics.
        with monkeypatch.context() as file_mode:
            file_mode.setattr(mode, "store_mode_for_reads", lambda _root: False)
            for caller, scope in callers.items():
                with scope():
                    before[caller] = comparable(answer(command, vault, arguments, source_schema), inbound_links=links)
    expected = copy.deepcopy(before)
    if name == "coordination_status":
        # Sidecar synchronization does not exist in store mode; assert the canonical revision instead.
        with read.snapshot(vault) as snapshot:
            revision = snapshot.revision
        for value in expected.values():
            value["graph_sync"] = {"state": "not_applicable", "generation": None}
            value["vault_store"] = {"state": "available", "revision": revision}
    if sample == "schema_memory:census":
        # Canonical-vault-store graph reads identify the store revision, not the sidecar counter.
        with read.snapshot(vault) as snapshot:
            revision = snapshot.revision
        for value in expected.values():
            if value.get("graph_generation") is not None:
                value["graph_generation"] = revision
    if sample == "review_memory:held":
        # Only a store-mode vault keeps a held queue; file mode answers that it has none.
        for value in expected.values():
            value["outcome"] = "Nothing is held: no outside edit or delete waits for a choice."
    if name == "viewer_map":
        with read.snapshot(vault) as snapshot:
            revision = snapshot.revision
        for value in expected.values():
            # The static graph identity survives migration; volatile file witnesses do not.
            value["snapshot"].pop("recall_projection_identity")
            value["snapshot"]["generation"] = str(revision)
    for sidecar in (epistemic_graph.sidecar_path(vault), lexstore.lexical_path(vault)):
        sidecar.unlink(missing_ok=True)
        sidecar.write_bytes(SENTINEL)

    if sample == "maintain_memory:tag-variants":
        # Tags remain canonical store facts when ordinary lexical retrieval uses its Python rung.
        monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    observations = []
    for damage in ("deleted", "poisoned"):
        for relative in owned:
            path = vault / relative
            if damage == "deleted":
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(SENTINEL)
        for caller, scope in callers.items():
            clear_reader_caches(vault)
            # Only migrated page rows count: a prospective destination or schema folder is no stored projection.
            with scope(), record_store_reads(vault, monkeypatch, page_paths=owned) as events:
                actual = comparable(answer(command, vault, arguments, source_schema, store=True), inbound_links=links)
            violations = []
            if actual != expected[caller]:
                violations.append("answer differs")
            if events["page_io"]:
                violations.append("page filesystem read")
            if events["sidecars"]:
                violations.append("sidecar connection")
            if len(events["snapshots"]) != 1:
                violations.append(f"snapshots={len(events['snapshots'])}")
            if set(events["snapshot_threads"]) != set(events["request_threads"]):
                violations.append("off-thread snapshot")
            if events["machinery"]:
                violations.append("file-mode machinery")
            observations.append({
                "caller": caller, "damage": damage, "violations": violations,
                "events": events, "file_answer": before[caller], "expected_answer": expected[caller],
                "store_answer": actual,
            })
    record_property("read_surface", json.dumps(observations, default=str))
    failures = [row for row in observations if row["violations"]]
    if failures:
        raise ReadSurfaceViolation(json.dumps(failures, indent=2, default=str))
