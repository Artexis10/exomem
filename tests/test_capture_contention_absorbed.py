"""`capture-survives-contention` part 2: an idempotent capture write waits out
an ordinary busy mutation boundary on the server.

`MUTATION_BUSY` used to reach the client whenever another write held the
boundary past the short acquire budget, so a valid `observe_memory`, `remember`,
`episode_memory` or `record_memory` append was reported as not committed and the
agent told the user so. The wait now happens here, in arrival order and bounded;
a refusal survives only for a holder past its allowance or a wait that ran out,
and it carries a cause the caller can act on.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture

from exomem import commands, mutation_lock, record_formats, writer_lease
from exomem import schema as schema_module
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.commands import op_remember
from exomem.governance.principal import owner_principal, request_scope

_OBSERVE_SECONDS = 15.0
_HOLD_SECONDS = 30.0
WRITERS = 5


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def _command(name: str):
    return next(c for c in commands.product_commands_for("mcp") if c.name == name)


def _managers(state_dir: Path) -> tuple[writer_lease.LeaseManager, writer_lease.LeaseManager]:
    """A writer with a short acquire budget, and a separate holder of the boundary."""
    config = writer_lease.LeaseConfig(state_dir=state_dir)
    return (
        writer_lease.LeaseManager(config, mutation_timeout_seconds=0.05),
        writer_lease.LeaseManager(config),
    )


class _Contention:
    """Hold the boundary until every writer has been refused at least once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, holder, vault: Path, *, refusals: int):
        self.refusals = refusals
        self.seen = 0
        self._lock = threading.Lock()
        self.saw_contention = threading.Event()
        self.release = threading.Event()
        self.holding = threading.Event()
        real = mutation_lock.VaultMutationCoordinator._refused
        contention = self

        def counted(coordinator, wait_start):
            with contention._lock:
                contention.seen += 1
                if contention.seen >= contention.refusals:
                    contention.saw_contention.set()
            return real(coordinator, wait_start)

        monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "_refused", counted)

        def hold() -> None:
            with holder.mutation_guard(vault, request_id="holder", operation="semantic edit"):
                self.holding.set()
                assert self.release.wait(_HOLD_SECONDS)

        self.thread = threading.Thread(target=hold)

    def __enter__(self) -> _Contention:
        self.thread.start()
        assert self.holding.wait(_OBSERVE_SECONDS)
        return self

    def __exit__(self, *exc) -> None:
        self.release.set()
        self.thread.join(timeout=_HOLD_SECONDS)


def _remember_kwargs(vault: Path, number: int) -> dict:
    kwargs = {
        "content": (
            f"# Held-boundary save {number}\n\n## Observations\n\n"
            "- [operating constraint] Keep capture off the client #reliability\n"
        ),
        "title": f"Held-boundary save {number}",
        "slug": f"held-boundary-save-{number}",
        "suggestions": False,
    }
    validation = op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )
    return kwargs


def _run_concurrently(writers, contention: _Contention) -> list:
    """Start every writer, release the boundary once each has met it, collect results."""
    with ThreadPoolExecutor(max_workers=len(writers)) as pool:
        futures = [pool.submit(write) for write in writers]
        assert contention.saw_contention.wait(_OBSERVE_SECONDS)
        contention.release.set()
        return [future.result(timeout=_OBSERVE_SECONDS * 2) for future in futures]


