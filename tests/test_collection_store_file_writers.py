"""A Records step on a collection the vault's marker routes to the store.

OpenSpec move-structured-collections-to-sqlite, the structured-collections scenarios for a
curation Records step on a store collection. The store is served only on the service's
store thread; off it, a routed collection's files are generated views, so a file-mode writer
refuses and a curation Records step runs on the store thread. Each case drives a migrated
vault that the service serves, through the call its owner or agent makes.
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
from test_collection_store_writer import CID, KEY
from test_governance_egress import EXTERNAL, write_rule, write_scope

from exomem import commands, curation, episode_workflow, records, writer_lease
from exomem import schema as schema_module
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import chain, connection, runtime
from exomem.collection_store.preview import bound_writer
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


def maintain_as(principal, root, **arguments):
    """`maintain_memory` curation through the same dispatcher."""
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "maintain_memory")
    with request_scope(principal):
        return writer_lease.invoke_command(command, root, mode="curation", **arguments)


def maintain(root, **arguments):
    return maintain_as(OWNER, root, **arguments)


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


def leaf_correcting_work(container_hash, item_version):
    return {"route": "records", "alternatives": [], "evidence": "complete", "title": "Work correction",
            "reason": "A Records correction the episode observed.",
            "leaves": [{"leaf_key": "correct", "effect_revision": 1, "kind": "update-record",
                        "args": {"collection": WORK, "item_key": KEY, "changes": {"title": "Corrected"},
                                 "why": "The episode corrected a work item.",
                                 "expected_container_hash": container_hash,
                                 "expected_item_version": item_version}}]}


def container_of(root):
    return call(root, "record_memory", action="inspect", collection=WORK)["lifecycle_guards"][
        "expected_container_hash"]


def prepare_and_route(root, proposal):
    """Seal a leaf through `episode prepare`, route its candidate, and return its run and identities."""
    prepared = episode(root, action="prepare", candidate="work", proposal=proposal)
    episode(root, action="disposition", candidate="work", disposition="routed", reason="Observed.")
    leaf = prepared["candidates"][0]["leaves"][0]
    plan_id, fingerprint = curation.CurationStore(root).identities(leaf["run_id"])
    return leaf["run_id"], leaf["operation_id"], plan_id, fingerprint


def apply_run(root, run, plan_id, fingerprint, key="apply-the-sealed-work-item"):
    return maintain(root, curation_action="apply", run_id=run, plan_id=plan_id, expected_plan_fingerprint=fingerprint,
                    why="Approve the sealed work item.", idempotency_key=key)


def resume_run(root, run, plan_id, key="resume-the-approved-work-item"):
    return maintain(root, curation_action="resume", run_id=run, plan_id=plan_id, idempotency_key=key)


def transactions(root, run, operation):
    """The store's transaction rows that one run's step committed."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT operation, collection_id FROM txns WHERE request_id = ?",
                              (curation.store_request_id(run, operation),)).fetchall()


def titles_in_work(root):
    return sorted(row["title"] for row in rows(root, CID).values())


def receipts(root, run):
    return [(item["outcome"], item["step_id"]) for item in maintain(root, curation_action="status", run_id=run)["receipts"]]


def sealed_plans(root):
    runs = root / "Knowledge Base/_Governance/curation/runs"
    return sorted(path.relative_to(runs).as_posix() for path in runs.rglob("plan.json")) if runs.is_dir() else []


