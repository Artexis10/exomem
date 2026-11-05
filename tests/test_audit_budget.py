"""An expired audit must never turn incomplete evidence into a clean census."""

import pytest
from test_collection_store_service import service as service  # noqa: F401

from exomem import audit, commands, request_budget
from exomem.cli_ops import OpError
from exomem.governance import egress


def test_interrupted_corpus_cannot_report_an_orphan(vault, monkeypatch):
    budget = request_budget.RequestBudget(seconds=50)
    parse = audit.find_module._parse_page
    read = []

    def expire(path, *args, **kwargs):
        page = parse(path, *args, **kwargs)
        read.append(path)
        request_budget.current().deadline = 0
        return page

    monkeypatch.setattr(audit.find_module, "_parse_page", expire)
    report = audit.audit(vault, categories=["orphan_entity", "broken_wikilink"], budget=budget)

    assert len(read) == 1
    assert report.findings == []
    assert report.summary == {}
    assert report.metadata["coverage"]["corpus_complete"] is False
    assert report.metadata["coverage"]["completed_categories"] == []
    assert "audit.corpus" in budget.truncated
    assert "audit.orphan_entity" in budget.skipped


@pytest.mark.parametrize("detail", ["actionable", "full"])
def test_completed_category_survives_interrupted_semantic_check(vault, monkeypatch, detail):
    budget = request_budget.RequestBudget(seconds=50)
    finding = audit.AuditFinding(category="tag_inconsistency", severity="warn", path="Knowledge Base/Notes/a.md", detail="Tag variants")
    monkeypatch.setattr(audit, "_check_tag_inconsistency", lambda pages: [finding])

    def expire(*args, **kwargs):
        request_budget.current().deadline = 0
        return [], {"semantic_category_summary": {"semantic_contract_drift": 7}}

    monkeypatch.setattr(audit, "_check_semantic_contract_drift", expire)
    report = audit.audit(vault, categories=["tag_inconsistency", "semantic_contract_drift"], budget=budget)
    public = report.as_public_dict(detail=detail)

    assert public["findings"] == [finding.as_dict()]
    assert public["summary"] == {"tag_inconsistency": 1}
    assert public["presentation"]["upstream_findings_complete"] is False
    assert public["presentation"]["observation_complete"] is False
    assert public["presentation"]["upstream_omitted_count"] is None
    assert public["presentation"]["legacy_backlog_complete"] is False
    assert "audit.semantic_contract_drift" in budget.truncated


def test_caller_error_code_cannot_bypass_error_filter(vault, monkeypatch):
    scans = []
    monkeypatch.setattr(egress._ArtifactReferenceGate, "gate_payload", lambda self, value, **kwargs: scans.append(value) or value)
    error = OpError("AUDIT_BUDGET_EXHAUSTED", "Knowledge Base/Notes/hidden.md")
    egress.postfilter_error("audit", error, vault)
    assert scans


def test_fixed_budget_terminal_never_constructs_an_artifact_census(vault, monkeypatch):
    from exomem.cli_ops import _AuditBudgetExhausted

    def forbidden(*args, **kwargs):
        pytest.fail("fixed terminal attempted content enumeration")

    monkeypatch.setattr(egress, "_ArtifactReferenceGate", forbidden)
    error = _AuditBudgetExhausted()
    assert egress.postfilter_error("audit", error, vault) is error
    assert error.as_public_dict() == {
        "code": "AUDIT_BUDGET_EXHAUSTED",
        "message": "The audit could not finish fresh authorization within its request budget.",
        "remediation": "Retry the audit with fewer categories.",
    }


@pytest.mark.parametrize("name,arguments,expected", [
    ("audit", {}, True),
    ("review_memory", {"mode": "audit"}, True),
    ("maintain_memory", {}, True),
    ("maintain_memory", {"mode": "audit"}, True),
    ("review_memory", {}, False),
    ("maintain_memory", {"mode": "reconcile"}, False),
])
def test_only_resolved_audit_routes_select_release_deadline(name, arguments, expected):
    command = next(item for item in (*commands.COMMANDS, *commands.PRODUCT_COMMANDS) if item.name == name)
    assert commands.invocation_is_audit(command, arguments) is expected


