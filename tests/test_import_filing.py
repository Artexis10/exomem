"""The cell files a registered import stage by itself and carries on exactly once.

OpenSpec file-imports-on-the-cell, groups 3.1 and 3.2. The vault is a temporary one with a real stage, and the
filer calls `adoption_studio file` through the real dispatcher. Each test names the failure it catches.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_series import _window

from exomem import (
    adoption_run,
    cloud_import_job,
    import_filing,
    import_stage,
    write_intake,
    writer_lease,
)
from exomem.governance import principal as principal_module

CELL = "abcdefghijklmnop"
FILES = {"a.md": b"# A\n", "b.md": b"# B\n", "paper.pdf": consume.PDF}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))
    yield
    writer_lease.reset_managers_for_tests()


@pytest.fixture
def cloud_cell(monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "t" * 64)  # the serving cell holds its bearer; only it may file as the owner


def _stage(vault: Path, *, filing: str | None = "cell", files: dict[str, bytes] = FILES) -> str:
    import_id = _window(vault, files, series=list(files))
    if filing is not None:
        (vault / "_Imports" / import_id / import_stage.SERIES_FILE).write_text(json.dumps(
            {"label": "Field Notes", "paths": list(files), "filing": filing, "origin": {"browser_family": "cli"}}))
    return import_id


def _owner() -> dict:
    return write_intake.principal_record(principal_module.owner_principal(surface="library"))


def _wait_for(vault: Path, import_id: str, state: str, seconds: float = 120.0) -> dict:
    deadline = time.monotonic() + seconds
    view = import_filing.read_view(vault, import_id)
    while view["state"] != state and time.monotonic() < deadline:
        time.sleep(0.2)
        view = import_filing.read_view(vault, import_id)
    return view


def _sources(vault: Path) -> list[Path]:
    return sorted(path for path in (vault / "Knowledge Base" / "Sources").rglob("*.md") if path.name != "index.md")


def test_a_registered_stage_is_filed_with_no_client_call(vault: Path) -> None:
    import_id = _stage(vault)
    filer = import_filing.start(vault)
    try:
        assert filer.register(import_id, principal=_owner(), surface=None)
        view = _wait_for(vault, import_id, "done")
    finally:
        import_filing.stop(vault)

    assert view["state"] == "done"
    assert view["progress"]["filed"] == len(FILES)
    assert len(_sources(vault)) == len(FILES)


def _drive(filer: import_filing.Filer, import_id: str, *, calls: int = 40) -> None:
    """Make the filer's calls on one stage in turn, as its thread does, until the stage leaves the list."""
    for _ in range(calls):
        if filer.run_once(import_id) is None:
            return
    raise AssertionError("the stage was still on the list after the calls")


def _file_call(vault: Path, import_id: str) -> dict:
    """An agent's own `file` call, through the dispatcher, as the local owner."""
    command = write_intake.command_named("adoption_studio")
    with principal_module.request_scope(principal_module.local_owner_principal(surface="mcp")):
        return writer_lease.invoke_command(
            command, vault, action="file", path=f"_Imports/{import_id}")


@pytest.fixture
def one_step_calls(monkeypatch):
    """A call budget so short that each `file` call runs one lifecycle step, so a stage takes many calls."""
    monkeypatch.setenv("EXOMEM_MCP_REQUEST_BUDGET_SECONDS", "1")


def test_a_cloud_stage_that_asks_the_cell_to_file_is_found_at_start_and_filed(vault: Path, cloud_cell) -> None:
    """The Cloud import ends with the cell starting: the series file alone must be enough, with no agent present."""
    import_id = _stage(vault, filing="cell")
    _stage(vault, filing=None)  # a stage that did not ask stays for the assistant

    filer = import_filing.start(vault)
    try:
        view = _wait_for(vault, import_id, "done")
    finally:
        import_filing.stop(vault)

    assert view["state"] == "done" and view["progress"]["filed"] == len(FILES)
    assert len(_sources(vault)) == len(FILES)  # the stage that did not ask is not filed
    assert filer.running is False


def test_a_series_file_is_not_registered_outside_the_cell_it_was_imported_for(vault: Path) -> None:
    """Without the Cloud cell's own binding the stage has no owner to file as: it stays staged, not filed as someone."""
    import_id = _stage(vault, filing="cell")

    filer = import_filing.Filer(vault)
    filer.scan()

    assert import_filing.read_view(vault, import_id) == {"state": "unknown", "stop": None, "person": []}


def test_a_crash_after_the_cell_committed_a_call_repeats_it_under_its_identity_and_files_each_file_once(
    vault: Path, cloud_cell, one_step_calls, monkeypatch,
) -> None:
    """The filer dies after a call committed and before it recorded the reply: a new identity would file twice."""
    import_id = _stage(vault)
    seen: list[str] = []
    real = writer_lease.invoke_command

    class Died(BaseException):
        """The process ends: nothing after the call runs."""

    def dies_after_the_second_call(*args, **kwargs):
        seen.append(kwargs["idempotency_key"])
        result = real(*args, **kwargs)
        if len(seen) == 2:
            raise Died
        return result

    monkeypatch.setattr(writer_lease, "invoke_command", dies_after_the_second_call)
    first = import_filing.Filer(vault)
    first.register(import_id, principal=_owner(), surface=None)
    with pytest.raises(Died):
        _drive(first, import_id)
    stored = import_filing.read_record(vault, import_id)
    assert stored["call"]["seq"] == 2 and stored["state"] == "working"

    second = import_filing.Filer(vault)  # the restart
    second.scan()
    _drive(second, import_id)

    assert seen[:3] == [f"import-file:{import_id}:{n}" for n in (1, 2, 2)]
    pages = [path.read_text(encoding="utf-8") for path in _sources(vault)]
    assert len(pages) == len(FILES)
    assert len({line for page in pages for line in page.splitlines() if line.startswith("original_sha256:")}) == len(FILES)
    assert import_filing.read_view(vault, import_id)["state"] == "done"
    assert len(adoption_run.AdoptionRunStore(vault).runs_at(f"_Imports/{import_id}")) == 1