def test_a_fresh_episode_records_leaf_commits_once_through_the_store(env, capsys, monkeypatch):
    """Defect: a Records leaf sealed against a store collection refuses, or writes the generated
    view file while the store stays unchanged, or commits twice when the run is resumed again."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        run, operation, plan_id, _fingerprint = prepare_and_route(root, leaf_into_work(container_of(root)))
        reviewed = episode(root, action="candidates")
        before = store_head(root)
        resumed = episode(root, action="resume", input_revision=reviewed["input_revision"],
                          journal_digest=reviewed["journal_digest"])
        assert [item["outcome"] for item in resumed["executed"]] == ["committed"], resumed
        assert "From an episode" in titles_in_work(root)
        assert store_head(root) != before
        assert transactions(root, run, operation) == [("append", CID)]
        assert receipts(root, run) == [("committed", "leaf")]
        # The agent's final pass reads the leaf back from the store's row and attests it.
        assert [item["readback"] for item in episode(root, action="coverage")["receipts"]] == ["verified"]
        reviewed = episode(root, action="candidates")
        attested = episode(root, action="resume", input_revision=reviewed["input_revision"],
                           journal_digest=reviewed["journal_digest"], postcommit=True)
        assert attested["status"] == "ok", attested
        after = store_head(root)
        assert resume_run(root, run, plan_id)["outcome"] == "replayed"
        assert (store_head(root), transactions(root, run, operation)) == (after, [("append", CID)])
    finally:
        service.stop()


def test_a_records_leaf_sealed_before_the_migration_commits_once_through_the_store(env, capsys, monkeypatch):
    """Defect: a Records leaf sealed against a file collection is refused once the collection
    migrates, or applies blind to the store's guards instead of proving its seal maps to them."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    run, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container))
    stop_writer()
    service = migrated(root, capsys)
    try:
        before = store_head(root)
        applied = apply_run(root, run, plan_id, fingerprint)
        assert applied["phase"] == "completed", applied
        assert "From an episode" in titles_in_work(root)
        assert transactions(root, run, operation) == [("append", CID)]
        assert store_head(root) != before
        # A second apply under a new request identity finds the seal spent and writes nothing.
        head = store_head(root)
        with pytest.raises((OpError, ValueError)):
            apply_run(root, run, plan_id, fingerprint, key="apply-it-again")
        assert (store_head(root), transactions(root, run, operation)) == (head, [("append", CID)])
    finally:
        service.stop()


def test_a_pre_migration_seal_refuses_when_the_store_has_moved_since_and_writes_nothing(env, capsys, monkeypatch):
    """Defect: a seal made against the files applies after the store took another write, so the
    leaf lands on a collection state its reviewer never saw."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    run, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container))
    stop_writer()
    service = migrated(root, capsys)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "Written in between"},
             why="A write the seal never saw.", expected_container_hash=container_of(root))
        before = store_head(root)
        with pytest.raises((OpError, ValueError)) as refused:
            apply_run(root, run, plan_id, fingerprint)
        assert getattr(refused.value, "code", None) == "CURATION_BINDING_STALE", refused.value
        assert transactions(root, run, operation) == []
        assert store_head(root) == before
        assert not curation.CurationStore(root).approval_path(run).exists()
    finally:
        service.stop()


def test_a_run_approved_before_the_migration_resumes_and_commits_once(env, capsys, monkeypatch):
    """Defect: the resume of a Records run approved before the migration refuses for good, or
    records the refusal as the run's final failure, so the run never commits through the store."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    prepared = episode(root, action="prepare", candidate="work", proposal=leaf_into_work(container))
    leaf = prepared["candidates"][0]["leaves"][0]
    run, operation = leaf["run_id"], leaf["operation_id"]
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
    migrated(root, capsys).stop()
    # With no service the dispatcher refuses the resume and records no receipt, so the run stays resumable.
    assert refusal(lambda: resume_run(root, run, plan_id))[0] == "CURATION_RECORDS_STORE_UNSUPPORTED"
    assert (receipts(root, run), transactions(root, run, operation)) == ([], [])
    service = start_service(root)
    try:
        resumed = resume_run(root, run, plan_id)
        assert resumed["phase"] == "completed", resumed
        assert transactions(root, run, operation) == [("append", CID)]
        assert "From an episode" in titles_in_work(root)
        head = store_head(root)
        assert resume_run(root, run, plan_id, key="resume-it-again")["outcome"] == "replayed"
        assert store_head(root) == head
    finally:
        service.stop()


