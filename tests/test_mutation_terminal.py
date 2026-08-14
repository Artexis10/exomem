from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


def _terminal_module():
    try:
        from exomem import mutation_terminal
    except ImportError:
        pytest.fail("mutation terminal module is missing")
    return mutation_terminal


def test_compact_projection_leads_with_decisive_commit_fields() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "path": "Knowledge Base/Notes/Insights/decisive.md",
        "warnings": ["review a link"],
        "semantic": {"transition": "verbose"},
    }
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt-1",
        idempotency_key="public-key",
    )

    assert mutation_terminal.project_terminal(terminal, "compact") == {
        "ok": True,
        "state": "committed",
        "terminal": True,
        "status": "committed",
        "mutated": True,
        "path": "Knowledge Base/Notes/Insights/decisive.md",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": "receipt-1",
        "idempotency_key": "public-key",
        "warnings_count": 1,
    }


def test_compact_terminal_retains_completed_graph_sync_fields() -> None:
    mutation_terminal = _terminal_module()
    terminal = mutation_terminal.committed_terminal(
        {
            "graph_sync": "failed",
            "graph_sync_code": "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
            "graph_sync_checkpoint": "a" * 64,
            "graph_sync_remediation": "Run reconcile to recover the derived graph.",
        },
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    projected = mutation_terminal.project_terminal(terminal)

    assert projected["graph_sync"] == "failed"
    assert projected["graph_sync_code"] == "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    assert projected["graph_sync_checkpoint"] == "a" * 64
    assert projected["graph_sync_remediation"] == "Run reconcile to recover the derived graph."


def test_compact_terminal_retains_finalized_graph_rebuild_fields() -> None:
    mutation_terminal = _terminal_module()
    terminal = mutation_terminal.committed_terminal(
        {
            "graph_rebuild_requested": True,
            "graph_rebuild_applicable": True,
            "graph_rebuild_status": "cleared",
            "graph_quarantine_id": "a" * 24,
            "graph_rebuild_warning": None,
            "_graph_rebuild_handoff": {"private": "must not escape"},
        },
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    compact = mutation_terminal.project_terminal(terminal, "compact")

    assert compact["graph_rebuild_status"] == "cleared"
    assert compact["graph_quarantine_id"] == "a" * 24
    assert "_graph_rebuild_handoff" not in compact


def test_terminal_and_replay_views_strip_graph_rebuild_handoff() -> None:
    mutation_terminal = _terminal_module()
    terminal = mutation_terminal.committed_terminal(
        {
            "graph_rebuild_requested": True,
            "graph_rebuild_applicable": True,
            "graph_rebuild_status": "cleared",
            "_graph_rebuild_handoff": {"private": "must not escape"},
        },
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    assert "_graph_rebuild_handoff" not in terminal["leaf_result"]
    assert "_graph_rebuild_handoff" not in mutation_terminal.project_terminal(
        terminal, "full"
    )["diagnostics"]
    assert "_graph_rebuild_handoff" not in mutation_terminal.project_terminal(
        terminal, "legacy"
    )


def test_full_projection_adds_the_complete_leaf_result_only_under_diagnostics() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "paths": ["Knowledge Base/one.md", "Knowledge Base/two.md"],
        "warnings": [],
        "semantic": {"transition": "complete"},
    }
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    projected = mutation_terminal.project_terminal(terminal, "full")

    assert projected == {
        "ok": True,
        "state": "committed",
        "terminal": True,
        "status": "committed",
        "mutated": True,
        "paths": ["Knowledge Base/one.md", "Knowledge Base/two.md"],
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": None,
        "warnings_count": 0,
        "diagnostics": raw,
    }
    assert "semantic" not in {key for key in projected if key != "diagnostics"}


def test_legacy_projection_returns_the_raw_leaf_result() -> None:
    mutation_terminal = _terminal_module()
    raw = {"path": "Knowledge Base/legacy.md", "semantic": {"raw": True}}
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt",
        idempotency_key=None,
    )

    assert mutation_terminal.project_terminal(terminal, "legacy") is raw


def test_preupgrade_raw_completed_result_is_never_fabricated_into_a_terminal() -> None:
    mutation_terminal = _terminal_module()
    raw = {"path": "Knowledge Base/pre-upgrade.md", "warnings": ["old"]}

    assert mutation_terminal.project_terminal(raw, "compact") is raw
    assert mutation_terminal.project_terminal(raw, "full") is raw
    assert mutation_terminal.project_terminal(raw, "legacy") is raw


def test_response_detail_is_removed_from_an_owned_payload_copy() -> None:
    mutation_terminal = _terminal_module()
    original = {"path": "Knowledge Base/note.md", "response_detail": "full"}

    payload, detail = mutation_terminal.split_response_detail(original)

    assert payload == {"path": "Knowledge Base/note.md"}
    assert detail == "full"
    assert original["response_detail"] == "full"


def test_record_replay_terminal_is_not_presented_as_a_new_commit() -> None:
    mutation_terminal = _terminal_module()
    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": "a" * 64,
        "after_item_hash": "a" * 64,
        "before_container_hash": "b" * 64,
        "after_container_hash": "b" * 64,
        "affected_paths": ["Knowledge Base/Records/log.md"],
        "payload_hash": "c" * 64,
        "outcome": "replayed",
        "audit_correlation": "d" * 24,
    }

    terminal = mutation_terminal.replayed_terminal(
        receipt,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt-1",
        idempotency_key="same-call",
    )

    assert mutation_terminal.project_terminal(terminal) == {
        "ok": True,
        "status": "replayed",
        "mutated": False,
        "paths": ["Knowledge Base/Records/log.md"],
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": "receipt-1",
        "idempotency_key": "same-call",
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": "a" * 64,
        "after_item_hash": "a" * 64,
        "before_container_hash": "b" * 64,
        "after_container_hash": "b" * 64,
        "affected_paths": ["Knowledge Base/Records/log.md"],
        "payload_hash": "c" * 64,
        "outcome": "replayed",
        "audit_correlation": "d" * 24,
        "warnings_count": 0,
    }


