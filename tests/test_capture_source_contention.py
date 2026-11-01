"""A source capture waits out an ordinary busy boundary and runs its body once.

`capture_source_artifacts` used to take `mutation_guard`, so a capture that met a
boundary held past one short acquire was refused `MUTATION_BUSY` and the agent
told the user the file was not stored. It now takes the absorbing guard.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from test_capture_contention_absorbed import _command, _Contention, _managers, _run_concurrently

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
        (result,) = _run_concurrently(
            [lambda: _capture(vault, schema, writer, **extra)], contention
        )

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


def test_a_busy_boundary_at_the_second_of_three_files_commits_all_three_once(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stage(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        payload = f"quarterly figures {file['file_id']}".encode()
        staged = tmp_path / f"stage-{file['file_id']}.bin"
        staged.write_bytes(payload)
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"],
            path=staged,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/octet-stream",
            filename=f"{file['file_id']}.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    writer, holder = _managers(vault.parent / "state")
    contention = _Contention(monkeypatch, holder, vault, refusals=1)
    real_guard = writer.absorbing_mutation_guard
    entered: list[int] = []

    def guard_met_by_another_process_on_file_two(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entered.append(1)
        if len(entered) == 2:
            # File 1 has committed and let go; another holder now takes the boundary.
            contention.__enter__()
            threading.Thread(
                target=lambda: (contention.saw_contention.wait(15), contention.release.set())
            ).start()
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(
        writer, "absorbing_mutation_guard", guard_met_by_another_process_on_file_two
    )
    try:
        result = writer.invoke(
            _command("capture_source"),
            (vault, schema_module.load_source_schema(vault)),
            {
                "title": "Quarterly figures",
                "source_type": "research-report",
                "files": [
                    {"download_url": f"https://files.example/{name}", "file_id": name}
                    for name in ("file-a", "file-b", "file-c")
                ],
            },
            implicit_idempotency_scope="principal:test",
        )
    finally:
        if len(entered) >= 2:
            contention.__exit__()

    assert result["state"] == "committed", result
    assert contention.seen >= 1, "file 2 never met the held boundary"
    assert [file["outcome"] for file in result["files"]] == ["stored"] * 3, result
    for file in result["files"]:
        assert (
            len(list((vault / file["stored_path"]).parent.glob(Path(file["stored_path"]).name)))
            == 1
        )
