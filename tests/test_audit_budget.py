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


def test_concurrent_withholding_is_fresh_and_expired_release_contains_no_metadata(vault, monkeypatch):
    from test_governance_egress import _external, write_rule, write_scope

    from exomem import published_admission, writer_lease
    from exomem.cli_ops import _AuditBudgetExhausted, error_dict
    from exomem.governance.principal import request_scope

    path = "Knowledge Base/Notes/withheld.md"
    (vault / path).write_text("---\ntype: insight\n---\nPrivate evidence", encoding="utf-8")
    report = {"findings": [{"path": path, "detail": "private evidence"}], "summary": {"orphan_entity": 9}}
    body_scope = []
    decisions = []
    budget = request_budget.RequestBudget(seconds=50)
    command = next(item for item in commands.COMMANDS if item.name == "audit")

    def body(*args, **kwargs):
        body_scope.append(published_admission.request_decisions())
        write_scope(vault, paths=path)
        write_rule(vault, ceiling=0)
        return report

    decide = egress._decide_path

    def withhold_then_expire(*args, **kwargs):
        decision = decide(*args, **kwargs)
        if request_budget.current_checkpoint() is not None:
            assert published_admission.request_decisions() is not body_scope[0]
            decisions.append(decision)
            budget.deadline = 0
        return decision

    monkeypatch.setattr(writer_lease.get_manager(), "invoke", body)
    monkeypatch.setattr(egress, "_decide_path", withhold_then_expire)
    token = request_budget.set_current(budget)
    try:
        with request_scope(_external()):
            with pytest.raises(_AuditBudgetExhausted) as caught:
                writer_lease.invoke_command(command, vault)
    finally:
        request_budget.reset_current(token)
    assert decisions and decisions[0].level < egress.RELEASE_FLOOR
    terminal = error_dict(caught.value)
    assert terminal["code"] == "AUDIT_BUDGET_EXHAUSTED"
    assert path not in str(terminal) and "orphan_entity" not in str(terminal)


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
