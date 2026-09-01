"""Bounded corpus-context flight joins and their pre-commit refusal contract.

Measured on a 3,006-page vault over 61 complete mutation traces, the median
mutation spent 98% of its wall time after canonical commit: 103.6 seconds at
the median, versus a roughly 150 ms durable write.  These guards keep an
interactive waiter on a fixed 2.0-second budget without weakening the corpus
context required for semantic validation.
"""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import (
    freshness,
    relation_registry,
    relation_review,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
)
from exomem.cli_ops import OpError, error_dict
from exomem.writer_lease import LeaseConfig, LeaseManager

_PAGE = "Knowledge Base/Notes/Insights/one.md"
_JOIN_SECONDS = 2.0


def _page(*, title: str = "One", exomem_id: str | None = None) -> str:
    identity = f"exomem_id: {exomem_id}\n" if exomem_id is not None else ""
    return (
        "---\n"
        f"{identity}"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "project: atlas\n"
        "---\n\n"
        "## Observations\n"
        "- [finding] The bounded corpus context is required before mutation. #reliability\n"
    )


@pytest.fixture(autouse=True)
def _clean_corpus_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
        semantic_contract._CORPUS_CONTEXT_FLIGHTS.clear()
    yield
    with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
        semantic_contract._CORPUS_CONTEXT_FLIGHTS.clear()
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    page = tmp_path / _PAGE
    page.parent.mkdir(parents=True)
    page.write_text(_page(), encoding="utf-8")
    return tmp_path