def test_expired_body_budget_keeps_attention_and_local_audit_unbounded(vault):
    token = request_budget.set_current(request_budget.RequestBudget(seconds=0))
    try:
        attention = commands.op_attention(vault, categories=["unprocessed_source"])
        local = audit.audit(vault, categories=["tag_inconsistency"])
        budgeted = commands.op_audit(vault, categories=["tag_inconsistency"])
    finally:
        request_budget.reset_current(token)
    assert "items" in attention
    assert local.metadata is None
    assert budgeted["meta"]["coverage"]["complete"] is False
    assert budgeted["budget"]["applied"] is True


def test_dispatch_discards_whole_report_when_authorization_expires(vault, monkeypatch):
    from test_governance_egress import write_rule, write_scope

    from exomem import writer_lease
    from exomem.cli_ops import _AuditBudgetExhausted, error_dict

    write_scope(vault)
    write_rule(vault, ceiling=0)

    def cold_census_forbidden(*args, **kwargs):
        pytest.fail("fixed terminal entered the cold artifact gate")

    monkeypatch.setattr(egress, "_ArtifactReferenceGate", cold_census_forbidden)
    command = next(item for item in commands.COMMANDS if item.name == "audit")
    payload = {"findings": [{"path": "Knowledge Base/Notes/private.md", "detail": "private"}], "summary": {"private": 12}}
    # Preserve the audit leaf identity for metadata routing; inject the body result at the manager boundary.
    monkeypatch.setattr(writer_lease.get_manager(), "invoke", lambda *args, **kwargs: payload)
    budget = request_budget.RequestBudget(seconds=50)
    original = egress.filter_withheld_entries

    def expire(*args, **kwargs):
        budget.deadline = 0
        return original(*args, **kwargs)

    monkeypatch.setattr(egress, "filter_withheld_entries", expire)
    token = request_budget.set_current(budget)
    try:
        with pytest.raises(_AuditBudgetExhausted) as caught:
            writer_lease.invoke_command(command, vault)
    finally:
        request_budget.reset_current(token)
    terminal = error_dict(caught.value)
    assert terminal["code"] == "AUDIT_BUDGET_EXHAUSTED"
    assert "private" not in str(terminal)
    assert "12" not in str(terminal)


def test_accepted_store_batch_stops_and_the_worker_serves_the_next_write(service):
    import time

    from test_collection_store_service import LATER, call
    from test_collection_store_writer import CID

    from exomem.collection_store import preview, runtime

    seen = []

    def read(root, path):
        seen.append(path)
        time.sleep(2.1)
        return {"path": path}

    server = runtime._server(service.root)
    assert server.call(lambda: "warm", read_only=True, wake_maintenance=False) == "warm"
    budget = request_budget.RequestBudget(seconds=2)
    batch = preview._Decide(read, service.root, {"first": (("first",), {}), "second": (("second",), {})})
    token = request_budget.set_current(budget)
    try:
        with request_budget.checking(request_budget.ReadCheckpoint(budget)):
            with pytest.raises(request_budget.ReadBudgetExhausted):
                server.call(batch, read_only=True, wake_maintenance=False)
    finally:
        request_budget.reset_current(token)
    assert seen == ["first"]
    assert server.thread.is_alive()
    assert server.call(lambda: "next read", read_only=True, wake_maintenance=False) == "next read"
    result = call(service.root, "record_memory", action="append", collection=CID,
                  item={"title": "After interrupted audit"}, item_key=LATER, why="test worker recovery")
    assert result["outcome"] == "committed"


