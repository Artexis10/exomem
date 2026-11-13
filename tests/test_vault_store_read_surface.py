"""The registered read surface must answer from one store snapshot, without projections.

OpenSpec make-vault-store-canonical 3.1/3.2. Unlike a sampled graph check, the coverage
check fails when the command registry gains a reader without a representative invocation.
Mixed commands enter through their registered read selectors and the dispatcher's classifier.
"""

from __future__ import annotations

import base64
import copy
import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
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
    epistemic_graph,
    find,
    freshness,
    lexstore,
    schema,
    temporal,
    working_set_runtime,
)
from exomem import vault as vaults
from exomem.cli_ops import OpError
from exomem.governance import egress, operations
from exomem.governance.principal import request_scope
from exomem.vault_store import ownership
from exomem.writer_lease import invoke_command

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(900),
]

SOURCE = "Knowledge Base/Sources/Beacon Survey.md"
DATA = "Knowledge Base/Records/dataset/readings.csv"

# These keys are command protocol names. Each sample reaches the named reader, rather than
# deliberately failing input validation. Media and operational status may legitimately refuse.
SAMPLES = {
    "served_context": {},
    "configure_memory": {"action": "inspect"},
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
    "observe_memory": {
        "operation": "validate", "path": LAMP, "category": "rule",
        "content": "Keep the lighthouse lantern lit.",
    },
    "query_data": {"path": DATA},
    "list_directory": {"path": "Knowledge Base/Notes", "recursive": True},
    "list_trash": {},
    "list_inbound_links": {"target": CLAIM},
    "schema_memory": {"operation": "infer", "subject": "categories"},
    "record_memory": {"action": "describe"},
    "plan_memory": {"action": "inspect"},
    "get_video_frames": {"path": "Knowledge Base/Notes/diagram.bin"},
    # This is the whole recall_context operation, through its registered execution name.
    "activate_context": {"turn": "What do we know about the lighthouse lantern?", "max_chars": 8000},
    "episode_memory": {"action": "inspect", "episode": EPISODE},
    "ask_memory": {"query": "lighthouse", "rerank": False},
    "read_memory": {"path": CLAIM, "links": True},
    "browse_memory": {"path": "Knowledge Base/Notes", "mode": "list", "limit": 5},
    "compile_source": {"sources": [SOURCE]},
    "process_media": {"operation": "status", "path": "Knowledge Base/Notes/diagram.bin"},
    "review_memory": {"mode": "attention"},
    "connect_memory": {"operation": "context", "path": LAMP, "depth": 2},
    "adopt_vault": {"path": "Knowledge Base/Notes"},
    "adoption_studio": {"action": "status", "run_id": "missing"},
    "maintain_memory": {"mode": "audit"},
    "manage_memory_file": {"operation": "list", "path": "Knowledge Base/Notes"},
    "query_dataset": {"path": DATA},
    "read_media": {"path": "Knowledge Base/Notes/diagram.bin"},
    "observe_transfer": {},
    "govern_memory": {"operation": "list"},
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
# protocol names use a path policy so their successful reads run; both policies hide the
# same two pages. The other samples retain the tag policy's frontmatter-dependent decisions.
PATH_POLICY_SAMPLES = frozenset({
    "compile_source", "propose_compilation", "query_data", "query_dataset",
    "get_video_frames", "read_media", "process_media", "episode_memory",
    "record_memory", "plan_memory",
    "remember", "edit_memory", "replace_memory",
})

# These protocol errors are legitimate local reads of unavailable media/control-plane state.
OWNER_REFUSALS = {
    "get_video_frames": "NOT_A_VIDEO:",
    "read_media": "NOT_A_VIDEO:",
    "adoption_studio": "RUN_NOT_FOUND:",
    "observe_transfer": "CLOUD_GATEWAY_REQUIRED:",
}

# These are the remaining observed command cases after the shared read fixes, not a blanket xfail.
# A new command is unmarked; an invalid sample raises an ordinary assertion, not this failure.
KNOWN_FAILURES = {
    "coordination_status": "Status still probes graph-sidecar ownership",
    "edit_memory": "Validation mints a fresh transition token and hashes even in unchanged file mode",
    "observe_memory": "Validation mints a fresh transition token and hashes even in unchanged file mode",
    "replace_memory": "Replacement validation's navigation plan still scans projected indexes",
    "viewer_map": "Payloads match; store revision identity intentionally differs from file generation metadata",
}


class ReadSurfaceViolation(AssertionError):
    """A recorded store contract failure, distinct from a bad sample or setup."""


def registered_read_commands():
    """Use the real registry and classifier, including mixed commands' declared selectors."""
    found = {}
    selectors = egress.selector_registry()
    for command in (*commands.COMMANDS, *commands.PRODUCT_COMMANDS):
        probes = [{}, {"validate_only": True}]
        probes.extend(
            {selector: value}
            for (name, selector), values in selectors.items()
            if name == command.name
            for value in values
        )
        if command.name == "govern_memory":
            probes.extend({"operation": value} for value in operations.OPERATION_SPECS)
        for arguments in probes:
            try:
                read_only = commands.invocation_is_read_only(command, arguments)
            except egress.SelectorCoverageError:
                continue  # A required selector has no default.
            if read_only:
                found[command.name] = command
                break
    return found


READ_COMMANDS = registered_read_commands()


def test_every_registered_reader_has_a_read_only_sample():
    assert set(READ_COMMANDS) == set(SAMPLES)
    for name, arguments in SAMPLES.items():
        assert commands.invocation_is_read_only(READ_COMMANDS[name], arguments), name


def clear_reader_caches(root):
    find.clear_cache()
    find.evict_resolver_caches(root)
    vaults.clear_inbound_index()
    working_set_runtime.reset_caches_for_tests()


def answer(command, root, arguments, source_schema, *, store=False):
    injected = (root, source_schema) if command.needs_schema else (root,)
    try:
        value = invoke_command(command, *injected, **arguments)
    except (ValueError, OpError) as error:
        return {"refusal": type(error).__name__, "reason": str(error)}
    except Exception as error:
        if not store:
            raise  # A broken file-mode sample cannot establish the comparison.
        return {"exception": type(error).__name__, "reason": str(error)}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


def comparable(value):
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
    return result


@pytest.mark.parametrize("name", [
    pytest.param(name, marks=(
        pytest.mark.xfail(strict=True, raises=ReadSurfaceViolation, reason=KNOWN_FAILURES[name])
        if name in KNOWN_FAILURES else ()
    ))
    for name in sorted(READ_COMMANDS)
])
def test_each_registered_read_uses_the_store(vault, monkeypatch, record_property, name):  # noqa: F811
    """Lost content, changed refusals, projection I/O, sidecars, extra snapshots and repairs."""
    assert name in SAMPLES, f"registered reader {name} has no sample"
    selector = (
        'paths: ["Notes/Secret Log.md", "Notes/Hidden/**"]'
        if name in PATH_POLICY_SAMPLES else 'tags: ["secret"]'
    )
    restrict_external(vault, selector)
    if SAMPLES[name].get("validate_only"):
        # Validation renders authored timestamps; a fixed clock compares the same prospective bytes.
        monkeypatch.setattr(temporal, "now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    write_pages(vault)
    write_pages(vault, UNIT_PAGES)
    write_pages(vault, {
        SOURCE: "---\ntitle: Beacon Survey\ntype: source\ncreated: 2025-01-01\n---\n"
        "# Beacon Survey\n\nThe lighthouse lantern uses oil.\n",
    })
    # Give the provenance reader a real observation to lose when a projection disappears.
    with (vault / CLAIM).open("a") as page:
        page.write("\nA lantern observation. <!-- survey:beacon -->\n")
    if name == "episode_memory":
        record_episode(vault)
    build_indexes(vault)
    source_schema = schema.load_source_schema(vault)
    arguments = copy.deepcopy(SAMPLES[name])
    if arguments.get("ref") == "$review":
        items = attention.attention(
            vault, categories=["unprocessed_source"], limit=0, record_surfacing=False,
        ).items
        arguments["ref"] = next(item.ref for item in items if item.path == SOURCE)
    command = READ_COMMANDS[name]
    assert commands.invocation_is_read_only(command, arguments)
    callers = {"owner": nullcontext, "external": lambda: request_scope(EXTERNAL)}
    before = {}
    for caller, scope in callers.items():
        clear_reader_caches(vault)
        # Cache eviction also clears the watcher registry. Restore service readiness before
        # comparing bounded graph reads and complete link answers with the store's current rows.
        if SAMPLES[name].get("links") or name == "viewer_map":
            freshness.seed(vault, "vault", (
                (str(path), freshness.stat_signature(path)) for path in vaults.walk_vault_md(vault)
            ))
            assert epistemic_graph.EpistemicGraphIndex(vault).available()
        if SAMPLES[name].get("links"):
            assert vaults.prime_inbound_index(vault, eager=True)
        with scope():
            before[caller] = comparable(answer(command, vault, arguments, source_schema))
    if "refusal" in before["owner"]:
        assert name in OWNER_REFUSALS, f"invalid file-mode sample: {before['owner']}"
        assert before["owner"]["reason"].startswith(OWNER_REFUSALS[name])

    owned = {
        item.rel: (vault / item.rel).read_bytes()
        for item in ownership.iter_owned(vault)
        if item.ownership is ownership.Ownership.PAGE
    }
    assert migrate_apply(vault) == 0
    for sidecar in (epistemic_graph.sidecar_path(vault), lexstore.lexical_path(vault)):
        sidecar.unlink(missing_ok=True)
        sidecar.write_bytes(SENTINEL)

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
                actual = comparable(answer(command, vault, arguments, source_schema, store=True))
            violations = []
            if actual != before[caller]:
                violations.append("answer differs")
            if events["page_io"]:
                violations.append("page filesystem read")
            if events["sidecars"]:
                violations.append("sidecar connection")
            if len(events["snapshots"]) != 1:
                violations.append(f"snapshots={len(events['snapshots'])}")
            if events["machinery"]:
                violations.append("file-mode machinery")
            observations.append({
                "caller": caller, "damage": damage, "violations": violations,
                "events": events, "file_answer": before[caller], "store_answer": actual,
            })
    record_property("read_surface", json.dumps(observations, default=str))
    failures = [row for row in observations if row["violations"]]
    if failures:
        raise ReadSurfaceViolation(json.dumps(failures, indent=2, default=str))
