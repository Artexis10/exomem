"""A source capture waits out an ordinary busy boundary and runs its body once.

`capture_source_artifacts` used to take `mutation_guard`, so a capture that met a
boundary held past one short acquire was refused `MUTATION_BUSY` and the agent
told the user the file was not stored. It now takes the absorbing guard.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_capture_contention_absorbed import _Contention, _managers, _run_concurrently

from exomem import add as add_module
from exomem import client_artifacts, writer_lease
from exomem import schema as schema_module
from exomem.cli_ops import OpError


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def _stage(tmp_path: Path):  # noqa: ANN202
    def stage(_file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        payload = b"quarterly figures"
        staged = tmp_path / "stage-file-a.bin"
        staged.write_bytes(payload)
        return client_artifacts.StagedArtifact(
            file_id="file-a",
            path=staged,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/octet-stream",
            filename="figures.bin",
        )

    return stage


def _capture(vault: Path, schema: schema_module.SourceSchema, writer, **extra) -> dict:
    token = writer_lease._ACTIVE_LEASE_MANAGER.set(writer)
    try:
        return client_artifacts.capture_source_artifacts(
            vault,
            source_schema=schema,
            title="Quarterly figures",
            source_type="research-report",
            files=[{"download_url": "https://files.example/a", "file_id": "file-a"}],
            **extra,
        )
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(token)


_ADOPTION = {"key": "figures-once", "trigger": "selected", "selected_file_id": "file-a"}


@pytest.mark.parametrize("extra", [{}, {"adoption": _ADOPTION}], ids=["plain", "adoption"])
def test_a_source_capture_waits_out_a_held_boundary_and_stores_once(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict,
) -> None:
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage(tmp_path))
    schema = schema_module.load_source_schema(vault)
    writer, holder = _managers(vault.parent / "state")
    with _Contention(monkeypatch, holder, vault, refusals=1) as contention:
        (result,) = _run_concurrently([lambda: _capture(vault, schema, writer, **extra)], contention)

    assert result["files"][0]["outcome"] == "stored", result
    stored = vault / result["files"][0]["stored_path"]
    assert stored.read_bytes() == b"quarterly figures"
    assert len(list(stored.parent.glob("*.bin"))) == 1


def test_a_busy_raised_after_the_body_started_is_not_run_again(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage(tmp_path))
    schema = schema_module.load_source_schema(vault)
    writer, _holder = _managers(vault.parent / "state")
    calls: list[int] = []

    def busy_inside_the_body(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        raise OpError("MUTATION_BUSY", "busy inside the body", details={"committed": False})

    monkeypatch.setattr(add_module, "add", busy_inside_the_body)
    with pytest.raises(OpError) as raised:
        _capture(vault, schema, writer)

    assert raised.value.code == "MUTATION_BUSY"
    assert calls == [1]