def test_an_agents_file_call_registers_the_stage_and_the_cell_finishes_it(vault: Path, one_step_calls) -> None:
    """The agent calls `file` once and goes away: with no registration the stage waits for a call that never comes."""
    import_id = _stage(vault, filing=None)
    filer = import_filing.start(vault)
    try:
        first = _file_call(vault, import_id)
        view = _wait_for(vault, import_id, "done")
    finally:
        import_filing.stop(vault)

    assert first["filing"] == "working"
    assert view["state"] == "done" and len(_sources(vault)) == len(FILES)
    assert filer.running is False


def test_a_foreground_file_call_and_the_filer_share_one_run_and_file_each_file_once(vault: Path, one_step_calls) -> None:
    """The agent keeps calling while the filer works: a second run, or a file filed in both, would show here."""
    import_id = _stage(vault, filing=None)
    import_filing.start(vault)
    try:
        reply = _file_call(vault, import_id)
        while reply["filing"] == "working":
            reply = _file_call(vault, import_id)
        view = _wait_for(vault, import_id, "done")
    finally:
        import_filing.stop(vault)

    assert reply["filing"] == "done" and view["state"] == "done"
    assert len(_sources(vault)) == len(FILES)
    assert len(adoption_run.AdoptionRunStore(vault).runs_at(f"_Imports/{import_id}")) == 1


def test_a_free_space_stop_parks_the_stage_and_a_later_call_resumes_it(vault: Path, monkeypatch) -> None:
    """A stage that stops for space must say so and carry on once there is room, without filing around the stop."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(1 << 50))  # more than any volume holds

    delays = [filer.run_once(import_id) for _ in range(6)]
    parked = import_filing.read_view(vault, import_id)

    assert import_filing.SPACE_CHECK_SECONDS in delays
    assert parked["state"] == "parked" and parked["stop"]["code"] == "INSUFFICIENT_SPACE"
    assert [need["need"] for need in parked["person"]] == ["free_space"]
    assert _sources(vault) == []

    monkeypatch.delenv("EXOMEM_IMPORT_RESERVE_BYTES")
    _drive(filer, import_id)  # the hourly check, or a file call, finds room

    assert import_filing.read_view(vault, import_id)["state"] == "done"
    assert len(_sources(vault)) == len(FILES)


def test_a_vault_with_no_knowledge_base_is_created_by_the_filer_and_the_stage_is_filed_once(vault: Path) -> None:
    """A first Home import on a new cell has no Knowledge Base: `file` refuses it, and no one is there to start."""
    shutil.rmtree(vault / "Knowledge Base")
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)

    _drive(filer, import_id)

    assert import_filing.read_view(vault, import_id)["state"] == "done"
    assert len(_sources(vault)) == len(FILES)
    assert len(adoption_run.AdoptionRunStore(vault).runs_at(f"_Imports/{import_id}")) == 1


def test_a_stage_that_was_discarded_ends_discarded(vault: Path, cloud_cell) -> None:
    """A stage the person discarded must leave the list, not be refiled from its record at every start."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    cloud_import_job.discard(vault, import_id)

    import_filing.Filer(vault).scan()

    assert import_filing.read_view(vault, import_id)["state"] == "discarded"
    assert filer.run_once(import_id) is None


def test_the_filing_view_of_a_stage_with_no_record_is_unknown_and_reports_no_counts(vault: Path) -> None:
    import_id = _stage(vault, filing=None)

    reply = adoption_run.status(vault, path=f"_Imports/{import_id}", view="filing")

    assert reply["state"] == "unknown"
    assert "progress" not in reply and "rate" not in reply
    assert reply["path"] == f"_Imports/{import_id}"


def test_the_filing_view_reads_the_record_alone_and_reports_the_measured_rate_once_two_calls_finished(
    vault: Path, one_step_calls, monkeypatch,
) -> None:
    """A client polls this view for a stage of tens of thousands of files: it may open no run, inventory or outcome."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    filer.run_once(import_id)
    assert "rate" not in adoption_run.status(vault, path=f"_Imports/{import_id}", view="filing")
    filer.run_once(import_id)
    opened: list[str] = []
    real_open = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(self.name) or real_open(self, *a, **k))

    reply = adoption_run.status(vault, path=f"_Imports/{import_id}", view="filing")

    assert opened == [f"{import_id}.json"]
    assert reply["state"] == "working" and reply["rate"]["over_calls"] == 2
    assert set(reply["progress"]) >= {"filed", "remaining"}


def test_the_filing_view_refuses_a_run_id_and_a_path_that_is_not_a_staged_folder(vault: Path) -> None:
    import_id = _stage(vault, filing=None)

    for kwargs in ({"path": "Notes"}, {"path": f"_Imports/{import_id}", "run_id": "run-1"}, {}):
        with pytest.raises(adoption_run.AdoptionRunError) as refused:
            adoption_run.status(vault, view="filing", **kwargs)
        assert refused.value.code == "IMPORT_REQUEST_INVALID"
