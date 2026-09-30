"""D1-T7: hosted and cloud cells start no dreamer in this tranche.

A background proposal worker for a tenant needs the hosted lifecycle's worker
registration and its own review, so the hosted cell never reaches the starter
and a cloud cell declines it, whatever the dreamer setting says.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from test_hosted_cell import _provisioned

from exomem import dreamer, server_runtime


@pytest.fixture(autouse=True)
def _clean():
    dreamer.reset_for_tests()
    yield
    dreamer.reset_for_tests()


def _dreamer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == dreamer.THREAD_NAME]


def test_hosted_cell_starts_no_dreamer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values, _config = _provisioned(tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    started: list[str] = []
    monkeypatch.setattr(dreamer, "start", lambda root: started.append(str(root)))
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _vault: None)
    monkeypatch.setattr(server_runtime, "_start_retrieval_runtime", lambda _v: None, raising=False)

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert runtime.hosted_lifecycle is not None
    assert started == []
    assert _dreamer_threads() == []


def test_cloud_cell_declines_the_dreamer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import cloud_cell

    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    monkeypatch.setattr(cloud_cell, "cloud_mode_enabled", lambda *_a, **_k: True)
    started: list[str] = []
    monkeypatch.setattr(dreamer, "start", lambda root: started.append(str(root)))
    assert server_runtime._start_dreamer(tmp_path) is None
    assert started == []
    assert _dreamer_threads() == []