def test_expired_owner_abandons_its_corpus_flight_for_a_healthy_waiter(vault, monkeypatch, corpus_cache_on):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from exomem import freshness, relation_registry, semantic_contract, semantic_language_registry

    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    corpus_options = {"registry": relation_registry.load_registry(vault),
                      "language_registry": semantic_language_registry.load_registry(vault)}
    owner_started, waiter_joined, release = threading.Event(), threading.Event(), threading.Event()
    budget = request_budget.RequestBudget(seconds=50)
    build = semantic_contract._build_corpus_context_uncached
    claim = semantic_contract._claim_corpus_context_flight

    def controlled_build(*args, **kwargs):
        if request_budget.current_checkpoint() is not None:
            owner_started.set()
            assert release.wait(5)
            budget.deadline = 0
            request_budget.checkpoint()
        return build(*args, **kwargs)

    def observed_claim(*args, **kwargs):
        flight, owner = claim(*args, **kwargs)
        if not owner:
            waiter_joined.set()
        return flight, owner

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", controlled_build)
    monkeypatch.setattr(semantic_contract, "_claim_corpus_context_flight", observed_claim)

    def expired_owner():
        with request_budget.checking(request_budget.ReadCheckpoint(budget)):
            return semantic_contract.build_corpus_context(vault, **corpus_options)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(expired_owner)
        try:
            assert owner_started.wait(5)
            waiter = pool.submit(semantic_contract.build_corpus_context, vault, **corpus_options)
            assert waiter_joined.wait(5)
        finally:
            release.set()
        with pytest.raises(request_budget.ReadBudgetExhausted):
            owner.result(timeout=5)
        assert waiter.result(timeout=5).pages
    assert not semantic_contract._CORPUS_CONTEXT_FLIGHTS
    semantic_contract.reset_corpus_context_cache()


