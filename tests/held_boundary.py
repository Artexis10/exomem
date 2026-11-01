"""Another writer holds the vault mutation boundary while the code under test waits for it."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from exomem import mutation_lock, writer_lease


@dataclass
class HeldBoundary:
    """`refused` is set when the code under test is first refused; setting `release` lets go."""

    refused: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


@contextmanager
def held_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch, *, release_when_refused: bool = True
) -> Iterator[HeldBoundary]:
    """Hold the boundary from another writer until the code under test has been refused once.

    The acquire timeout shrinks to a tenth of a second, so a refusal comes quickly and the code
    under test must wait the holder out to succeed. With `release_when_refused` false the holder
    keeps the boundary until the test sets `release`, for a test that needs the wait to run out.
    Enter before the first `writer_lease.get_manager()` of the test: a manager reads its timeout
    from the environment when it is built.
    """
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "0.1")
    held = HeldBoundary()
    holding = threading.Event()
    real = mutation_lock.VaultMutationCoordinator._refused

    def noting(coordinator, wait_start):
        held.refused.set()
        if release_when_refused:
            held.release.set()
        return real(coordinator, wait_start)

    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "_refused", noting)
    holder = writer_lease.LeaseManager(writer_lease.LeaseConfig.from_env())

    def hold() -> None:
        with holder.mutation_guard(vault, request_id="holder", operation="semantic edit"):
            holding.set()
            # The timeout keeps a lost wake-up from hanging the suite.
            held.release.wait(60)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(30)
    try:
        yield held
        assert held.refused.is_set(), "the code under test never met the held boundary"
    finally:
        held.release.set()
        thread.join(30)
