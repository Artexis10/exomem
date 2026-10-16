"""File-mode writers never write a collection the vault's marker routes to the store.

OpenSpec move-structured-collections-to-sqlite, the structured-collections scenario for a
curation Records step on a store collection. The store is served only on the service's
store thread; off it, a routed collection's files are generated views. Each case drives a
migrated vault that the service serves, through the call its owner or agent makes.
"""

from __future__ import annotations

from contextlib import closing

import pytest
from test_collection_store_migration import (
    WORK,
    X3,
    legacy_vault,
    migrate,
    printed,
    rows,
    start_service,
    stop_writer,
)
from test_collection_store_migration import env as env
from test_collection_store_service import OWNER, call
from test_collection_store_writer import CID
from test_governance_egress import EXTERNAL, write_rule, write_scope

from exomem import commands, curation, episode_workflow, records, writer_lease
from exomem import schema as schema_module
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import chain, connection
from exomem.governance.principal import RequestPrincipal, request_scope

X3_LOG = "Knowledge Base/Records/Health/X3/Training Log.md"
EPISODE = "ep-" + "5e" * 16


def migrated(root, capsys):
    """The legacy vault after `exomem collections migrate --apply`, served by the service."""
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    return start_service(root)


def store_head(root):
    """The store's verified chain head: any committed transaction moves it."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return chain.verify_store_chain(reader)


def test_an_off_thread_append_to_a_store_log_collection_refuses_and_writes_nothing(env, capsys):
    """Defect: a Records append that runs off the store thread writes a store collection's
    log view while the store stays unchanged, so the store's next projection drops it."""
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        before = rows(root, X3), store_head(root), (root / X3_LOG).read_bytes()
        with pytest.raises(collections.CollectionError) as refused:
            records.append_record(root, X3, item={"occurred_on": "2024-12-01", "title": "Pull",
                                                  "status": "completed"},
                                  why="an append that reaches no store thread")
        assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
        assert (rows(root, X3), store_head(root), (root / X3_LOG).read_bytes()) == before
    finally:
        service.stop()


def test_a_withheld_caller_hears_a_store_collection_is_absent_and_a_released_one_that_it_is_unavailable(
        env, capsys):
    """Defect: the store refusal tells a caller whom the collection is withheld from that it
    exists, or tells a caller it is released to that it does not exist."""
    root = legacy_vault(env)
    write_scope(root, paths="Records/Work/**")
    write_rule(root, ceiling=0)  # withholds Work from EXTERNAL only
    service = migrated(root, capsys)
    try:
        answers = {}
        for audience in (EXTERNAL, "partner"):
            with request_scope(RequestPrincipal(audience_id=audience, surface="mcp")):
                with pytest.raises(collections.CollectionError) as refused:
                    records.append_record(root, WORK, item={"title": "Off-thread"},
                                          why="an append that reaches no store thread")
            answers[audience] = refused.value.code
        assert answers == {EXTERNAL: "COLLECTION_NOT_FOUND", "partner": "COLLECTION_STORE_UNAVAILABLE"}
    finally:
        service.stop()


def episode(root, **arguments):
    """`episode_memory` through the dispatcher every surface shares, as the owner's agent."""
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "episode_memory")
    with request_scope(OWNER):
        return writer_lease.invoke_command(
            command, root, schema_module.load_source_schema(root), episode=EPISODE, **arguments)


def record_an_episode(root):
    return episode(root, action="record", subject="Work log", summary="Logged one work item.",
                   worked_on=["Logged a work item"], decided=["Keep it in Work"])


def leaf_into_work(container_hash):
    return {"route": "records", "alternatives": [], "evidence": "complete", "title": "Work item",
            "reason": "A Records event the episode observed.",
            "leaves": [{"leaf_key": "append", "effect_revision": 1, "kind": "append-record",
                        "args": {"collection": WORK, "item": {"title": "From an episode"},
                                 "why": "The episode logged a work item.",
                                 "expected_container_hash": container_hash}}]}


def sealed_plans(root):
    runs = root / "Knowledge Base/_Governance/curation/runs"
    return sorted(path.relative_to(runs).as_posix() for path in runs.rglob("plan.json")) if runs.is_dir() else []