def test_expired_waiter_leaves_a_foreign_corpus_owner_running(vault, monkeypatch, corpus_cache_on):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from exomem import freshness, relation_registry, semantic_contract, semantic_language_registry

    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    corpus_options = {"registry": relation_registry.load_registry(vault),
                      "language_registry": semantic_language_registry.load_registry(vault)}
    owner_started, release = threading.Event(), threading.Event()
    budget = request_budget.RequestBudget(seconds=50)
    build = semantic_contract._build_corpus_context_uncached
    flight_type = semantic_contract._CorpusContextFlight

    class ExpireWaiter(threading.Event):
        def wait(self, timeout=None):
            if request_budget.current_checkpoint() is not None:
                assert timeout <= budget.remaining()
                budget.deadline = 0
                return False
            return super().wait(timeout)

    def controlled_build(*args, **kwargs):
        owner_started.set()
        assert release.wait(5)
        return build(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", controlled_build)
    monkeypatch.setattr(semantic_contract, "_CorpusContextFlight", lambda *args, **kwargs: flight_type(*args, **kwargs, done=ExpireWaiter()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(semantic_contract.build_corpus_context, vault, **corpus_options)
        try:
            assert owner_started.wait(5)
            with request_budget.checking(request_budget.ReadCheckpoint(budget)):
                with pytest.raises(request_budget.ReadBudgetExhausted):
                    semantic_contract.build_corpus_context(vault, **corpus_options)
            assert not owner.done()
        finally:
            release.set()
        assert owner.result(timeout=5).pages
    assert not semantic_contract._CORPUS_CONTEXT_FLIGHTS
    semantic_contract.reset_corpus_context_cache()


def test_interrupted_resolver_proof_does_not_start_a_disk_fallback(vault, monkeypatch):
    from exomem import find, semantic_contract

    budget = request_budget.RequestBudget(seconds=50)

    def interrupted_proof(*args, **kwargs):
        budget.deadline = 0
        request_budget.checkpoint()

    def forbidden(*args, **kwargs):
        pytest.fail("interrupted resolver proof restarted a disk build")

    find.evict_resolver_caches(vault)
    monkeypatch.setattr(semantic_contract, "current_writer_resolver_entries", interrupted_proof)
    monkeypatch.setattr(audit.vault_module, "WikilinkResolver", forbidden)
    with request_budget.checking(request_budget.ReadCheckpoint(budget)):
        with pytest.raises(request_budget.ReadBudgetExhausted):
            find.writer_resolver_snapshot(vault)




def test_audit_allowance_interrupts_guarded_hashing_and_releases_descriptors(vault, monkeypatch):
    import hashlib
    import os

    path = vault / "Knowledge Base/Notes/large.md"
    content = b"x" * (65536 * 3)
    path.write_bytes(content)
    guard = audit.vault_module.PathGuard.capture(
        vault, path.relative_to(vault).as_posix(), leaf_policy="content",
        expected_content_hash=hashlib.sha256(content).hexdigest(),
        expected_content_size=len(content),
    )
    budget = request_budget.RequestBudget(seconds=50)
    read = os.read
    chunks = []

    def expire_after_a_chunk(descriptor, size):
        result = read(descriptor, size)
        if os.fstat(descriptor).st_size == path.stat().st_size:
            chunks.append(len(result))
            budget.deadline = 0
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", expire_after_a_chunk)
        with request_budget.checking(request_budget.ReadCheckpoint(budget)):
            with pytest.raises(request_budget.ReadBudgetExhausted):
                guard.recheck(vault)
    assert 0 < sum(chunks) < path.stat().st_size
    guard.recheck(vault)


def test_foreign_corpus_lock_owner_survives_an_expired_resolver_proof(vault):
    from concurrent.futures import ThreadPoolExecutor

    from exomem import semantic_contract

    def attempt():
        budget = request_budget.RequestBudget(seconds=0.05)
        with request_budget.checking(request_budget.ReadCheckpoint(budget)):
            return semantic_contract.current_writer_resolver_entries(vault)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:
            pending = pool.submit(attempt)
            with pytest.raises(request_budget.ReadBudgetExhausted):
                pending.result(timeout=1)
    # The proof releases no foreign lock and leaves the next owner's build usable.
    assert semantic_contract.build_corpus_context(vault).pages


def test_matching_corpus_cache_wait_uses_the_same_allowance(vault, monkeypatch, corpus_cache_on):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from exomem import semantic_contract

    assert semantic_contract.build_corpus_context(vault).pages
    census = semantic_contract._corpus_census

    def allow_only_the_lock_wait(*args, **kwargs):
        result = census(*args, **kwargs)
        checkpoint = request_budget.current_checkpoint()
        if checkpoint is not None:
            checkpoint.budget.deadline = time.monotonic() + 0.05
        return result

    monkeypatch.setattr(semantic_contract, "_corpus_census", allow_only_the_lock_wait)

    def attempt():
        budget = request_budget.RequestBudget(seconds=50)
        with request_budget.checking(request_budget.ReadCheckpoint(budget)):
            return semantic_contract.build_corpus_context(vault)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:
            pending = pool.submit(attempt)
            with pytest.raises(request_budget.ReadBudgetExhausted):
                pending.result(timeout=1)
    assert semantic_contract.build_corpus_context(vault).pages


def test_store_authorization_can_use_the_final_release_reserve(service):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from exomem.collection_store import runtime

    server = runtime._server(service.root)
    serving, release, requested = threading.Event(), threading.Event(), threading.Event()

    def foreign_work():
        serving.set()
        assert release.wait(5)

    def authorize():
        budget = request_budget.RequestBudget(seconds=2)
        token = request_budget.set_current(budget)
        try:
            with request_budget.checking(request_budget.ReadCheckpoint(budget)):
                requested.set()
                return server.call(lambda: "fresh authorization", read_only=True, wake_maintenance=False)
        finally:
            request_budget.reset_current(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(server.call, foreign_work, read_only=True, wake_maintenance=False)
        try:
            assert serving.wait(5)
            audited = pool.submit(authorize)
            assert requested.wait(5)
            time.sleep(0.05)
            assert not audited.done(), "the store subtracted the delivery reserve twice"
        finally:
            release.set()
        owner.result(timeout=5)
        assert audited.result(timeout=5) == "fresh authorization"


def test_receipt_audit_stops_at_the_record_where_its_allowance_expires(vault, monkeypatch):
    import json

    directory = vault / "Knowledge Base/_Governance/events" / ("a" * 32)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-11.jsonl").write_bytes(b"{}\n" * 1000)
    load = json.loads
    parsed = []

    def expire_at_first_record(value, *args, **kwargs):
        if value == b"{}":
            parsed.append(value)
            request_budget.current().deadline = 0
        return load(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", expire_at_first_record)
    budget = request_budget.RequestBudget(seconds=50)
    report = audit.audit(vault, categories=["governance_receipts"], budget=budget)

    assert len(parsed) == 1
    assert report.findings == [] and report.summary == {}
    assert report.metadata["coverage"]["complete"] is False
    assert report.metadata["coverage"]["completed_categories"] == []
    assert "audit.governance_receipts" in budget.truncated


def test_an_audit_checkpoint_does_not_cancel_ordinary_receipt_verification_or_append(vault):
    from exomem.governance import receipts

    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    expired = request_budget.RequestBudget(seconds=0)
    with request_budget.checking(request_budget.ReadCheckpoint(expired)):
        assert receipts.verify_chain(vault)["valid"] is True
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    verified = receipts.verify_chain(vault)
    assert verified["valid"] is True
    assert next(iter(verified["instances"].values()))["tail_seq"] == 2


def test_unavailable_requested_category_is_not_a_completed_check(vault, monkeypatch):
    from exomem.collection_store import ownership

    def unavailable(*args, **kwargs):
        raise ownership.unavailable()

    monkeypatch.setattr(ownership, "current", unavailable)
    report = audit.audit(vault, categories=["collection_view_corrections"],
                         budget=request_budget.RequestBudget(seconds=50))
    coverage = report.as_public_dict()["metadata"]["coverage"]
    assert coverage["collection_view_corrections"] == "unknown"
    assert coverage["completed_categories"] == []
    assert coverage["complete"] is False
    local = audit.audit(vault, categories=["collection_view_corrections"])
    assert local.metadata == {"coverage": {"collection_view_corrections": "unknown"}}


def test_capped_producer_does_not_hide_an_independently_complete_category(vault, monkeypatch):
    from exomem import artifact_role_state

    finding = audit.AuditFinding(category="artifact_role_promotion", severity="info",
                                 path="Knowledge Base/Notes/observed.md", detail="Observed role review")
    monkeypatch.setattr(artifact_role_state, "inspect", lambda *args, **kwargs: ([finding], {
        "artifact_role_promotion": "capped", "transient_state_review": "complete",
    }))
    report = audit.audit(vault, categories=["artifact_role_promotion", "transient_state_review"],
                         budget=request_budget.RequestBudget(seconds=50))
    coverage = report.as_public_dict()["metadata"]["coverage"]
    assert coverage["artifact_role_promotion"] == "capped"
    assert coverage["completed_categories"] == ["transient_state_review"]
    assert coverage["complete"] is False
    assert report.findings == [finding]


def test_receipt_audit_stops_when_record_validation_exhausts_its_allowance(vault, monkeypatch):
    from exomem.governance import receipts

    directory = vault / "Knowledge Base/_Governance/events" / ("a" * 32)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-11.jsonl").write_bytes(b"{}\n" * 1000)
    envelope_error = receipts._envelope_error
    validated = []

    def expire_at_first_validation(record):
        validated.append(record)
        request_budget.current().deadline = 0
        return envelope_error(record)

    monkeypatch.setattr(receipts, "_envelope_error", expire_at_first_validation)
    budget = request_budget.RequestBudget(seconds=50)
    report = audit.audit(vault, categories=["governance_receipts"], budget=budget)

    assert len(validated) == 1
    assert report.findings == [] and report.summary == {}
    assert report.metadata["coverage"]["completed_categories"] == []
    assert "audit.governance_receipts" in budget.truncated


@pytest.mark.parametrize("revoke_at,budgeted", [("audit", True), ("filter", True), (None, True), (None, False)])
def test_actual_owner_audit_releases_no_aggregate_after_revocation(vault, monkeypatch, revoke_at, budgeted):
    from exomem import writer_lease
    from exomem.governance import raw_protection
    from exomem.governance.principal import owner_principal, request_scope

    path = "Knowledge Base/Notes/" + raw_protection.PREFIX + "record.md"
    (vault / path).write_text("---\ntype: insight\n---\nPrivate evidence", encoding="utf-8")
    permitted = [True]
    finding = audit.AuditFinding(category="tag_inconsistency", severity="warn", path=path, detail="Tag variants")
    monkeypatch.setattr(raw_protection, "_authority", lambda *args, **kwargs: (permitted[0], None))

    def check(*args, **kwargs):
        if revoke_at == "audit":
            permitted[0] = False
        return [finding]

    filtered = egress._postfilter

    def filter_then_revoke(*args, **kwargs):
        result = filtered(*args, **kwargs)
        if revoke_at == "filter":
            permitted[0] = False
        return result

    monkeypatch.setattr(audit, "_check_tag_inconsistency", check)
    monkeypatch.setattr(egress, "_postfilter", filter_then_revoke)
    monkeypatch.setattr(writer_lease.get_manager(), "invoke",
                        lambda command, injected, kwargs, **rest: command.leaf(*injected, **kwargs))
    command = next(item for item in commands.COMMANDS if item.name == "audit")
    token = request_budget.set_current(request_budget.RequestBudget(seconds=50) if budgeted else None)
    try:
        with request_scope(owner_principal(surface="mcp")):
            result = writer_lease.invoke_command(command, vault, categories=["tag_inconsistency"])
    finally:
        request_budget.reset_current(token)
    if revoke_at is None:
        assert result["findings"] == [finding.as_dict()]
        assert result["summary"] == {"tag_inconsistency": 1}
        assert result["presentation"]["actionable_count"] == 1
    else:
        assert result == {"available": False, "reason": egress.AUDIENCE_RESTRICTED}


def test_fresh_aggregate_walk_stops_under_the_audit_allowance(service, monkeypatch):
    from exomem import held_fs
    from exomem.collection_store import preview, runtime
    from exomem.governance.principal import owner_principal, request_scope

    budget = request_budget.RequestBudget(seconds=50)
    checkpoint = request_budget.ReadCheckpoint(budget)
    proving = [False]
    visited = []
    with held_fs.acquire(service.root).require() as filesystem:
        filesystem_type = type(filesystem)
    children = filesystem_type.children

    def expire_at_first_child(filesystem, directory, *, name_filter=None):
        if proving[0] and name_filter is not None:
            admit = name_filter

            def name_filter(name):
                visited.append(name)
                budget.deadline = 0
                return admit(name)
        return children(filesystem, directory, name_filter=name_filter)

    monkeypatch.setattr(filesystem_type, "children", expire_at_first_child)

    def authorize():
        writer = preview.bound_writer(service.root)
        with writer.read_snapshot():
            proving[0] = True
            try:
                return egress.owner_only_aggregate(service.root, checkpoint=checkpoint)
            finally:
                proving[0] = False

    with request_scope(owner_principal(surface="mcp")), request_budget.checking(checkpoint):
        with pytest.raises(request_budget.ReadBudgetExhausted):
            runtime._server(service.root).call(authorize, read_only=True, wake_maintenance=False)
    assert len(visited) == 1


def test_expired_final_aggregate_proof_uses_the_fixed_terminal_without_a_cold_census(vault, monkeypatch):
    from exomem import writer_lease
    from exomem.cli_ops import _AuditBudgetExhausted, error_dict
    from exomem.governance.principal import owner_principal, request_scope

    command = next(item for item in commands.COMMANDS if item.name == "audit")
    aggregate = egress.owner_only_aggregate
    budget = request_budget.RequestBudget(seconds=50)

    def forbid_cold_gate(*args, **kwargs):
        pytest.fail("the expired aggregate proof started an artifact census")

    def expire_in_final_proof(*args, **kwargs):
        checkpoint = kwargs.get("checkpoint")
        if checkpoint is not None:
            budget.deadline = 0
            monkeypatch.setattr(egress, "_ArtifactReferenceGate", forbid_cold_gate)
        return aggregate(*args, **kwargs)

    monkeypatch.setattr(egress, "owner_only_aggregate", expire_in_final_proof)
    monkeypatch.setattr(writer_lease.get_manager(), "invoke",
                        lambda command, injected, kwargs, **rest: command.leaf(*injected, **kwargs))
    token = request_budget.set_current(budget)
    try:
        with request_scope(owner_principal(surface="mcp")):
            with pytest.raises(_AuditBudgetExhausted) as caught:
                writer_lease.invoke_command(command, vault, categories=["tag_inconsistency"])
    finally:
        request_budget.reset_current(token)
    assert error_dict(caught.value)["code"] == "AUDIT_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("expire_at", ["body", "authorization", "error_filter", "error_ownership", "result_ownership"])
def test_admitted_audit_error_delivery_stops_when_its_allowance_expires(vault, monkeypatch, expire_at):
    from test_governance_egress import write_rule, write_scope

    from exomem import writer_lease
    from exomem.cli_ops import _AuditBudgetExhausted, error_dict
    from exomem.collection_store import ownership
    from exomem.governance import artifact_census
    from exomem.governance.principal import owner_principal, request_scope

    write_scope(vault)
    write_rule(vault, ceiling=0)
    artifact_census.clear()
    budget = request_budget.RequestBudget(seconds=50)
    command = next(item for item in commands.COMMANDS if item.name == "audit")
    failure = OpError("COLLECTION_STORE_UNAVAILABLE", "Cannot authorize Knowledge Base/Notes/neutral.md")
    bodies, post_expiry_reads = [], []
    failed = False
    filter_entries = egress.filter_withheld_entries
    list_directory = artifact_census._list_directory
    finish = ownership.ReadScope.finish

    def invoke(command, injected, kwargs, **rest):
        result = command.leaf(*injected, **kwargs)
        assert "findings" in result, "the audit must be admitted before its release fails"
        bodies.append(result)
        if expire_at == "body":
            budget.deadline = 0
            raise failure
        return result

    def fail_authorization(*args, **kwargs):
        nonlocal failed
        checkpoint = request_budget.current_checkpoint()
        if checkpoint is not None and checkpoint.budget is budget and expire_at != "result_ownership":
            failed = True
            if expire_at == "authorization":
                budget.deadline = 0
            raise failure
        return filter_entries(*args, **kwargs)

    def enumerate_directory(path, *args, **kwargs):
        if bodies and budget.remaining() == 0:
            post_expiry_reads.append(path)
        listing = list_directory(path, *args, **kwargs)
        if failed and expire_at == "error_filter":
            budget.deadline = 0
        return listing

    def finish_ownership(scope):
        if bodies and expire_at in ("error_ownership", "result_ownership"):
            budget.deadline = 0
        return finish(scope)

    monkeypatch.setattr(writer_lease.get_manager(), "invoke", invoke)
    monkeypatch.setattr(egress, "filter_withheld_entries", fail_authorization)
    monkeypatch.setattr(artifact_census, "_list_directory", enumerate_directory)
    monkeypatch.setattr(ownership.ReadScope, "finish", finish_ownership)
    token = request_budget.set_current(budget)
    try:
        with request_scope(owner_principal(surface="mcp")):
            with pytest.raises(OpError) as caught:
                writer_lease.invoke_command(command, vault, categories=["tag_inconsistency"])
    finally:
        request_budget.reset_current(token)
    assert len(bodies) == 1
    assert not post_expiry_reads, "error delivery continued its artifact census after expiry"
    assert type(caught.value) is _AuditBudgetExhausted
    assert error_dict(caught.value) == _AuditBudgetExhausted().as_public_dict()


@pytest.mark.parametrize("terminal_at", ["body", "error_filter"])
def test_fixed_budget_terminal_finishes_dispatch_with_an_expired_allowance(vault, monkeypatch, terminal_at):
    from exomem import writer_lease
    from exomem.cli_ops import _AuditBudgetExhausted
    from exomem.collection_store import ownership

    terminal = _AuditBudgetExhausted()
    budget = request_budget.RequestBudget(seconds=50)

    def fail(*args, **kwargs):
        if terminal_at == "error_filter":
            raise OpError("COLLECTION_STORE_UNAVAILABLE", "ordinary failure")
        budget.deadline = 0
        raise terminal

    def filter_error(*args, **kwargs):
        budget.deadline = 0
        raise terminal

    def forbid_proof(*args, **kwargs):
        pytest.fail("the fixed terminal attempted content filtering or ownership proof")

    monkeypatch.setattr(writer_lease.get_manager(), "invoke", fail)
    monkeypatch.setattr(egress, "postfilter_error", filter_error)
    monkeypatch.setattr(egress, "_ArtifactReferenceGate", forbid_proof)
    monkeypatch.setattr(ownership.ReadScope, "finish", forbid_proof)
    command = next(item for item in commands.COMMANDS if item.name == "audit")
    token = request_budget.set_current(budget)
    try:
        with pytest.raises(_AuditBudgetExhausted) as caught:
            writer_lease.invoke_command(command, vault, categories=["tag_inconsistency"])
    finally:
        request_budget.reset_current(token)
    assert caught.value is terminal


@pytest.mark.parametrize("name,budgeted", [("audit", True), ("audit", False), ("attention", True)])
def test_ordinary_dispatch_errors_keep_their_identity_and_chain(vault, monkeypatch, name, budgeted):
    from exomem import writer_lease

    failure = OpError("COLLECTION_STORE_UNAVAILABLE", "ordinary failure")
    cause = ValueError("original cause")

    def fail(*args, **kwargs):
        raise failure from cause

    monkeypatch.setattr(writer_lease.get_manager(), "invoke", fail)
    command = next(item for item in commands.COMMANDS if item.name == name)
    budget = request_budget.RequestBudget(seconds=50 if name == "audit" else 0) if budgeted else None
    token = request_budget.set_current(budget)
    try:
        with pytest.raises(OpError) as caught:
            writer_lease.invoke_command(command, vault)
    finally:
        request_budget.reset_current(token)
    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert caught.value.message == "ordinary failure"


@pytest.mark.parametrize("fail_at", ["body", "error_filter", "ownership"])
def test_audit_expiry_does_not_replace_fatal_or_cancellation_signals(vault, monkeypatch, fail_at):
    from asyncio import CancelledError

    from exomem import writer_lease
    from exomem.collection_store import ownership

    if fail_at == "body":
        failure = CancelledError()
    elif fail_at == "error_filter":
        failure = SystemExit(70)
    else:
        failure = KeyboardInterrupt()
    budget = request_budget.RequestBudget(seconds=50)

    def fatal(*args, **kwargs):
        budget.deadline = 0
        raise failure

    def invoke(*args, **kwargs):
        if fail_at == "body":
            fatal()
        raise OpError("COLLECTION_STORE_UNAVAILABLE", "ordinary failure")

    monkeypatch.setattr(writer_lease.get_manager(), "invoke", invoke)
    if fail_at == "error_filter":
        monkeypatch.setattr(egress, "postfilter_error", fatal)
    elif fail_at == "ownership":
        monkeypatch.setattr(ownership.ReadScope, "finish", fatal)
    command = next(item for item in commands.COMMANDS if item.name == "audit")
    token = request_budget.set_current(budget)
    try:
        with pytest.raises(type(failure)) as caught:
            writer_lease.invoke_command(command, vault)
    finally:
        request_budget.reset_current(token)
    assert caught.value is failure
