"""The out-of-process index drain refuses contended graph ownership (1.5 / 2.3).

Measured in the 2026-08 incident: an `exomem index` drain and the live service
contended for the same graph claim. The CLI held it at 0 CPU while the service
kept minting receipts behind it -- ~2,143 receipts in 40 minutes, of which
draining the backlog re-embedded exactly 3 files. The drain has to detect that
a live service currently performs graph work for the target vault BEFORE taking
any graph claim, and refuse with the stop-window remediation instead of
contending for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from exomem import graph_sync, mutation_lock
from exomem.writer_lease import active_manager


def _coordinator(vault: Path) -> mutation_lock.VaultMutationCoordinator:
    return mutation_lock.VaultMutationCoordinator(active_manager().config.state_dir, vault)


def test_no_holder_means_no_live_graph_owner(vault: Path) -> None:
    """Counter-scenario: an uncontended vault drains normally."""
    assert graph_sync.live_graph_owner(vault) is None


def test_a_live_graph_holder_is_detected(vault: Path) -> None:
    """A service holding the boundary for GRAPH work is the refusal condition."""
    held = threading.Event()
    release = threading.Event()
    observed: list[object] = []

    def service() -> None:
        with _coordinator(vault).hold(
            operation="epistemic_graph_rebuild",
            holder_kind="graph",
        ):
            held.set()
            release.wait(timeout=30)

    worker = threading.Thread(target=service, daemon=True)
    worker.start()
    try:
        assert held.wait(timeout=30)
        observed.append(graph_sync.live_graph_owner(vault))
    finally:
        release.set()
        worker.join(timeout=30)

    owner = observed[0]
    assert owner is not None, "a live graph holder was not detected"
    assert owner["holder_kind"] == "graph"


def test_a_non_graph_holder_is_not_a_graph_owner(vault: Path) -> None:
    """Counter-scenario: ordinary write traffic is not graph contention.

    Refusing on any holder would make the drain unusable on a busy vault.
    """
    held = threading.Event()
    release = threading.Event()
    observed: list[object] = []

    def writer() -> None:
        with _coordinator(vault).hold(
            operation="write_note",
            holder_kind="write",
        ):
            held.set()
            release.wait(timeout=30)

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    try:
        assert held.wait(timeout=30)
        observed.append(graph_sync.live_graph_owner(vault))
    finally:
        release.set()
        worker.join(timeout=30)

    assert observed[0] is None


def test_the_cli_drain_refuses_and_names_the_stop_window(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario: the drain exits without taking the claim, naming the remedy."""
    from exomem import __main__ as main_module

    monkeypatch.setattr(
        graph_sync,
        "live_graph_owner",
        lambda _root: {
            "state": "held",
            "holder_kind": "graph",
            "operation": "epistemic_graph_rebuild",
            "age_seconds": 12.0,
        },
    )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the drain took a graph claim despite a live owner")

    monkeypatch.setattr(graph_sync, "claim_rebuild_owner", forbidden)

    code = main_module._index_main(["--vault", str(vault), "--dry-run"])

    assert code != 0
    message = capsys.readouterr().err
    assert "stop window" in message.lower()
    assert "graph" in message.lower()


def test_the_cli_drain_proceeds_without_a_live_graph_owner(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-scenario: no live graph owner means the drain is not blocked."""
    from exomem import __main__ as main_module

    monkeypatch.setattr(graph_sync, "live_graph_owner", lambda _root: None)

    code = main_module._index_main(["--vault", str(vault), "--dry-run"])

    assert code == 0


def test_state_is_authoritative_over_a_stale_holder_kind(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`state` decides, not the leftover holder fields.

    A free boundary happens to carry no `holder_kind` today, so the kind check
    alone would pass by accident. Pin the intended precedence directly: if the
    boundary is not held, nothing owns graph work, whatever else the payload
    still says.
    """
    monkeypatch.setattr(
        mutation_lock.VaultMutationCoordinator,
        "snapshot",
        lambda _self: {"state": "free", "holder_kind": "graph", "operation": "stale"},
    )

    assert graph_sync.live_graph_owner(vault) is None