def test_an_episode_records_leaf_into_a_store_collection_refuses_before_any_seal(env, capsys, monkeypatch):
    """Defect: preparing a Records leaf for a migrated collection refuses as if the collection
    did not exist, which sends the agent looking for a collection that is there."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        container = call(root, "record_memory", action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
        with pytest.raises((OpError, ValueError)) as refused:
            episode(root, action="prepare", candidate="work", proposal=leaf_into_work(container))
        assert getattr(refused.value, "code", None) == "CURATION_RECORDS_STORE_UNSUPPORTED", refused.value
        assert sealed_plans(root) == []
    finally:
        service.stop()


def test_a_records_leaf_sealed_before_the_migration_refuses_before_approval(env, capsys, monkeypatch):
    """Defect: a Records leaf sealed while its collection was a file collection is approved
    after the migration, then refused by its writer, and the dispatcher reports the refused
    apply as committed; or the episode calls the leaf stale, so its agent seals it again."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    prepared = episode(root, action="prepare", candidate="work", proposal=leaf_into_work(container))
    episode(root, action="disposition", candidate="work", disposition="routed", reason="Observed.")
    run = prepared["candidates"][0]["leaves"][0]["run_id"]
    plan_id, fingerprint = curation.CurationStore(root).identities(run)
    stop_writer()
    service = migrated(root, capsys)
    try:
        before = store_head(root), rows(root, CID)
        command = next(command for command in commands.product_commands_for("mcp") if command.name == "maintain_memory")
        answers = []
        for _attempt in range(2):
            with request_scope(OWNER), pytest.raises((OpError, ValueError)) as refused:
                writer_lease.invoke_command(
                    command, root, mode="curation", curation_action="apply", run_id=run, plan_id=plan_id,
                    expected_plan_fingerprint=fingerprint, why="Approve the sealed work item.",
                    idempotency_key="apply-the-sealed-work-item")
            details = getattr(refused.value, "details", None) or {}
            answers.append((getattr(refused.value, "code", None), details.get("committed")))
        assert answers == [("CURATION_RECORDS_STORE_UNSUPPORTED", None)] * 2
        # The dispatcher's terminal keeps only the status; the episode command reports each stale leaf.
        with request_scope(OWNER):
            schema = schema_module.load_source_schema(root)
            reviewed = commands.op_episode_memory(root, schema, action="candidates", episode=EPISODE)
            resumed = commands.op_episode_memory(
                root, schema, action="resume", episode=EPISODE, input_revision=reviewed["input_revision"],
                journal_digest=reviewed["journal_digest"])
        assert [item["code"] for item in resumed["stale"]] == ["CURATION_RECORDS_STORE_UNSUPPORTED"]
        assert not curation.CurationStore(root).approval_path(run).exists()
        assert (store_head(root), rows(root, CID)) == before
    finally:
        service.stop()


def test_a_run_approved_before_the_migration_refuses_its_resume_and_stays_resumable(env, capsys, monkeypatch):
    """Defect: the resume of a Records run approved before the migration records the store
    refusal as the run's final failure, so the run can never commit once the store runs it."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    prepared = episode(root, action="prepare", candidate="work", proposal=leaf_into_work(container))
    run = prepared["candidates"][0]["leaves"][0]["run_id"]
    plan_id, fingerprint = curation.CurationStore(root).identities(run)

    def stopped(name):
        if name == "after-prepared-state":
            raise curation.CurationFault(name)

    # The owner approves the run, and the process stops before its leaf runs.
    with monkeypatch.context() as crash, request_scope(OWNER):
        crash.setattr(curation, "_fault_barrier", stopped)
        with pytest.raises(curation.CurationFault):
            curation.apply(root, run_id=run, plan_id=plan_id, expected_plan_fingerprint=fingerprint,
                           why="Approve the sealed work item.")
    stop_writer()
    service = migrated(root, capsys)
    try:
        command = next(command for command in commands.product_commands_for("mcp") if command.name == "maintain_memory")
        answers = []
        for _attempt in range(2):
            with request_scope(OWNER), pytest.raises((OpError, ValueError)) as refused:
                writer_lease.invoke_command(command, root, mode="curation", curation_action="resume", run_id=run,
                                            plan_id=plan_id, idempotency_key="resume-the-approved-work-item")
            details = getattr(refused.value, "details", None) or {}
            answers.append((getattr(refused.value, "code", None), details.get("committed")))
        assert answers == [("CURATION_RECORDS_STORE_UNSUPPORTED", None)] * 2
        status = curation.status(root, run_id=run)
        assert (status["receipts"], status["failed_step"], status["next_action"]) == ([], None, "resume")
    finally:
        service.stop()
