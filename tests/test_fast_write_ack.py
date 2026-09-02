from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from derived_receipt_fakes import DerivedReceiptProtocolFake

from exomem import (
    derived_receipts,
    semantic_index,
    semantic_writes,
    vault,
    writer_lease,
)
from exomem.cli_ops import OpError


def _install_protocol(
    monkeypatch: pytest.MonkeyPatch,
    fake: DerivedReceiptProtocolFake,
) -> None:
    for seam in (
        "prepare_batch",
        "prove_committed",
        "publish_pending_visibility",
        "signal_components",
        "component_status",
        "advisory_result_ref",
    ):
        monkeypatch.setattr(derived_receipts, seam, getattr(fake, seam))
    monkeypatch.setattr(
        writer_lease,
        "_PENDING_VISIBILITY_PUBLISHER",
        lambda _root, _receipt: True,
        raising=False,
    )


def _status_callback(states: dict[derived_receipts.DerivedComponent, str]):
    def status(_root, receipt, component):  # noqa: ANN001
        current = next(item for item in receipt.components if item.component is component)
        if current.state == "not_required":
            # The store never promotes a component this batch did not require.
            return current
        return replace(
            current,
            state=states.get(component, "completed"),
            failure_code=(
                "component_failed" if states.get(component) == "failed" else None
            ),
        )

    return status


def _inject_statuses(
    fake: DerivedReceiptProtocolFake,
    states: dict[derived_receipts.DerivedComponent, str],
    *,
    count: int = 64,
) -> None:
    callback = _status_callback(states)
    fake.inject("component_status", *(callback for _ in range(count)))


def _invoke_batch(
    tmp_path: Path,
    *,
    command_name: str = "remember",
    target_name: str = "fast.md",
    content: str = "# Fast\n",
    idempotency_key: str | None = None,
    response_detail: str = "compact",
    declaration: str = "semantic_states",
    extra_target_names: tuple[str, ...] = (),
) -> tuple[dict, Path]:
    """Drive one governed canonical batch through the real central write seam.

    ``declaration`` selects which existing producer names the governed note
    this write is about: the ``semantic_states`` argument the existing-page
    writer passes to :func:`vault.batch_atomic_write`, the coordinator-owned
    parent-state binding the creation writer sets around it, or neither -- an
    ungoverned batch that carries no advisory target. ``extra_target_names``
    adds further governed notes to the same canonical batch, which is the
    multi-write shape that cannot name exactly one advisory target.
    """
    root = tmp_path / "vault"
    (root / "Knowledge Base" / "Notes" / "Insights").mkdir(parents=True, exist_ok=True)
    notes = root / "Knowledge Base" / "Notes" / "Insights"
    target = notes / target_name
    targets = (target, *(notes / name for name in extra_target_names))

    def leaf(vault_root: Path):
        rel_paths = [item.relative_to(vault_root).as_posix() for item in targets]
        planned = [
            vault.PlannedWrite(item, content, create_only=not item.exists())
            for item in targets
        ]
        if declaration == "none":
            vault.batch_atomic_write(planned, vault_root=vault_root)
        else:
            states = {
                rel_path: semantic_index.build_parent_index_state(
                    vault_root, rel_path, source=content
                )
                for rel_path in rel_paths
            }
            if declaration == "semantic_states":
                vault.batch_atomic_write(
                    planned,
                    vault_root=vault_root,
                    semantic_states=states,
                )
            else:
                token = semantic_index.set_parent_states(states)
                try:
                    vault.batch_atomic_write(planned, vault_root=vault_root)
                finally:
                    semantic_index.reset_parent_states(token)
        return {
            "path": rel_paths[0],
            "warnings": [],
        }

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    command = SimpleNamespace(name=command_name, leaf=leaf, read_only=False)
    result = manager.invoke(
        command,
        (root,),
        {"response_detail": response_detail},
        idempotency_key=idempotency_key,
        mutation_request_id="11111111-1111-4111-8111-111111111111",
    )
    return result, target