class ProcessCut(BaseException):
    """A process dying: unlike an Exception, no handler in the step repairs or records it."""


@pytest.mark.parametrize("cut", ["before the evidence file", "after the evidence file"])
def test_a_crash_after_the_store_commit_reconciles_from_the_transaction_row(env, capsys, monkeypatch, cut):
    """Defect: a step the store committed before the process died is retried and writes twice,
    or is failed for lack of a receipt, because nothing but the transaction row remembers it."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        run, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container_of(root)))

        def die(*_arguments, **_keywords):
            raise ProcessCut

        with monkeypatch.context() as crash:
            if cut == "before the evidence file":
                crash.setattr(curation.CurationStore, "create_evidence", die)
            else:
                crash.setattr(curation, "_fault_barrier", lambda name: die() if name == "after-leaf-witness" else None)
            with pytest.raises(ProcessCut):
                apply_run(root, run, plan_id, fingerprint)
        head = store_head(root)
        assert transactions(root, run, operation) == [("append", CID)]
        assert curation.CurationStore(root).reconstruct(run)["receipts"] == []
        reconciled = resume_run(root, run, plan_id)
        assert reconciled["phase"] == "completed", reconciled
        assert receipts(root, run) == [("recovered-committed", "leaf")]
        assert (store_head(root), transactions(root, run, operation)) == (head, [("append", CID)])
        assert resume_run(root, run, plan_id, key="resume-it-again")["outcome"] == "replayed"
    finally:
        service.stop()


def test_an_update_record_leaf_applies_a_correction_through_the_store(env, capsys, monkeypatch):
    """Defect: a Records correction leaf on a store collection cannot be sealed or applied, so the
    agent that observed the correction has no way to make it."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        version = next(row["item_version"] for row in call(root, "record_memory", action="query", collection=WORK)[
            "rows"] if row["record_id"] == KEY)
        run, operation, plan_id, fingerprint = prepare_and_route(
            root, leaf_correcting_work(container_of(root), version))
        assert apply_run(root, run, plan_id, fingerprint)["phase"] == "completed"
        assert rows(root, CID)[KEY]["title"] == "Corrected"
        assert transactions(root, run, operation) == [("update", CID)]
    finally:
        service.stop()


def test_a_curation_with_no_service_refuses_a_store_records_step_and_writes_nothing(env, capsys, monkeypatch):
    """Defect: a request that reaches a store collection's Records step with no service serving the
    vault writes the generated view files, or records an approval for a run that cannot run."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        run, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container_of(root)))
        before = store_head(root)
    finally:
        service.stop()
    refused = refusal(lambda: apply_run(root, run, plan_id, fingerprint))
    assert refused[0] == "CURATION_RECORDS_STORE_UNSUPPORTED" and "record_memory append" in refused[1], refused
    assert (store_head(root), transactions(root, run, operation)) == (before, [])
    assert not curation.CurationStore(root).approval_path(run).exists()


def test_a_records_leaf_into_a_log_collection_refuses_before_any_seal(env, capsys, monkeypatch):
    """Defect: a leaf into a store collection that keeps one log file seals a witness no single
    item path can name, so its run could never be verified."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        container = call(root, "record_memory", action="inspect", collection=X3)["lifecycle_guards"][
            "expected_container_hash"]
        leaf = {"leaf_key": "append", "effect_revision": 1, "kind": "append-record",
                "args": {"collection": X3, "why": "The episode logged a pull.", "expected_container_hash": container,
                         "item": {"occurred_on": "2024-12-01", "title": "Pull", "status": "completed"}}}
        proposal = {**leaf_into_work(container), "leaves": [leaf]}
        with pytest.raises((OpError, ValueError)) as refused:
            episode(root, action="prepare", candidate="work", proposal=proposal)
        assert getattr(refused.value, "code", None) == "CURATION_RECORDS_STORAGE_UNSUPPORTED", refused.value
        assert sealed_plans(root) == []
    finally:
        service.stop()