def test_compact_lifecycle_record_receipt_retains_its_closed_receipt_fields() -> None:
    mutation_terminal = _terminal_module()
    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 2,
        "operation": "revise",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_manifest_hash": "a" * 64,
        "after_manifest_hash": "b" * 64,
        "before_container_hash": "c" * 64,
        "after_container_hash": "d" * 64,
        "affected_paths": ["Knowledge Base/Records/log/_collection.md"],
        "payload_hash": "e" * 64,
        "outcome": "committed",
        "audit_correlation": "f" * 24,
        "continuity": True,
        "acknowledged_gap_codes": [],
        "gap_fingerprint": None,
        "checkpoint_snapshot_hash": None,
        "minimum_reader_version": 2,
    }

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            receipt,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id="receipt-1",
            idempotency_key="same-call",
        )
    )

    assert projected == {
        "ok": True,
        "state": "committed",
        "terminal": True,
        "status": "committed",
        "mutated": True,
        "paths": ["Knowledge Base/Records/log/_collection.md"],
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": "receipt-1",
        "idempotency_key": "same-call",
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 2,
        "operation": "revise",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_manifest_hash": "a" * 64,
        "after_manifest_hash": "b" * 64,
        "before_container_hash": "c" * 64,
        "after_container_hash": "d" * 64,
        "affected_paths": ["Knowledge Base/Records/log/_collection.md"],
        "payload_hash": "e" * 64,
        "outcome": "committed",
        "audit_correlation": "f" * 24,
        "continuity": True,
        "acknowledged_gap_codes": [],
        "gap_fingerprint": None,
        "checkpoint_snapshot_hash": None,
        "minimum_reader_version": 2,
        "warnings_count": 0,
    }


def test_unvalidated_affected_paths_do_not_become_terminal_paths() -> None:
    mutation_terminal = _terminal_module()

    terminal = mutation_terminal.committed_terminal(
        {"affected_paths": ["Knowledge Base/private.md"]},
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    assert mutation_terminal.project_terminal(terminal)["paths"] == []


@pytest.mark.parametrize("detail", ["verbose", []])
def test_unknown_response_detail_is_rejected_before_invocation(detail) -> None:
    mutation_terminal = _terminal_module()

    with pytest.raises(ValueError, match="response_detail"):
        mutation_terminal.split_response_detail({"response_detail": detail})


def test_compound_source_result_uses_its_explicit_nested_path_and_warnings() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "source": {
            "path": "Knowledge Base/Sources/Other/source.md",
            "warnings": ["source warning"],
        },
        "compile_guidance": {"available": True},
    }

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert projected["path"] == "Knowledge Base/Sources/Other/source.md"
    assert projected["warnings_count"] == 1


def test_compact_record_receipt_uses_the_content_free_whitelist() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": None,
        "after_item_hash": "a" * 64,
        "before_container_hash": "b" * 64,
        "after_container_hash": "c" * 64,
        "affected_paths": ["Knowledge Base/Records/example.md"],
        "payload_hash": "d" * 64,
        "outcome": "committed",
        "audit_correlation": "e" * 24,
        "why": "private rationale",
        "values": {"secret": "canonical item value"},
    }

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert projected["collection_id"] == raw["collection_id"]
    assert projected["after_item_hash"] == raw["after_item_hash"]
    assert "why" not in projected
    assert "values" not in projected


