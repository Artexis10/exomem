from __future__ import annotations

import io
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem import commands, preserve, schema
from exomem.cli_ops import OpError
from exomem.writer_lease import LeaseConfig, LeaseManager

#: The wall-clock shape of every contention test in this file.
#:
#: A HOLD parks a thread or process while the test observes an ordering; an
#: OBSERVATION is how long the test waits for that state to be reached. The gap
#: between them is the entire discriminating power of these tests -- a hold that
#: does not outlast its observation lets the ordering pass vacuously, and an
#: observation sized for an idle laptop fails on a loaded shard while the code
#: under test behaves correctly.
#:
#: A NEGATIVE wait (`assert not x.wait(0.1)`) proves something has NOT happened
#: yet and stays tight: widening one changes the scenario rather than merely
#: slowing it, because the product's own timeouts run in the same window.
#:
#: `join(timeout=N)` followed by `assert t.is_alive()` is the SAME negative
#: observation in join form, and it is the one shape that consumes its whole
#: window on every healthy run -- it exists to prove a competitor is still
#: parked. Widening one from 0.3s to 60s bought nothing and cost a minute a run.
#:
#: Both constants stay strictly under pytest's per-test `timeout` (pyproject
#: `[tool.pytest.ini_options]`). A valve at or above it never gets to fire: the
#: harness kills the test first and you get a thread dump where a named
#: assertion should have been. tests/test_timing_assertion_hygiene.py pins that.
#:
#: These are not latency claims. Nothing here asserts the product is fast.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 15.0
_BARRIER_SECONDS = 2.0


def _add_command():
    return next(command for command in commands.COMMANDS if command.name == "add")


def test_twenty_concurrent_real_captures_leave_complete_vault_state(
    vault: Path, source_schema
) -> None:
    assert os.environ["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    state_dir = vault.parent / "mutation-state"
    manager = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.05)
    retry_manager = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.05)
    assert retry_manager._mutation_timeout_seconds == 0.05
    holder = LeaseManager(LeaseConfig(state_dir=state_dir))
    command = _add_command()
    start = threading.Barrier(20)
    holding = threading.Event()
    release = threading.Event()

    def arguments(number: int) -> dict[str, str]:
        slug = f"concurrent-capture-{number:02d}"
        return {
            "content": f"bounded concurrent payload {number}",
            "source_type": "other",
            "title": f"Concurrent Capture {number:02d}",
            "slug": slug,
        }

    def capture(number: int, *, synchronize: bool, retry: bool = False) -> dict:
        if synchronize:
            start.wait(timeout=_OBSERVE_SECONDS)
        return (retry_manager if retry else manager).invoke(
            command,
            (vault, source_schema),
            arguments(number),
        )

    def hold_boundary() -> None:
        with holder.mutation_guard(vault, request_id="holder", operation="capture"):
            holding.set()
            assert release.wait(_HOLD_SECONDS)

    thread = threading.Thread(target=hold_boundary)
    thread.start()
    assert holding.wait(_OBSERVE_SECONDS)
    with ThreadPoolExecutor(max_workers=20) as pool:
        first_attempts = [pool.submit(capture, number, synchronize=True) for number in range(20)]
        refused: list[OpError] = []
        for future in first_attempts:
            with pytest.raises(OpError) as raised:
                future.result(timeout=10.0)
            refused.append(raised.value)
        assert len(refused) == 20
        assert all(error.code == "MUTATION_BUSY" for error in refused)
        assert all(error.details.get("committed") is False for error in refused)
        assert not list((vault / "Knowledge Base/Sources/Other").glob("concurrent-capture-*.md"))

        release.set()
        thread.join(timeout=_HOLD_SECONDS)
        assert not thread.is_alive()
        pending = set(range(20))
        results: dict[int, dict] = {}
        deadline = time.monotonic() + 45.0
        while pending:
            assert time.monotonic() < deadline, "concurrent retry waves did not make progress"
            with ThreadPoolExecutor(max_workers=20) as retry_pool:
                futures = {
                    number: retry_pool.submit(capture, number, synchronize=False, retry=True)
                    for number in pending
                }
                for number, future in futures.items():
                    try:
                        remaining = deadline - time.monotonic()
                        assert remaining > 0, "concurrent retry waves did not make progress"
                        results[number] = future.result(timeout=remaining)
                    except OpError as error:
                        assert error.code == "MUTATION_BUSY"
                        assert error.details.get("committed") is False
            pending.difference_update(results)

    paths = [result["path"] for result in results.values()]
    assert len(paths) == len(set(paths)) == 20
    sources_index = (vault / "Knowledge Base/Sources/index.md").read_text(encoding="utf-8")
    top_index = (vault / "Knowledge Base/index.md").read_text(encoding="utf-8")
    activity_log = (vault / "Knowledge Base/log.md").read_text(encoding="utf-8")
    for number in range(20):
        slug = f"concurrent-capture-{number:02d}"
        assert slug in sources_index
        assert slug in top_index
        assert slug in activity_log
    assert "|Other]] — miscellaneous captures (20)" in sources_index
    assert "- Sources: 24 " in top_index
    assert len(list((vault / "Knowledge Base/Sources/Other").glob("*.md"))) == 20

    residue = [
        path for path in vault.rglob("*") if path.is_file() and path.name.endswith((".tmp", ".bak"))
    ]
    assert residue == []


class _BarrierStream(io.BytesIO):
    def __init__(self, data: bytes, barrier: threading.Barrier):
        super().__init__(data)
        self._barrier = barrier
        self._first_read = True

    def read(self, size: int = -1) -> bytes:
        if self._first_read:
            self._first_read = False
            self._barrier.wait(timeout=_BARRIER_SECONDS)
        return super().read(size)


def test_independent_vault_real_uploads_commit_concurrently(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    shutil.copytree(fixture, vault_a)
    shutil.copytree(fixture, vault_b)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "mutation-state"))
    start = threading.Barrier(2)

    def upload(vault: Path, filename: str) -> str:
        with manager.mutation_guard(vault):
            result = preserve.preserve_stream(
                vault,
                scope="Concurrent",
                category="Uploads",
                filename=filename,
                stream=_BarrierStream(filename.encode(), start),
            )
        return result.path

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(upload, vault_a, "alpha.bin")
        second = pool.submit(upload, vault_b, "beta.bin")
        assert first.result(timeout=5.0).endswith("alpha.bin")
        assert second.result(timeout=5.0).endswith("beta.bin")

    assert (vault_a / "Knowledge Base/Evidence/Concurrent/Uploads/alpha.bin").exists()
    assert (vault_b / "Knowledge Base/Evidence/Concurrent/Uploads/beta.bin").exists()
    assert not (vault_a / "Knowledge Base/Evidence/Concurrent/Uploads/beta.bin").exists()
    assert not (vault_b / "Knowledge Base/Evidence/Concurrent/Uploads/alpha.bin").exists()
    assert schema.load_source_schema(vault_a)
    assert schema.load_source_schema(vault_b)