def test_a_curation_with_no_store_records_step_stays_on_its_file_path(env, capsys, monkeypatch):
    """Defect: every curation in a migrated vault moves to the store thread, so a page plan waits
    behind store work and runs under a bound store writer it never needed."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        page = {"version": 1, "title": "A plain page", "steps": [{
            "step_id": "page", "kind": "create-note",
            "args": {"title": "Plain page", "slug": "plain-page",
                     "content": "## Observations\n\n- [finding] A plain page. ^plain\n"}}]}
        proposed = maintain(root, curation_action="propose", plan=page)
        record_an_episode(root)
        records_run = prepare_and_route(root, leaf_into_work(container_of(root)))[0]

        def route(run):
            return runtime.route("maintain_memory", root, {"mode": "curation", "curation_action": "apply", "run_id": run})

        assert route(proposed["run_id"]) is None
        assert route(records_run) is not None
        applied = apply_run(root, proposed["run_id"], proposed["plan_id"], proposed["plan_fingerprint"])
        assert applied["phase"] == "completed", applied
    finally:
        service.stop()


def test_a_withheld_caller_hears_a_store_records_leaf_collection_is_absent(env, capsys, monkeypatch):
    """Defect: preparing a Records leaf into a store collection withheld from the caller answers
    differently from a collection that does not exist, so it confirms the withheld one is there."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    write_scope(root, paths="Records/Work/**")
    write_rule(root, ceiling=0)  # withholds Work from EXTERNAL only
    service = migrated(root, capsys)
    try:
        command = next(command for command in commands.product_commands_for("mcp") if command.name == "episode_memory")
        answers = {}
        with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
            schema = schema_module.load_source_schema(root)
            writer_lease.invoke_command(command, root, schema, episode=EPISODE, action="record", subject="Work log",
                                        summary="Logged one work item.", worked_on=["Logged a work item"])
            for name, target in {"withheld": WORK, "missing": "Knowledge Base/Records/Nowhere/_collection.md"}.items():
                proposal = leaf_into_work("0" * 64)
                proposal["leaves"][0]["args"]["collection"] = target
                with pytest.raises((OpError, ValueError)) as refused:
                    writer_lease.invoke_command(command, root, schema, episode=EPISODE, action="prepare",
                                                candidate="work", proposal=proposal)
                answers[name] = (getattr(refused.value, "code", None), str(refused.value))
        assert answers["withheld"] == answers["missing"], answers
        assert answers["withheld"][0] == "COLLECTION_NOT_FOUND", answers
        assert sealed_plans(root) == []
    finally:
        service.stop()


def episode_as(principal, root, **arguments):
    """`episode_memory` through the dispatcher as a caller other than the owner."""
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "episode_memory")
    with request_scope(principal):
        return writer_lease.invoke_command(
            command, root, schema_module.load_source_schema(root), episode=EPISODE, **arguments)


EXTERNAL_CALLER = RequestPrincipal(audience_id=EXTERNAL, surface="mcp")
ORIGINAL_ITEM = "Records/Work/Items/Original.md"


def refusal(work):
    """The code and text a refused call answers with."""
    with pytest.raises((OpError, ValueError)) as refused:
        work()
    return getattr(refused.value, "code", None), str(refused.value)


def publish_pending(service, root):
    """The pass the serving store thread makes over views that await publication."""
    def reconcile():
        with request_scope(OWNER), service.manager.mutation_guard(root, operation="publish-views"):
            bound_writer(root).reconcile_views(pending_only=True)

    runtime._SERVERS[root.resolve()].call(reconcile)


def view_state(root, view):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        found = reader.execute("SELECT state FROM projection_state WHERE path = ?", (view,)).fetchone()
    return None if found is None else found[0]


def process_cut_at(name):
    """A fault barrier that kills the process at the named point of a curation step."""
    def barrier(reached):
        if reached == name:
            raise ProcessCut
    return barrier


def withhold_from_external(root, paths):
    """Withhold the named knowledge-base paths from EXTERNAL only."""
    write_scope(root, paths=paths)
    write_rule(root, ceiling=0)


def test_a_curation_leaf_into_a_store_collection_with_no_service_refuses_by_who_may_know_it(env, capsys, monkeypatch):
    """Defect: with no service serving the vault, the route answers UNAVAILABLE before any visibility
    check, so a caller from whom the collection is withheld hears that it exists, and the owner loses
    the refusal that names the Records action that makes the change."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    write_scope(root, paths="Records/Work/**")
    write_rule(root, ceiling=0)  # withholds Work from EXTERNAL only
    service = migrated(root, capsys)
    try:
        container = container_of(root)
    finally:
        service.stop()
    record_an_episode(root)
    episode_as(EXTERNAL_CALLER, root, action="record", subject="Work log", summary="Logged one work item.",
               worked_on=["Logged a work item"])
    missing = leaf_into_work(container)
    missing["leaves"][0]["args"]["collection"] = "Knowledge Base/Records/Nowhere/_collection.md"
    prepare = {"action": "prepare", "candidate": "work"}
    withheld = refusal(lambda: episode_as(EXTERNAL_CALLER, root, **prepare, proposal=leaf_into_work(container)))
    absent = refusal(lambda: episode_as(EXTERNAL_CALLER, root, **prepare, proposal=missing))
    assert withheld == absent and withheld[0] == "COLLECTION_NOT_FOUND", (withheld, absent)
    owner = refusal(lambda: episode_as(OWNER, root, **prepare, proposal=leaf_into_work(container)))
    assert owner[0] == "CURATION_RECORDS_STORE_UNSUPPORTED" and "record_memory append" in owner[1], owner
    assert sealed_plans(root) == []


def test_a_pre_migration_seal_refuses_when_a_published_view_was_deleted_by_hand(env, capsys, monkeypatch):
    """Defect: the mapping checks that each file view is published but not that each published view is
    a file, so a view deleted by hand hides a row the store holds and the apply commits over a state the
    approver never saw."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    with request_scope(OWNER):
        container = commands.op_record_memory(root, action="inspect", collection=WORK)["lifecycle_guards"][
            "expected_container_hash"]
    record_an_episode(root)
    run, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container))
    stop_writer()
    service = migrated(root, capsys)
    try:
        written = call(root, "record_memory", action="append", collection=WORK, item={"title": "Written after"},
                       why="A row the seal never saw.", expected_container_hash=container_of(root))
        view = written["paths"][0]
        publish_pending(service, root)
        # Installation finishes after commit; the next pass acknowledges its hash.
        publish_pending(service, root)
        assert view_state(root, view) == "current" and (root / view).is_file()
        (root / view).unlink()
        before = store_head(root)
        assert refusal(lambda: apply_run(root, run, plan_id, fingerprint))[0] == "CURATION_BINDING_STALE"
        assert (store_head(root), transactions(root, run, operation)) == (before, [])
    finally:
        service.stop()


def commit_one_leaf_as_external(root):
    """An episode of EXTERNAL's own, with a Records leaf into Work that EXTERNAL committed while it could read all of Work."""
    episode_as(EXTERNAL_CALLER, root, action="record", subject="Work log", summary="Logged one work item.",
               worked_on=["Logged a work item"])
    prepared = episode_as(EXTERNAL_CALLER, root, action="prepare", candidate="work",
                          proposal=leaf_into_work(container_of(root)))
    episode_as(EXTERNAL_CALLER, root, action="disposition", candidate="work", disposition="routed",
               reason="Observed.")
    return prepared["candidates"][0]["leaves"][0]["run_id"]


def resume_as_external(root):
    reviewed = episode_as(EXTERNAL_CALLER, root, action="candidates")
    return episode_as(EXTERNAL_CALLER, root, action="resume", input_revision=reviewed["input_revision"],
                      journal_digest=reviewed["journal_digest"])


def test_a_collection_partly_withheld_after_preparation_refuses_the_leaf_before_any_attempt(env, capsys, monkeypatch):
    """Defect: a caller who may read part of the collection passes the pre-attempt check, the writer
    then refuses the write, and the refusal costs the leaf a terminal failed attempt where file mode
    refuses before any attempt."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        run = commit_one_leaf_as_external(root)
        withhold_from_external(root, ORIGINAL_ITEM)
        before = store_head(root)
        resumed = resume_as_external(root)
        leaf = resumed["candidates"][0]["leaves"][0]
        assert (resumed["status"], leaf["outcome"], leaf["attempts"]) == ("stale", "pending", 0), resumed
        assert store_head(root) == before and curation.CurationStore(root).reconstruct(run)["receipts"] == []
    finally:
        service.stop()


def test_a_direct_curation_apply_to_a_partly_withheld_collection_refuses_before_approval(env, capsys, monkeypatch):
    """A direct apply must preserve the unapproved run when its collection becomes partly withheld."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        run = commit_one_leaf_as_external(root)
        plans = curation.CurationStore(root)
        plan_id, fingerprint = plans.identities(run)
        withhold_from_external(root, ORIGINAL_ITEM)
        before = store_head(root)
        assert refusal(lambda: maintain_as(
            EXTERNAL_CALLER, root, curation_action="apply", run_id=run, plan_id=plan_id,
            expected_plan_fingerprint=fingerprint, why="Approve the sealed item.",
            idempotency_key="direct-apply-partly-withheld",
        ))[0] == "CURATION_BINDING_STALE"
        assert not plans.approval_path(run).exists()
        assert plans.reconstruct(run)["receipts"] == []
        assert store_head(root) == before
    finally:
        service.stop()


def test_a_leaf_committed_to_a_collection_since_partly_withheld_is_not_read_back_as_verified(env, capsys, monkeypatch):
    """Defect: the store's check of a committed step raises when the caller can read the item but not the
    whole collection, so the final coverage pass ends in a raw error instead of reporting that it cannot
    verify the leaf."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        commit_one_leaf_as_external(root)
        assert resume_as_external(root)["status"] == "ok"
        withhold_from_external(root, ORIGINAL_ITEM)
        covered = episode_as(EXTERNAL_CALLER, root, action="coverage")
        assert [item["readback"] for item in covered["receipts"]] == ["changed"], covered
    finally:
        service.stop()


def test_a_step_committed_in_a_collection_since_partly_withheld_resumes_as_uncertain_not_as_an_error(
        env, capsys, monkeypatch):
    """Defect: after a crash between the store commit and the evidence file, a caller who can no longer
    see the whole collection cannot read the step's transaction row, and the resume ends in a raw
    collection error instead of recording the run as blocked."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        run = commit_one_leaf_as_external(root)
        plan_id, fingerprint = curation.CurationStore(root).identities(run)
        with monkeypatch.context() as crash:
            crash.setattr(curation.CurationStore, "create_evidence", lambda *_a, **_k: (_ for _ in ()).throw(ProcessCut()))
            with pytest.raises(ProcessCut):
                maintain_as(EXTERNAL_CALLER, root, curation_action="apply", run_id=run, plan_id=plan_id,
                            expected_plan_fingerprint=fingerprint, why="Approve it.", idempotency_key="apply-it")
        withhold_from_external(root, ORIGINAL_ITEM)
        resume = {"curation_action": "resume", "run_id": run, "plan_id": plan_id, "idempotency_key": "resume-it"}
        assert refusal(lambda: maintain_as(EXTERNAL_CALLER, root, **resume))[0] == "CURATION_OUTCOME_UNCERTAIN"
        assert maintain(root, curation_action="status", run_id=run)["phase"] == "blocked"
    finally:
        service.stop()


def test_a_resume_with_a_page_leaf_and_a_store_records_leaf_commits_each_once(env, capsys, monkeypatch):
    """Defect: running the whole episode resume on the store thread breaks a page leaf that shares it
    with a store Records leaf, or commits either twice."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        episode(root, action="prepare", candidate="note", proposal={
            "route": "focused_note", "alternatives": [], "evidence": "complete", "title": "A plain page",
            "reason": "The episode settled one fact.",
            "leaves": [{"leaf_key": "write", "effect_revision": 1, "kind": "create-note",
                        "args": {"title": "Plain page", "slug": "plain-page",
                                 "content": "## Observations\n\n- [finding] A plain page. ^plain-page\n"}}]})
        episode(root, action="disposition", candidate="note", disposition="routed", reason="Observed.")
        run, operation, plan_id, _fingerprint = prepare_and_route(root, leaf_into_work(container_of(root)))
        reviewed = episode(root, action="candidates")
        resumed = episode(root, action="resume", input_revision=reviewed["input_revision"],
                          journal_digest=reviewed["journal_digest"])
        assert [item["outcome"] for item in resumed["executed"]] == ["committed", "committed"], resumed
        assert transactions(root, run, operation) == [("append", CID)]
        assert titles_in_work(root).count("From an episode") == 1
        assert len(list(root.glob("Knowledge Base/**/plain-page.md"))) == 1
        assert receipts(root, run) == [("committed", "leaf")]
        head = store_head(root)
        reviewed = episode(root, action="candidates")
        again = episode(root, action="resume", input_revision=reviewed["input_revision"],
                        journal_digest=reviewed["journal_digest"])
        assert again["executed"] == [] and store_head(root) == head
    finally:
        service.stop()