def test_compact_record_receipt_rejects_manifest_only_create_without_audit_claim() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "create",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_container_hash": None,
        "after_container_hash": None,
        "affected_paths": ["Knowledge Base/Records/example/_collection.md"],
        "payload_hash": None,
        "outcome": "committed",
        "audit_correlation": None,
    }

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert "collection_id" not in projected


@pytest.mark.parametrize("operation", ["append", "update"])
def test_compact_record_projection_rejects_missing_required_mutation_hashes(
    operation: str,
) -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": operation,
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": None,
        "after_item_hash": None,
        "before_container_hash": None,
        "after_container_hash": None,
        "affected_paths": ["Knowledge Base/Records/example.md"],
        "payload_hash": None,
        "outcome": "committed",
        "audit_correlation": "e" * 24,
    }
    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )
    assert "collection_id" not in projected


def test_compact_record_projection_rejects_create_with_item_identity() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "create",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": None,
        "after_item_hash": None,
        "before_container_hash": None,
        "after_container_hash": "a" * 64,
        "affected_paths": ["Knowledge Base/Records/example.md"],
        "payload_hash": None,
        "outcome": "committed",
        "audit_correlation": "e" * 24,
    }
    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )
    assert "collection_id" not in projected


def test_record_projection_requires_an_owned_validated_receipt_sentinel() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "after_item_hash": "a" * 64,
        "after_container_hash": "b" * 64,
        "affected_paths": ["Knowledge Base/Records/example.md"],
        "outcome": "committed",
    }
    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )
    assert "collection_id" not in projected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"old_path": "Knowledge Base/old.md", "new_path": "Knowledge Base/new.md"},
            {"paths": ["Knowledge Base/old.md", "Knowledge Base/new.md"]},
        ),
        (
            {"restored_path": "Knowledge Base/restored.md"},
            {"path": "Knowledge Base/restored.md"},
        ),
        ({"saved": True}, {"paths": []}),
    ],
)
def test_explicit_multi_path_restore_and_safe_fallback_adapters(raw, expected) -> None:
    mutation_terminal = _terminal_module()

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert {key: projected[key] for key in expected} == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"manifest": {"path": "Knowledge Base/_Adoption/manifest.md"}},
            {"path": "Knowledge Base/_Adoption/manifest.md"},
        ),
        (
            {
                "copy": {
                    "copied_sources": [
                        {"source_path": "Knowledge Base/Sources/Imported/one.md"},
                        {"source_path": "Knowledge Base/Sources/Imported/two.md"},
                    ]
                }
            },
            {
                "paths": [
                    "Knowledge Base/Sources/Imported/one.md",
                    "Knowledge Base/Sources/Imported/two.md",
                ]
            },
        ),
        (
            {
                "compile_plan": {
                    "copied_sources": [
                        {"source_path": ("Knowledge Base/Sources/Imported/compiled-one.md")},
                        {"source_path": ("Knowledge Base/Sources/Imported/compiled-two.md")},
                    ]
                }
            },
            {
                "paths": [
                    "Knowledge Base/Sources/Imported/compiled-one.md",
                    "Knowledge Base/Sources/Imported/compiled-two.md",
                ]
            },
        ),
    ],
)
def test_adopt_write_results_have_explicit_compact_paths(raw, expected) -> None:
    mutation_terminal = _terminal_module()

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert {key: projected[key] for key in expected} == expected