@pytest.fixture(autouse=True)
def _fast_ack_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")


@pytest.mark.parametrize(
    "component",
    (
        derived_receipts.DerivedComponent.GRAPH,
        derived_receipts.DerivedComponent.EMBEDDINGS,
        derived_receipts.DerivedComponent.CLAIMS,
        derived_receipts.DerivedComponent.LEXSTORE,
        derived_receipts.DerivedComponent.RESOLVER,
        derived_receipts.DerivedComponent.WRITE_ADVISORY,
    ),
    ids=lambda component: component.value,
)
def test_slow_component_can_return_after_canonical_success_with_exact_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: derived_receipts.DerivedComponent,
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {component: "prepared"})

    terminal, target = _invoke_batch(tmp_path, target_name=f"{component.value}.md")

    assert target.read_text(encoding="utf-8") == "# Fast\n"
    assert terminal["status"] == "committed"
    if component is derived_receipts.DerivedComponent.GRAPH:
        assert terminal["graph_sync"] == "pending"
    elif component is derived_receipts.DerivedComponent.WRITE_ADVISORY:
        assert terminal["advisory_sync"] == "pending"
        assert terminal["advisory_result_ref"].startswith(
            "exomem://write-advisory-result/"
        )
    else:
        assert terminal["derived_sync"] == "pending"
        assert component.value in terminal["derived_sync_components"]
    assert fake.call_count("prepare_batch") == 1
    assert fake.call_count("prove_committed") == 1
    assert fake.call_count("publish_pending_visibility") == 1
    assert fake.call_count("signal_components") == 1


def test_receipt_prepare_failure_leaves_canonical_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    fake.inject("prepare_batch", RuntimeError("receipt store unavailable"))
    _install_protocol(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="receipt store unavailable"):
        _invoke_batch(tmp_path)

    target = tmp_path / "vault" / "Knowledge Base" / "Notes" / "Insights" / "fast.md"
    assert not target.exists()
    assert fake.call_count("prepare_batch") == 1
    assert fake.call_count("prove_committed") == 0


def test_unproven_post_commit_state_never_returns_ordinary_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()

    def unproven(_root, receipt, **kwargs):  # noqa: ANN001, ARG001
        return derived_receipts.DerivedBatchProof(
            batch_id=receipt.batch_id,
            outcome="reconcile_required",
            canonical_generation=kwargs["current_generation"],
            path_states=("other",) * len(receipt.paths),
            ready_components=(),
            canonical_replay_authorized=False,
        )

    fake.inject("prove_committed", unproven)
    _install_protocol(monkeypatch, fake)

    with pytest.raises(OpError) as caught:
        _invoke_batch(tmp_path, idempotency_key="unproven")

    assert caught.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert (tmp_path / "vault" / "Knowledge Base" / "Notes" / "Insights" / "fast.md").exists()
    assert fake.call_count("publish_pending_visibility") == 0
    assert fake.call_count("signal_components") == 0


def test_slow_component_runs_after_authority_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {derived_receipts.DerivedComponent.EMBEDDINGS: "claimed"})
    observed: list[bool] = []

    def wait_after_release(_root, _receipt, status, *, deadline_monotonic):  # noqa: ANN001, ARG001
        observed.append(writer_lease._ACTIVE_WRITE_FENCE.get() is None)
        return replace(status, state="completed")

    monkeypatch.setattr(
        writer_lease,
        "_wait_for_derived_component",
        wait_after_release,
        raising=False,
    )

    terminal, _target = _invoke_batch(tmp_path)

    assert observed and all(observed)
    assert terminal["status"] == "committed"


