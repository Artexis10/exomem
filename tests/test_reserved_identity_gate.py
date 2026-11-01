"""A read never waits behind the reserved-identity gate, and a refusal names the lock that refused.

The gate orders owners that change identity against the exclusive scanner. Before this
change a reader of a free store queued behind an owner that was itself waiting on a busy
domain, and every refusal said "vault mutation boundary is busy".
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from exomem import embeddings, lexstore, writer_lease
from exomem.cli_ops import OpError
from exomem.writer_lease import LeaseConfig, LeaseManager

TIMEOUT_SECONDS = 4.0
PROMPT_SECONDS = 1.0


def _manager(tmp_path: Path, timeout: float = TIMEOUT_SECONDS) -> LeaseManager:
    return LeaseManager(
        LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=timeout,
        mutation_poll_interval_seconds=0.01,
    )


@contextlib.contextmanager
def _owner_thread(manager: LeaseManager, vault: Path, domain: str, *, advance: bool = True):
    """Enter the guard on its own thread; yield events for 'inside' and 'release'."""
    inside, release = threading.Event(), threading.Event()
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            with manager.reserved_identity_guard(
                vault, domains={domain}, exclusive=False, advance_generation=advance
            ):
                inside.set()
                release.wait(TIMEOUT_SECONDS * 2)
        except BaseException as error:  # noqa: BLE001 - handed to the test thread
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield inside, release
    finally:
        release.set()
        thread.join(TIMEOUT_SECONDS * 2)
    if outcome:
        raise outcome[0]


def _seconds_to_enter(manager: LeaseManager, vault: Path, domain: str, *, advance: bool) -> float:
    started = time.monotonic()
    with manager.reserved_identity_guard(
        vault, domains={domain}, exclusive=False, advance_generation=advance
    ):
        return time.monotonic() - started


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_a_read_of_a_free_store_does_not_queue_behind_an_owner_waiting_on_a_busy_domain(
    tmp_path: Path, vault: Path
) -> None:
    manager = _manager(tmp_path)
    with _owner_thread(manager, vault, "deferred-index-store") as (a_inside, a_release):
        assert a_inside.wait(PROMPT_SECONDS)
        with _owner_thread(manager, vault, "deferred-index-store") as (b_inside, _b_release):
            time.sleep(0.3)  # owner B is now blocked on A's domain
            assert not b_inside.is_set()

            waited = _seconds_to_enter(manager, vault, "embeddings-store", advance=False)

            assert waited < PROMPT_SECONDS
            a_release.set()
            assert b_inside.wait(PROMPT_SECONDS)


def test_a_writer_of_a_free_domain_does_not_queue_behind_an_owner_waiting_on_a_busy_one(
    tmp_path: Path, vault: Path
) -> None:
    manager = _manager(tmp_path)
    with _owner_thread(manager, vault, "deferred-index-store") as (a_inside, a_release):
        assert a_inside.wait(PROMPT_SECONDS)
        with _owner_thread(manager, vault, "deferred-index-store") as (b_inside, _b_release):
            time.sleep(0.3)
            assert not b_inside.is_set()

            waited = _seconds_to_enter(manager, vault, "claims-store", advance=True)

            assert waited < PROMPT_SECONDS
            a_release.set()
            assert b_inside.wait(PROMPT_SECONDS)


def test_the_exclusive_scanner_waits_for_a_domain_holder_and_then_runs(
    tmp_path: Path, vault: Path
) -> None:
    """The scanner still takes gate then every domain: it cannot run beside an open owner."""
    manager = _manager(tmp_path)
    scanned: list[str] = []
    with _owner_thread(manager, vault, "claims-store", advance=False) as (inside, release):
        assert inside.wait(PROMPT_SECONDS)

        def scan() -> None:
            with manager.reserved_identity_guard(
                vault, domains={"claims-store", "graph-store"}, exclusive=True
            ) as generation:
                scanned.append(generation)

        scanner = threading.Thread(target=scan, daemon=True)
        scanner.start()
        time.sleep(0.3)
        assert scanned == []  # the open reader holds a domain the scanner needs
        release.set()
        scanner.join(PROMPT_SECONDS * 2)

    assert len(scanned) == 1


def test_an_identity_changing_owner_is_admitted_while_readers_keep_taking_its_domain(
    tmp_path: Path, vault: Path
) -> None:
    """A stream of short readers of its domain must not starve an owner that changes identity."""
    manager = _manager(tmp_path, timeout=3.0)
    stop = threading.Event()

    def read_loop() -> None:
        while not stop.is_set():
            with manager.reserved_identity_guard(
                vault, domains={"claims-store"}, exclusive=False, advance_generation=False
            ):
                time.sleep(0.005)
            time.sleep(0.0005)

    readers = [threading.Thread(target=read_loop, daemon=True) for _ in range(4)]
    for reader in readers:
        reader.start()
    try:
        time.sleep(0.1)
        seconds = _seconds_to_enter(manager, vault, "claims-store", advance=True)
    finally:
        stop.set()
        for reader in readers:
            reader.join(TIMEOUT_SECONDS)

    assert seconds < 1.5


def test_an_owner_that_waits_past_its_deadline_is_refused_naming_its_domain(
    tmp_path: Path, vault: Path
) -> None:
    manager = _manager(tmp_path, timeout=0.3)
    with _owner_thread(manager, vault, "deferred-index-store") as (inside, _release):
        assert inside.wait(PROMPT_SECONDS)
        with pytest.raises(OpError) as refused:
            with manager.reserved_identity_guard(
                vault, domains={"deferred-index-store"}, exclusive=False
            ):
                pass

    assert refused.value.code == "MUTATION_BUSY"
    assert refused.value.details["coordinator"] == "identity_domain"
    assert refused.value.details["coordinator_name"] == "deferred-index-store"
    assert "boundary" not in refused.value.message


def test_a_refusal_at_the_vault_boundary_still_says_so(tmp_path: Path, vault: Path) -> None:
    manager = _manager(tmp_path, timeout=0.2)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with manager.mutation_guard(vault):
            held.set()
            release.wait(TIMEOUT_SECONDS)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert held.wait(PROMPT_SECONDS)
        with pytest.raises(OpError) as refused:
            with manager.mutation_guard(vault):
                pass
    finally:
        release.set()
        thread.join(TIMEOUT_SECONDS)

    assert refused.value.details["coordinator"] == "vault_boundary"
    assert refused.value.message == "vault mutation boundary is busy"


def test_a_search_connect_to_an_existing_store_does_not_advance_the_identity_generation(
    tmp_path: Path,
) -> None:
    state_dir = writer_lease.active_manager().config.state_dir

    def generation(root: Path) -> str:
        return writer_lease._read_reserved_identity_generation(state_dir, root)

    lexical_root = tmp_path / "lexical"
    page = lexical_root / "Knowledge Base" / "alpha.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: insight\ntitle: alpha\nupdated: 2026-01-01\n---\n# alpha\n\nbody\n")
    lexstore.reset_memo()
    lexstore.clear_stores()
    assert lexstore.search_bm25(lexical_root, "body", k=3, scope="kb") is not None
    before = generation(lexical_root)
    lexstore.clear_stores()
    assert lexstore.search_bm25(lexical_root, "body", k=3, scope="kb") is not None
    assert generation(lexical_root) == before

    embedding_root = tmp_path / "embedding"
    (embedding_root / "Knowledge Base").mkdir(parents=True)
    embeddings.clear_embedding_indexes()
    seed = embeddings.EmbeddingIndex(embedding_root)
    vector = np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32)
    seed.upsert_file("a.md", ["a"], vector, 1.0)
    before = generation(embedding_root)
    embeddings.EmbeddingIndex(embedding_root)._connect().close()
    assert generation(embedding_root) == before


def test_a_narrow_command_journals_its_own_vault_boundary_hold(tmp_path: Path, monkeypatch) -> None:
    """A later short hold of another lock must not replace the boundary hold in the row."""
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()

    def leaf(_vault: Path) -> str:
        with manager.mutation_guard(vault):
            time.sleep(0.3)
        with manager.reserved_identity_guard(vault, domains={"review-state"}, exclusive=False):
            pass
        return "ok"

    command = SimpleNamespace(name="edit_memory", read_only=False, leaf=leaf)
    assert manager.invoke(command, (vault,), {}, mutation_request_id="req-narrow") == "ok"

    lines = (tmp_path / "logs" / "mutations.jsonl").read_text(encoding="utf-8").splitlines()
    (record,) = [json.loads(line) for line in lines]
    assert record["boundary_hold_ms"] >= 300