def test_a_second_run_of_the_same_sealed_plan_does_not_take_the_first_runs_commit_as_its_own(env, capsys, monkeypatch):
    """Defect: two runs sealed from one unchanged state on different days share a plan, so they share
    an operation identity; when the first run commits, the second, approved but not yet executed,
    finds that transaction row on its resume and reports its own step recovered-committed, though it
    wrote nothing and its seal is stale."""
    import datetime as dt

    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        first, operation, plan_id, fingerprint = prepare_and_route(root, leaf_into_work(container_of(root)))
        sealed = curation.CurationStore(root).load_plan(first)
        base = {name: value for name, value in sealed.items() if name not in {"binding_manifest", "registry_ids"}}
        second = curation.CurationStore(root).create_forward(
            base, binding_manifest=sealed["binding_manifest"], registry_ids=sealed["registry_ids"],
            today=dt.date.today() + dt.timedelta(days=1), allow_records=True)["run_id"]
        assert second != first and curation.CurationStore(root).identities(second) == (plan_id, fingerprint)
        with monkeypatch.context() as crash:
            crash.setattr(curation, "_fault_barrier", process_cut_at("after-prepared-state"))
            with pytest.raises(ProcessCut):
                apply_run(root, second, plan_id, fingerprint, key="apply-the-second-run")
        apply_run(root, first, plan_id, fingerprint)
        head = store_head(root)
        assert refusal(lambda: resume_run(root, second, plan_id))[0] == "CURATION_BINDING_STALE"
        assert store_head(root) == head and receipts(root, second) == [("failed", "leaf")]
        assert transactions(root, first, operation) == [("append", CID)] and transactions(root, second, operation) == []
    finally:
        service.stop()


def test_a_prepared_store_leaf_advances_the_commit_generation_once_like_a_page_leaf(env, capsys, monkeypatch):
    """Defect: preparing a store leaf takes a second mutation boundary inside the command's own, so the
    commit generation moves twice for a step that writes nothing and every stamp captured before it is
    stale."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = legacy_vault(env)
    service = migrated(root, capsys)
    try:
        record_an_episode(root)
        bumps = []
        advance = writer_lease._bump_commit_generation
        monkeypatch.setattr(writer_lease, "_bump_commit_generation", lambda *a, **k: (bumps.append(1), advance(*a, **k))[1])
        episode(root, action="prepare", candidate="work", proposal=leaf_into_work(container_of(root)))
        assert len(bumps) == 1
    finally:
        service.stop()