def test_one_committed_identity_projects_compact_full_and_legacy_without_rerun(
    tmp_path,
) -> None:
    from exomem import writer_lease

    calls = 0
    raw = {
        "path": "Knowledge Base/Notes/Insights/once.md",
        "warnings": ["one warning"],
        "semantic": {"transition": "verbose"},
    }

    def leaf(vault, *, value: int):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        writer_lease.mark_active_mutation_committed()
        return raw

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    first_request_id = "11111111-1111-4111-8111-111111111111"
    compact = manager.invoke(
        command,
        (vault,),
        {"value": 7, "response_detail": "compact"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
        mutation_request_id=first_request_id,
    )
    full = manager.invoke(
        command,
        (vault,),
        {"value": 7, "response_detail": "full"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
        mutation_request_id="22222222-2222-4222-8222-222222222222",
    )
    legacy = manager.invoke(
        command,
        (vault,),
        {"value": 7, "response_detail": "legacy"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
    )

    assert compact["request_id"] == first_request_id
    assert compact["receipt_id"]
    assert compact["idempotency_key"] == "same-public-key"
    assert compact["warnings_count"] == 1
    assert full == {**compact, "diagnostics": raw}
    assert legacy == raw
    assert "replayed" not in compact
    assert calls == 1


@pytest.mark.parametrize(
    ("public_key", "expected_key"),
    [("visible-client-key", "visible-client-key"), (None, None)],
)
def test_internal_replay_key_is_separate_from_public_terminal_identity(
    tmp_path,
    public_key,
    expected_key,
) -> None:
    from exomem import writer_lease

    def leaf(vault):  # noqa: ANN001, ARG001
        writer_lease.mark_active_mutation_committed()
        return {"path": "Knowledge Base/hosted.md", "warnings": []}

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    internal_key = "hosted:" + "a" * 64

    terminal = manager.invoke(
        command,
        (vault,),
        {},
        idempotency_key=internal_key,
        public_idempotency_key=public_key,
    )

    assert terminal.get("idempotency_key") == expected_key
    if expected_key is None:
        assert "idempotency_key" not in terminal
    assert internal_key not in repr(terminal)


@pytest.mark.parametrize("public_key", ["visible-client-key", None])
def test_structured_errors_use_only_the_public_idempotency_key(
    tmp_path,
    public_key,
) -> None:
    from exomem import writer_lease
    from exomem.cli_ops import OpError, error_dict

    command = SimpleNamespace(
        name="remember",
        leaf=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OpError("MUTATION_BUSY", "synthetic busy boundary")
        ),
        read_only=False,
    )
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    internal_key = "hosted:" + "b" * 64

    with pytest.raises(OpError) as caught:
        manager.invoke(
            command,
            (tmp_path / "vault",),
            {},
            idempotency_key=internal_key,
            public_idempotency_key=public_key,
        )

    payload = error_dict(caught.value)
    assert payload.get("idempotency_key") == public_key
    if public_key is None:
        assert "idempotency_key" not in payload
    assert internal_key not in repr(payload)


def test_acknowledgement_loss_replays_the_persisted_original_terminal(
    tmp_path,
) -> None:
    from exomem import writer_lease

    calls = 0
    interrupt = True
    raw = {"path": "Knowledge Base/Notes/Insights/ack-lost.md", "warnings": []}

    def leaf(vault):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        writer_lease.mark_active_mutation_committed()
        return raw

    def after_terminal_persisted() -> None:
        nonlocal interrupt
        if interrupt:
            interrupt = False
            raise asyncio.CancelledError

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        after_terminal_persisted=after_terminal_persisted,
    )
    first_request_id = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(asyncio.CancelledError):
        manager.invoke(
            command,
            (vault,),
            {"response_detail": "compact"},
            idempotency_key="ack-lost",
            mutation_request_id=first_request_id,
        )

    replay = manager.invoke(
        command,
        (vault,),
        {"response_detail": "full"},
        idempotency_key="ack-lost",
        mutation_request_id="22222222-2222-4222-8222-222222222222",
    )

    assert replay["request_id"] == first_request_id
    assert replay["diagnostics"] == raw
    assert calls == 1


def test_result_without_active_commit_marker_keeps_its_existing_shape(tmp_path) -> None:
    from exomem import writer_lease

    raw = {"validate_only": True, "mutated": False, "semantic": {"preview": True}}

    def preview(vault, *, validate_only: bool = False):  # noqa: ANN001, ARG001
        assert validate_only is True
        return raw

    command = SimpleNamespace(name="edit_memory", leaf=preview, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))

    result = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"validate_only": True, "response_detail": "full"},
        read_only=True,
    )

    assert result is raw


def test_preupgrade_completed_receipt_replays_raw_without_leaf_execution(tmp_path) -> None:
    from exomem import writer_lease

    raw = {"path": "Knowledge Base/pre-upgrade.md", "semantic": {"legacy": True}}
    command = SimpleNamespace(
        name="remember",
        leaf=lambda *_args, **_kwargs: pytest.fail("legacy receipt reran the leaf"),
        read_only=False,
    )
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    payload = {"value": 3}
    digest = writer_lease._command_digest(command, payload)
    key, _, _ = writer_lease._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject=tmp_path / "vault",
        digest=digest,
        idempotency_key="pre-upgrade",
        principal_scope=None,
    )
    assert key is not None
    assert manager.idempotency._claim_or_inspect(key, digest, None) == ("owner", None)
    manager.idempotency._persist_completed(key, digest, raw)

    replay = manager.invoke(
        command,
        (tmp_path / "vault",),
        {**payload, "response_detail": "full"},
        idempotency_key="pre-upgrade",
    )

    assert replay == raw


def test_mutation_response_detail_is_declared_once_for_every_shared_surface() -> None:
    from exomem import cli_ops, command_surface
    from exomem.commands import product_commands_for

    command = next(item for item in product_commands_for("mcp") if item.name == "remember")
    [parameter] = [item for item in command.params if item.name == "response_detail"]
    bound = command_surface.bind_vault(
        command.leaf,
        object(),
        name=command.name,
        command=command,
    )

    assert parameter.choices == ("compact", "full", "legacy")
    assert inspect.signature(bound).parameters["response_detail"].default == "compact"
    assert cli_ops.coerce(command.params, {"response_detail": "full"}) == {
        "response_detail": "full"
    }