def test_concurrent_remembers_all_commit_once_with_no_busy_reaching_the_client(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, holder = _managers(vault.parent / "state")
    remember = _command("remember")
    kwargs = [_remember_kwargs(vault, number) for number in range(WRITERS)]
    with _Contention(monkeypatch, holder, vault, refusals=WRITERS) as contention:
        results = _run_concurrently(
            [
                lambda kw=kw: writer.invoke(
                    remember, (vault,), kw, implicit_idempotency_scope="principal:test"
                )
                for kw in kwargs
            ],
            contention,
        )

    paths = [result["path"] for result in results]
    assert len(set(paths)) == WRITERS
    assert all((vault / path).is_file() for path in paths)


def test_concurrent_episode_records_all_commit_once_with_no_busy_reaching_the_client(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, holder = _managers(vault.parent / "state")
    episode = _command("episode_memory")
    schema = schema_module.load_source_schema(vault)

    def record(number: int):
        with request_scope(owner_principal(surface="mcp")):
            return writer.invoke(
                episode,
                (vault, schema),
                {
                    "action": "record",
                    "episode": "ep-" + f"{number:02x}" * 16,
                    "subject": f"Harbor Lamp purchase {number}",
                    "summary": "Chose the brass lamp; delivery date still open.",
                    "decided": ["Buy the brass Harbor Lamp"],
                },
                implicit_idempotency_scope="principal:test",
            )

    with _Contention(monkeypatch, holder, vault, refusals=WRITERS) as contention:
        results = _run_concurrently(
            [lambda number=number: record(number) for number in range(WRITERS)], contention
        )

    paths = [result["source"]["path"] for result in results]
    assert len(set(paths)) == WRITERS
    assert all((vault / path).is_file() for path in paths)
    folder = vault / "Knowledge Base" / "Sources" / "Episodes"
    assert len(list(folder.glob("*.md"))) == WRITERS


def test_a_retried_keyed_capture_is_not_committed_twice_when_it_waited(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, holder = _managers(vault.parent / "state")
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 0)
    with _Contention(monkeypatch, holder, vault, refusals=1) as contention:
        (first,) = _run_concurrently(
            [
                lambda: writer.invoke(
                    remember, (vault,), kwargs, idempotency_key="capture-once-key"
                )
            ],
            contention,
        )
    replay = writer.invoke(remember, (vault,), kwargs, idempotency_key="capture-once-key")

    assert replay["path"] == first["path"]
    assert len(list((vault / "Knowledge Base").rglob("held-boundary-save-0*.md"))) == 1


def test_a_holder_past_its_allowance_is_refused_at_once_with_an_actionable_cause(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, holder = _managers(vault.parent / "state")
    real_refused = mutation_lock.VaultMutationCoordinator._refused

    def refused_as_overdue(coordinator, wait_start):
        error = real_refused(coordinator, wait_start)
        error.details["holder"] = {"operation": "semantic edit", "overdue": True}
        return error

    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "_refused", refused_as_overdue)
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 0)
    with _Contention(monkeypatch, holder, vault, refusals=10**6) as contention:
        with pytest.raises(OpError) as raised:
            writer.invoke(remember, (vault,), kwargs, implicit_idempotency_scope="principal:test")
        contention.release.set()

    error = raised.value
    assert error.code == "MUTATION_BUSY"
    assert error.details["cause"] == "holder_overdue"
    assert error.details["committed"] is False
    assert "idempotency key" in error.remediation
    assert not list((vault / "Knowledge Base").rglob("held-boundary-save-0*.md"))


def test_an_exhausted_wait_is_refused_with_its_own_cause(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.3")
    writer, holder = _managers(vault.parent / "state")
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 0)
    with _Contention(monkeypatch, holder, vault, refusals=10**6) as contention:
        with pytest.raises(OpError) as raised:
            writer.invoke(remember, (vault,), kwargs, implicit_idempotency_scope="principal:test")
        contention.release.set()

    assert raised.value.code == "MUTATION_BUSY"
    assert raised.value.details["cause"] == "capture_wait_exhausted"
    assert raised.value.details["committed"] is False


def test_only_idempotent_capture_writes_wait(vault: Path) -> None:
    absorbs = writer_lease._capture_write_absorbs_contention
    for name in ("observe_memory", "remember", "episode_memory"):
        assert absorbs(name, {})
    assert absorbs("record_memory", {"action": "append"})
    assert not absorbs("record_memory", {"action": "update"})
    assert not absorbs("edit_memory", {})
    assert not absorbs("manage_memory_file", {"operation": "move"})


def test_a_record_append_waits_out_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    log = tmp_path / "Knowledge Base/log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    sample = parsed.records[0]
    writer, holder = _managers(tmp_path / "state")
    command = _command("record_memory")
    item = dict(sample.values)
    item.update(occurred_on="2031-01-02", provider="Invented Garage Two")
    with _Contention(monkeypatch, holder, tmp_path, refusals=1) as contention:
        (result,) = _run_concurrently(
            [
                lambda: writer.invoke(
                    command,
                    (tmp_path,),
                    {
                        "action": "append",
                        "collection": (fixture / "_collection.md").relative_to(tmp_path).as_posix(),
                        "item": item,
                        "item_key": "22222222-2222-4222-8222-222222222222",
                        "expected_container_hash": parsed.snapshot,
                        "why": "record a service visit",
                    },
                    implicit_idempotency_scope="principal:test",
                )
            ],
            contention,
        )
    assert result