def test_components_share_one_absolute_post_commit_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    slow = {
        derived_receipts.DerivedComponent.RESOLVER: "claimed",
        derived_receipts.DerivedComponent.EMBEDDINGS: "claimed",
        derived_receipts.DerivedComponent.CLAIMS: "claimed",
    }
    _inject_statuses(fake, slow)
    clock = {"now": 100.0}
    deadlines: list[float] = []
    monkeypatch.setattr(vault, "_fast_ack_monotonic", lambda: clock["now"], raising=False)
    monkeypatch.setattr(
        writer_lease,
        "_fast_ack_monotonic",
        lambda: clock["now"],
        raising=False,
    )

    def sequential_wait(_root, _receipt, status, *, deadline_monotonic):  # noqa: ANN001
        deadlines.append(deadline_monotonic)
        clock["now"] += 2.1
        return status

    monkeypatch.setattr(
        writer_lease,
        "_wait_for_derived_component",
        sequential_wait,
        raising=False,
    )

    terminal, _target = _invoke_batch(tmp_path)

    assert deadlines == [102.0, 102.0, 102.0]
    assert clock["now"] == pytest.approx(106.3)
    assert terminal["derived_sync"] == "pending"


def test_settled_component_snapshot_never_invokes_timed_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("settled component invoked timed wait")

    monkeypatch.setattr(writer_lease, "_wait_for_derived_component", forbidden, raising=False)
    terminal, _target = _invoke_batch(tmp_path)

    assert terminal["derived_sync"] == "completed"
    assert terminal["advisory_sync"] == "completed"


def test_absent_component_flight_never_invokes_timed_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    pending = {component: "prepared" for component in derived_receipts.DerivedComponent}
    _inject_statuses(fake, pending)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("absent component flight invoked timed wait")

    monkeypatch.setattr(writer_lease, "_wait_for_derived_component", forbidden, raising=False)
    terminal, _target = _invoke_batch(tmp_path)

    assert terminal["derived_sync"] == "pending"
    assert terminal["advisory_sync"] == "pending"


def test_real_custody_failure_is_not_laundered_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    fake.inject("publish_pending_visibility", RuntimeError("registration failed"))
    _install_protocol(monkeypatch, fake)

    with pytest.raises(OpError) as caught:
        _invoke_batch(tmp_path, idempotency_key="publication-failure")

    assert caught.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert "pending" not in str(caught.value).lower()


@pytest.mark.parametrize(
    "route",
    (
        "remember",
        "capture_source",
        "add",
        "edit_memory",
        "replace_memory",
        "observe_memory",
        "governed_batch",
    ),
)
def test_each_governed_write_route_prepares_and_hands_off_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {})

    _invoke_batch(
        tmp_path,
        command_name=route,
        target_name=f"{route}.md",
        extra_target_names=(
            (f"{route}-second.md",) if route == "governed_batch" else ()
        ),
    )

    assert fake.call_count("prepare_batch") == 1
    assert fake.call_count("prove_committed") == 1
    assert fake.call_count("publish_pending_visibility") == 1
    assert fake.call_count("signal_components") == 1
    assert fake.call_order[:4] == (
        "prepare_batch",
        "prove_committed",
        "publish_pending_visibility",
        "signal_components",
    )


def test_default_write_route_never_runs_model_or_full_corpus_work_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {})
    calls: list[str] = []
    from exomem import embeddings, index_sync, readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(embeddings, "get_model", lambda: calls.append("model"))
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: calls.append("fanout"),
    )
    semantic_writes._prewarm_embeddings()
    _invoke_batch(tmp_path)

    assert calls == []


def test_background_completion_never_mutates_original_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    pending = {derived_receipts.DerivedComponent.EMBEDDINGS: "prepared"}
    _inject_statuses(fake, pending)

    first, _target = _invoke_batch(tmp_path, idempotency_key="immutable")
    original = json.dumps(first, sort_keys=True, separators=(",", ":"))
    calls_before = tuple(fake.calls)
    _inject_statuses(fake, {})
    replay, _target = _invoke_batch(tmp_path, idempotency_key="immutable")

    assert first["derived_sync"] == "pending"
    assert json.dumps(replay, sort_keys=True, separators=(",", ":")) == original
    assert tuple(fake.calls) == calls_before
    assert hashlib.sha256(original.encode()).hexdigest()


