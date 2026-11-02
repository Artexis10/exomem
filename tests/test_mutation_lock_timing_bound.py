"""The per-context hold timing map must not make a hold slower as a process sees more coordinators."""

from __future__ import annotations

import contextvars
import time
from pathlib import Path

from exomem.mutation_lock import VaultMutationCoordinator, last_mutation_timing

COORDINATORS = 2000
HOLDS = 300


def _per_hold_seconds(state_root: Path, vault: Path) -> float:
    hot = VaultMutationCoordinator(state_root, vault)
    with hot.hold(operation="warm"):
        pass
    start = time.perf_counter()
    for _ in range(HOLDS):
        with hot.hold(operation="hot"):
            pass
    assert last_mutation_timing(hot)["operation"] == "hot"
    return (time.perf_counter() - start) / HOLDS


def test_hold_cost_does_not_grow_with_the_coordinators_a_context_has_seen(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    fresh = contextvars.copy_context().run(_per_hold_seconds, state_root, tmp_path / "hot")

    def busy_context() -> float:
        for index in range(COORDINATORS):
            with VaultMutationCoordinator(state_root, tmp_path / f"vault-{index}").hold(
                operation="once"
            ):
                pass
        return _per_hold_seconds(state_root, tmp_path / "hot")

    seen = contextvars.copy_context().run(busy_context)
    assert seen < fresh * 3 + 0.0005