class _ControlledDone:
    def __init__(
        self,
        *,
        settled: bool = False,
        wait_result: bool = False,
        fail_on_wait: bool = False,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        self.settled = settled
        self.wait_result = wait_result
        self.fail_on_wait = fail_on_wait
        self.on_wait = on_wait
        self.wait_calls: list[float | None] = []
        self.set_calls = 0

    def is_set(self) -> bool:
        return self.settled

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        if self.fail_on_wait:
            pytest.fail("an already-settled flight invoked the timed wait")
        if self.on_wait is not None:
            self.on_wait()
        return self.wait_result

    def set(self) -> None:
        self.set_calls += 1
        self.settled = True


class _RecordingEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        return super().wait(timeout)


def _flight_inputs(vault: Path) -> tuple[Any, Any, tuple, tuple[str, str]]:
    registry = relation_registry.load_registry(vault)
    language = semantic_language_registry.load_registry(vault)
    census = semantic_contract._corpus_census(vault)
    assert census is not None
    return registry, language, census, semantic_contract._corpus_cache_key(vault)


def _register_flight(
    vault: Path,
    done: _ControlledDone | threading.Event,
    *,
    result: semantic_contract.SemanticCorpusContext | None = None,
    error: BaseException | None = None,
    registry_identity: tuple[Any, ...] | None = None,
) -> tuple[semantic_contract._CorpusContextFlight, Any, Any, tuple[str, str]]:
    registry, language, census, cache_key = _flight_inputs(vault)
    flight = semantic_contract._CorpusContextFlight(
        census,
        registry_identity
        or (registry.core_version, registry.extension_hash),
        (language.schema_version, language.content_hash),
        done=done,  # type: ignore[arg-type] - deterministic Event contract fake
        result=result,
        error=error,
    )
    with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
        semantic_contract._CORPUS_CONTEXT_FLIGHTS[cache_key] = flight
    return flight, registry, language, cache_key


def _uncached_context(vault: Path) -> semantic_contract.SemanticCorpusContext:
    registry = relation_registry.load_registry(vault)
    language = semantic_language_registry.load_registry(vault)
    return semantic_contract._build_corpus_context_uncached(
        vault,
        candidate=None,
        relation_definitions=registry,
        language=language,
    )


def _invoke_joining_mutation(vault: Path, state_dir: Path, *, request_id: str) -> None:
    def leaf(root: Path) -> None:
        semantic_contract.build_corpus_context_with_census(root)
        target = root / _PAGE
        target.write_bytes(target.read_bytes() + b"\ncanonical mutation continued\n")

    command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
    manager = LeaseManager(LeaseConfig(state_dir=state_dir))
    manager.invoke(command, (vault,), {}, mutation_request_id=request_id)


def test_slow_corpus_flight_returns_mutation_warming_before_commit(vault: Path) -> None:
    done = _RecordingEvent()
    _register_flight(vault, done)
    owner_started = threading.Event()
    release_owner = threading.Event()
    owner_completed = threading.Event()

    def slow_owner() -> None:
        owner_started.set()
        release_owner.wait()
        done.set()
        owner_completed.set()

    owner = threading.Thread(target=slow_owner, name="slow-corpus-owner")
    owner.start()
    target = vault / _PAGE
    before = target.read_bytes()

    try:
        assert owner_started.wait(timeout=1.0)
        started = time.monotonic()
        with pytest.raises(Exception) as raised:
            _invoke_joining_mutation(vault, vault / ".state", request_id="slow-corpus")
        elapsed = time.monotonic() - started

        assert getattr(raised.value, "code", None) == "MUTATION_WARMING"
        assert _JOIN_SECONDS <= elapsed < _JOIN_SECONDS + 0.75
        assert not owner_completed.is_set()
        assert target.read_bytes() == before
        assert done.wait_calls == [_JOIN_SECONDS]
    finally:
        release_owner.set()
        owner.join(timeout=1.0)

    assert not owner.is_alive()
    assert owner_completed.is_set()


def test_already_settled_corpus_flight_never_invokes_timed_wait(vault: Path) -> None:
    expected = _uncached_context(vault)
    done = _ControlledDone(settled=True, wait_result=True, fail_on_wait=True)
    _register_flight(vault, done, result=expected)

    actual, census = semantic_contract.build_corpus_context_with_census(vault)

    assert actual is expected
    assert census is None
    assert done.wait_calls == []


def test_settled_or_absent_corpus_flight_never_waits(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _uncached_context(vault)
    settled_done = _ControlledDone(settled=True, wait_result=True, fail_on_wait=True)
    _flight, _registry, _language, cache_key = _register_flight(
        vault,
        settled_done,
        result=expected,
    )

    settled, settled_census = semantic_contract.build_corpus_context_with_census(vault)

    assert settled is expected
    assert settled_census is None
    assert settled_done.wait_calls == []

    with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
        semantic_contract._CORPUS_CONTEXT_FLIGHTS.pop(cache_key)
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_FLIGHTS

    real_flight_type = semantic_contract._CorpusContextFlight
    owner_done_events: list[_ControlledDone] = []

    def guarded_owner_flight(*args: Any, **kwargs: Any) -> Any:
        done = _ControlledDone(fail_on_wait=True)
        owner_done_events.append(done)
        return real_flight_type(*args, **kwargs, done=done)

    monkeypatch.setattr(semantic_contract, "_CorpusContextFlight", guarded_owner_flight)

    owned, _owned_census = semantic_contract.build_corpus_context_with_census(vault)

    assert owned.pages
    assert len(owner_done_events) == 1
    assert owner_done_events[0].wait_calls == []
    assert owner_done_events[0].set_calls == 1


def test_corpus_flight_owner_error_is_not_laundered(vault: Path) -> None:
    done = threading.Event()
    owner_error = RuntimeError("owner corpus build failed")
    _register_flight(vault, done, error=owner_error)
    done.set()

    with pytest.raises(RuntimeError, match="owner corpus build failed") as raised:
        semantic_contract.build_corpus_context_with_census(vault)

    assert raised.value is owner_error


def test_expired_corpus_waiter_does_not_disturb_owner(vault: Path) -> None:
    done = _ControlledDone(wait_result=False)
    flight, _registry, _language, cache_key = _register_flight(vault, done)

    with pytest.raises(Exception) as raised:
        _invoke_joining_mutation(vault, vault / ".state", request_id="expired-corpus")

    assert getattr(raised.value, "code", None) == "MUTATION_WARMING"
    with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
        assert semantic_contract._CORPUS_CONTEXT_FLIGHTS[cache_key] is flight
    assert flight.result is None
    assert flight.error is None
    assert done.set_calls == 0


def test_changed_inputs_recompute_after_bounded_join(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_key = semantic_contract._corpus_cache_key(vault)

    def release_completed_flight() -> None:
        with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
            semantic_contract._CORPUS_CONTEXT_FLIGHTS.pop(cache_key, None)

    done = _ControlledDone(wait_result=True, on_wait=release_completed_flight)
    _register_flight(vault, done, registry_identity=("stale", "registry"))
    real_build = semantic_contract._build_corpus_context_uncached
    build_calls: list[str] = []

    def counted_build(*args: Any, **kwargs: Any) -> semantic_contract.SemanticCorpusContext:
        build_calls.append("build")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", counted_build)

    context, _census = semantic_contract.build_corpus_context_with_census(vault)

    assert context.registry.extension_hash == relation_registry.load_registry(vault).extension_hash
    assert build_calls == ["build"]
    assert done.wait_calls == [_JOIN_SECONDS]


def test_default_warming_projection_is_retryable_and_uncommitted(vault: Path) -> None:
    done = _ControlledDone(wait_result=False)
    _register_flight(vault, done)

    with pytest.raises(Exception) as raised:
        _invoke_joining_mutation(vault, vault / ".state", request_id="warming-projection")

    payload = error_dict(raised.value)
    assert payload["code"] == "MUTATION_WARMING"
    assert payload["status"] == "retryable"
    assert payload["committed"] is False
    assert payload["retry_after_ms"] == 2000
    assert payload["request_id"] == "warming-projection"
    assert "corpus_context_sync" not in payload


@pytest.mark.parametrize(
    "consumer",
    ["semantic_contract", "semantic_writes_existing", "semantic_writes_creation", "relation_review"],
)
def test_typed_warming_propagates_through_every_corpus_consumer(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    warming = OpError(
        "MUTATION_WARMING",
        "semantic corpus context is still being built",
        details={"status": "retryable", "committed": False, "retry_after_ms": 2000},
    )

    def refuse(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        raise warming

    monkeypatch.setattr(semantic_contract, "build_corpus_context_with_census", refuse)
    draft_id = "11111111-1111-4111-8111-111111111111"
    calls: dict[str, Callable[[], Any]] = {
        "semantic_contract": lambda: semantic_contract.build_corpus_context(vault),
        "semantic_writes_existing": lambda: semantic_writes.preflight_existing(
            vault,
            path=_PAGE,
            after_source=_page(title="After"),
            operation="edit",
        ),
        "semantic_writes_creation": lambda: semantic_writes._evaluate_structural(
            vault,
            destination="Knowledge Base/Notes/Insights/new.md",
            source=_page(title="New", exomem_id=draft_id),
            operation="create",
        ),
        "relation_review": lambda: relation_review.validate_creation_draft(
            vault,
            path="Knowledge Base/Notes/Insights/new.md",
            source=_page(title="New", exomem_id=draft_id),
            draft_id=draft_id,
            operation="create",
        ),
    }

    with pytest.raises(OpError) as raised:
        calls[consumer]()

    assert raised.value.code == "MUTATION_WARMING"
    assert raised.value.details == warming.details


class _UnboundedJoinVisitor(ast.NodeVisitor):
    def __init__(self, module: Path) -> None:
        self.module = module
        self.scope: list[str] = []
        self.found: set[tuple[str, str, str]] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"wait", "join"}
            and not node.args
            and not any(keyword.arg == "timeout" for keyword in node.keywords)
        ):
            self.found.add(
                (
                    self.module.as_posix(),
                    ".".join(self.scope),
                    ast.unparse(node.func),
                )
            )
        self.generic_visit(node)


# Exact background/process-lifecycle joins.  These declarations are deliberately
# test-local because the worker allowlist excludes the unrelated implementations.
_DECLARED_UNBOUNDED_JOINS = {
    (
        "file_watcher.py",
        "FileWatcher._run_dispatch",
        "self._wake.wait",
    ): "daemon debounce loop; stop() sets the event and request handlers never join it",
    (
        "media_worker.py",
        "MediaWorker.join",
        "self._q.join",
    ): "explicit test/operator queue drain when timeout=None; request paths do not call it",
    (
        "governance/receipts.py",
        "_prepare_receipt_connections_for_fork",
        "_RECEIPT_CONNECTIONS_CONDITION.wait",
    ): "process-fork quiescence handshake, not a request flight or worker join",
    (
        "governance/receipts.py",
        "_receipt_connection",
        "_RECEIPT_CONNECTIONS_CONDITION.wait",
    ): "process-fork resume handshake, not a request flight or worker join",
}


def test_request_path_blocking_joins_are_bounded_or_declared() -> None:
    source_root = Path(semantic_contract.__file__).parent
    found: set[tuple[str, str, str]] = set()
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        visitor = _UnboundedJoinVisitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.update(visitor.found)

    assert found == set(_DECLARED_UNBOUNDED_JOINS), (
        "an unbounded wait/join appeared or moved: give request-reachable work a fixed "
        "budget, or declare the exact background/process-lifecycle reason"
    )