def _prepare_call(fake: DerivedReceiptProtocolFake) -> dict:
    return next(call for call in fake.calls if call[0] == "prepare_batch")[2]


@pytest.mark.parametrize("declaration", ("semantic_states", "parent_states"))
def test_prepare_batch_receives_advisory_target_path_and_fingerprint_only_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str
) -> None:
    content = "# Advisory target\n"
    required = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, required)
    _inject_statuses(required, {})

    _invoke_batch(
        tmp_path / "required",
        target_name="target.md",
        content=content,
        declaration=declaration,
    )

    kwargs = _prepare_call(required)
    rel_path = "Knowledge Base/Notes/Insights/target.md"
    after_hash = next(
        path.after_hash for path in kwargs["paths"] if path.rel_path == rel_path
    )
    assert after_hash == vault.content_hash(content)
    assert kwargs["advisory_target_rel_path"] == rel_path
    assert kwargs["advisory_target_fingerprint"] == after_hash
    assert kwargs["terminal_replay_until"] is not None
    assert (
        derived_receipts.DerivedComponent.WRITE_ADVISORY
        in set(kwargs["required_components"])
    )

    ungoverned = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, ungoverned)
    _inject_statuses(ungoverned, {})

    terminal, _target = _invoke_batch(
        tmp_path / "ungoverned",
        target_name="plain.md",
        content=content,
        declaration="none",
    )

    plain = _prepare_call(ungoverned)
    assert plain["advisory_target_rel_path"] is None
    assert plain["advisory_target_fingerprint"] is None
    assert (
        derived_receipts.DerivedComponent.WRITE_ADVISORY
        not in set(plain["required_components"])
    )
    assert terminal["advisory_sync"] == "not_required"
    assert "advisory_result_ref" not in terminal


def test_multi_note_batch_cannot_name_one_advisory_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {})

    terminal, _target = _invoke_batch(
        tmp_path,
        command_name="governed_batch",
        target_name="first.md",
        extra_target_names=("second.md",),
    )

    kwargs = _prepare_call(fake)
    assert len(kwargs["paths"]) == 2
    assert kwargs["advisory_target_rel_path"] is None
    assert kwargs["advisory_target_fingerprint"] is None
    assert terminal["advisory_sync"] == "not_required"
    assert "advisory_result_ref" not in terminal


def test_one_session_binds_exactly_one_advisory_result_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = DerivedReceiptProtocolFake()
    _install_protocol(monkeypatch, fake)
    _inject_statuses(fake, {})

    root = tmp_path / "vault"
    notes = root / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    content = "# Sequential\n"

    def leaf(vault_root: Path):
        for name in ("first.md", "second.md"):
            rel_path = f"Knowledge Base/Notes/Insights/{name}"
            state = semantic_index.build_parent_index_state(
                vault_root, rel_path, source=content
            )
            vault.batch_atomic_write(
                [vault.PlannedWrite(notes / name, content, create_only=True)],
                vault_root=vault_root,
                semantic_states={rel_path: state},
            )
        return {"path": "Knowledge Base/Notes/Insights/first.md", "warnings": []}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    command = SimpleNamespace(name="governed_batch", leaf=leaf, read_only=False)
    terminal = manager.invoke(
        command,
        (root,),
        {"response_detail": "compact"},
        mutation_request_id="11111111-1111-4111-8111-111111111111",
    )

    prepares = [call[2] for call in fake.calls if call[0] == "prepare_batch"]
    assert len(prepares) == 2
    assert prepares[0]["advisory_target_rel_path"] == (
        "Knowledge Base/Notes/Insights/first.md"
    )
    assert prepares[1]["advisory_target_rel_path"] is None
    assert prepares[1]["advisory_target_fingerprint"] is None
    assert terminal["advisory_result_ref"].startswith(
        "exomem://write-advisory-result/"
    )
