"""A completed receipt expires by the window it was stored with, not by a later caller's window."""

from __future__ import annotations

from pathlib import Path

from exomem.writer_lease import IdempotencyStore

_HOUR = 3600.0
_IMPLICIT_WINDOW = 600.0
_EXPLICIT_WINDOW = 24 * _HOUR


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value


def _store(tmp_path: Path, clock: _Clock) -> IdempotencyStore:
    return IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", clock=clock)


def test_a_short_window_claim_does_not_delete_another_keys_longer_receipt(tmp_path: Path) -> None:
    # Probe B: a 24 h receipt under an `implicit:` key was deleted once any other implicit claim
    # passed its own 600 s window, so a completed write re-ran on its retry.
    clock = _Clock()
    store = _store(tmp_path, clock)
    runs: list[str] = []

    def leaf(name: str):
        def run() -> dict:
            runs.append(name)
            return {"ran": name}

        return run

    store.run("implicit:alice:a", "digest-a", leaf("a"), expires_after=_EXPLICIT_WINDOW)
    clock.value += _IMPLICIT_WINDOW + 1
    store.run("implicit:alice:b", "digest-b", leaf("b"), expires_after=_IMPLICIT_WINDOW)

    replayed = store.run("implicit:alice:a", "digest-a", leaf("a-again"), expires_after=_EXPLICIT_WINDOW)

    assert replayed == {"ran": "a"}
    assert runs == ["a", "b"]


def test_a_receipt_expires_after_its_own_window(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    runs: list[int] = []

    def leaf() -> dict:
        runs.append(1)
        return {"n": len(runs)}

    store.run("implicit:alice:a", "digest-a", leaf, expires_after=_IMPLICIT_WINDOW)
    clock.value += _IMPLICIT_WINDOW + 1
    store.run("implicit:alice:a", "digest-a", leaf, expires_after=_IMPLICIT_WINDOW)

    assert len(runs) == 2


def test_a_journaled_key_keeps_its_receipt_for_the_intent_retention(tmp_path: Path) -> None:
    # The intent journal keeps a terminal for 24 h. A 600 s implicit window must not drop the
    # receipt first, or a recovery after the terminal persisted would re-run the write.
    clock = _Clock()
    store = _store(tmp_path, clock)
    runs: list[int] = []

    def leaf() -> dict:
        runs.append(1)
        return {"n": len(runs)}

    store.run(
        "implicit:alice:a",
        "digest-a",
        leaf,
        expires_after=_IMPLICIT_WINDOW,
        min_retention_seconds=_EXPLICIT_WINDOW,
    )
    clock.value += _EXPLICIT_WINDOW - 1
    store.run("implicit:alice:a", "digest-a", leaf, expires_after=_IMPLICIT_WINDOW)
    assert len(runs) == 1

    clock.value += 2
    store.run("implicit:alice:a", "digest-a", leaf, expires_after=_IMPLICIT_WINDOW)
    assert len(runs) == 2
