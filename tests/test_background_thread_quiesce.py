"""The suite's teardown drain covers every vault-walking background owner.

A test that starts one of these and returns leaves a daemon thread walking its
vault while the next test runs. A later test that counts process-wide directory
probes then counts that walk too.
"""

from __future__ import annotations

import threading

import pytest
from conftest import _drain_background_threads


@pytest.mark.parametrize(
    "name",
    [
        "exomem-graph-rebuild",
        "exomem-working-set-warm",
        "exomem-identity-catalogue-warm",
        "exomem-lexical-repair",
    ],
)
def test_the_drain_waits_for_a_vault_walking_background_thread(name: str) -> None:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name=name, daemon=True)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="did not finish"):
            _drain_background_threads(timeout=0.05)
    finally:
        release.set()
        thread.join(timeout=5)

    _drain_background_threads(timeout=5)
    assert not thread.is_alive()


def test_the_drain_ignores_long_lived_service_threads() -> None:
    # A snapshotter or lease renewer lives as long as the process. Waiting for
    # one would hang every teardown.
    release = threading.Event()
    thread = threading.Thread(
        target=release.wait, name="exomem-metrics-snapshotter", daemon=True
    )
    thread.start()
    try:
        _drain_background_threads(timeout=0.05)
    finally:
        release.set()
        thread.join(timeout=5)
