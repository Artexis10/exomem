"""The cell files a registered import stage by itself and carries on exactly once.

OpenSpec file-imports-on-the-cell, groups 3.1 and 3.2. The vault is a temporary one with a real stage, and the
filer calls `adoption_studio file` through the real dispatcher. Each test names the failure it catches.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_series import _window

from exomem import (
    adoption_plan,
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


def _file_call(vault: Path, import_id: str, **arguments) -> dict:
    """An agent's own `file` call, through the dispatcher, as the local owner."""
    command = write_intake.command_named("adoption_studio")
    with principal_module.request_scope(principal_module.local_owner_principal(surface="mcp")):
        return writer_lease.invoke_command(
            command, vault, action="file", path=f"_Imports/{import_id}", **arguments)


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

    view = import_filing.read_view(vault, import_id)
    assert view["state"] == "unknown" and view["progress"] is None and view["needs"] == []


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


def test_an_agents_file_call_registers_the_stage_and_the_cell_finishes_it(
    vault: Path, one_step_calls, monkeypatch
) -> None:
    """The agent calls `file` once and goes away: with no registration the stage waits for a call that never comes."""
    monkeypatch.setattr(import_filing, "AGENT_QUIET_SECONDS", 1.0)  # the agent never calls again, so the quiet window is the wait
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


def test_the_filer_stands_aside_while_an_agent_files_the_stage_and_files_it_once_the_agent_stops(
    vault: Path, one_step_calls, monkeypatch
) -> None:
    """Catches a filer that takes the boundary between an agent's calls: each of its calls ends after one turn for the
    agent that waits, and pays the fixed cost of a call again, so the two together file slower than the agent alone.
    It also catches a view that parks the stage for it, or stamps the record again for each wake of the filer."""
    monkeypatch.setattr(import_filing, "AGENT_QUIET_SECONDS", 1.5)
    import_id = _stage(vault, filing=None)
    import_filing.start(vault)
    try:
        replies = [_file_call(vault, import_id), _file_call(vault, import_id)]
        time.sleep(0.5)  # the filer was woken by each call, and would have taken the boundary by now
        record = import_filing.read_record(vault, import_id)
        view = import_filing.read_view(vault, import_id)
        time.sleep(0.5)
        later = import_filing.read_record(vault, import_id)
        done = _wait_for(vault, import_id, "done", seconds=60)  # the agent stops calling; the quiet window passes
        after = import_filing.read_record(vault, import_id)
        finished = import_filing.read_view(vault, import_id)
    finally:
        import_filing.stop(vault)

    assert [reply["filing"] for reply in replies] == ["working", "working"]
    assert record["seq"] == 0  # the filer made no call while the agent filed
    assert later["as_of"] == record["as_of"]  # and stamped no record for the wait
    assert view["state"] == "working" and view["stop"] is None and view["needs"] == []
    assert view["note"] == import_filing.AGENT_FILING_NOTE
    # The agent's own counts are the view's: its reply, no staler than a filer call's would be.
    expected = {key: replies[-1]["progress"][key] for key in ("filed", "skipped_duplicates", "failed")}
    assert record["progress"] == expected
    assert view["progress"]["filed"] == expected["filed"]
    assert done["state"] == "done" and after["seq"] > 0  # then the cell carried the stage to its end
    assert "note" not in finished  # and the view no longer says an agent files it
    assert len(_sources(vault)) == len(FILES)
    assert len(adoption_run.AdoptionRunStore(vault).runs_at(f"_Imports/{import_id}")) == 1


def test_an_agents_hand_off_with_wait_false_is_filed_at_once_and_not_after_the_quiet_window(
    vault: Path, one_step_calls, monkeypatch
) -> None:
    """Catches a hand-off that lands inside the window its earlier calls left: the filer would wait it out for nothing."""
    monkeypatch.setattr(import_filing, "AGENT_QUIET_SECONDS", 600.0)
    import_id = _stage(vault, filing=None)
    import_filing.start(vault)
    try:
        _file_call(vault, import_id)
        waiting = import_filing.read_view(vault, import_id)
        _file_call(vault, import_id, wait=False)
        done = _wait_for(vault, import_id, "done", seconds=30)
    finally:
        import_filing.stop(vault)

    assert waiting["note"] == import_filing.AGENT_FILING_NOTE  # the stage was the agent's, for ten minutes
    assert done["state"] == "done" and len(_sources(vault)) == len(FILES)


def test_an_agent_that_repeats_an_earlier_call_does_not_take_the_views_counts_back_to_that_reply(vault: Path) -> None:
    """Catches a repeat of an earlier `file` call, answered with its stored reply, that writes the older counts with a new `as_of`."""
    import_id = _stage(vault, filing=None)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    older = {"filed": 2, "skipped_duplicates": 0, "failed": 0}
    newer = {"filed": 5, "skipped_duplicates": 1, "failed": 0}
    for counts in (older, newer):
        filer.agent_began(import_id)
        filer.agent_ended(import_id, working=True, counts=counts)
    kept = import_filing.read_record(vault, import_id)

    filer.agent_began(import_id)
    filer.agent_ended(import_id, working=True, counts=older)  # the stored reply of the first call, answered again

    repeated = import_filing.read_record(vault, import_id)
    assert kept["progress"] == newer and repeated["progress"] == newer
    assert repeated["as_of"] == kept["as_of"] and repeated["calls"] == kept["calls"]


def test_the_filers_own_file_call_does_not_list_its_stage_with_itself_again(vault: Path) -> None:
    """Catches a filer whose own `file` call listed the stage again: that reset its due time, so no back-off held."""
    import_id = _stage(vault, filing=None)
    path = f"_Imports/{import_id}"
    principal = write_intake.restore_principal(_owner())
    import_filing.start(vault)
    try:
        with principal_module.request_scope(principal), writer_lease.as_background_work():
            with import_filing.agent_filing(vault, path):
                pass
        from_the_filer = import_filing.read_record(vault, import_id)
        with principal_module.request_scope(principal):
            with import_filing.agent_filing(vault, path):
                pass
        from_an_agent = import_filing.read_record(vault, import_id)
    finally:
        import_filing.stop(vault)

    assert from_the_filer is None
    assert from_an_agent is not None  # the same call from an agent lists the stage: the check above is not vacuous


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
    (need,) = parked["needs"]
    assert need["need"] == "free_space" and need["bytes"] >= 1 << 50  # the chunk plus the reserve that the volume must hold
    assert _sources(vault) == []

    monkeypatch.delenv("EXOMEM_IMPORT_RESERVE_BYTES")
    _drive(filer, import_id)  # the hourly check, or a file call, finds room

    assert import_filing.read_view(vault, import_id)["state"] == "done"
    assert len(_sources(vault)) == len(FILES)


def test_a_read_only_cell_parks_the_stage_for_a_later_check_and_files_it_when_the_mode_ends(vault: Path, monkeypatch) -> None:
    """A cell that serves read-only for a while must not leave the stage stopped until a person calls or the pod restarts."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    monkeypatch.setenv("EXOMEM_CLOUD_READ_ONLY", "1")

    delay = filer.run_once(import_id)
    parked = import_filing.read_view(vault, import_id)

    assert delay == import_filing.READ_ONLY_CHECK_SECONDS
    assert parked["state"] == "parked" and parked["stop"]["code"] == "CLOUD_CELL_READ_ONLY"
    assert parked["needs"] == [{"need": "read_only"}]
    assert _sources(vault) == []

    monkeypatch.delenv("EXOMEM_CLOUD_READ_ONLY")
    _drive(filer, import_id)  # the next scheduled check finds the cell writable

    done = import_filing.read_view(vault, import_id)
    assert done["state"] == "done" and done["stop"] is None and done["needs"] == []
    assert len(_sources(vault)) == len(FILES)


def test_a_stage_whose_calls_move_nothing_waits_longer_and_tries_again_without_asking_for_support(
    vault: Path, monkeypatch
) -> None:
    """Catches a filer that calls again at once for ever on a stage that cannot move, or one that gives up on it for good."""
    stuck, plans = [True], []
    real_plan = adoption_run.plan

    def plan(root, **options):
        plans.append(1)
        return None if stuck[0] else real_plan(root, **options)  # a stuck plan makes no progress

    monkeypatch.setattr(adoption_run, "plan", plan)
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)

    waits = []
    while len(waits) < 12:
        delay = filer.run_once(import_id)
        assert delay is not None, "a stall must not take the stage off the list"
        if delay > 0:
            waits.append(delay)
    shown = import_filing.read_view(vault, import_id)

    assert len(plans) == adoption_run.FILING_STALL_CALLS + 1 + len(waits) - 1, plans  # one call after each wait
    assert waits[:4] == [60.0, 120.0, 240.0, 480.0] and set(waits[4:]) == {import_filing.STALL_MAX_BACKOFF_SECONDS}
    assert shown["state"] == "parked" and shown["stop"]["code"] == import_filing.STOP_STALLED
    assert shown["stop"]["reason"] == f"filing made no progress in {adoption_run.FILING_STALL_CALLS + len(waits) - 1} calls; it will try again"
    assert shown["needs"] == []  # a stall is not a fault the person can mend

    stuck[0] = False  # the step moves on at a retry, and nothing needed waking
    while filer.run_once(import_id) is not None:
        pass
    done = import_filing.read_view(vault, import_id)

    assert done["state"] == "done" and done["stop"] is None and done["needs"] == []
    assert len(_sources(vault)) == len(FILES)


def _every_call_ends_for_a_waiting_writer(monkeypatch) -> None:
    """Make each step end at its first item boundary, as a writer that waits does, and the turn last no time."""
    monkeypatch.setattr(adoption_run, "_writer_waits", lambda root: True)
    monkeypatch.setattr(adoption_run, "TURN_SECONDS", 0.0)


def _file_to_done_without_a_stall(vault: Path, import_id: str, *, calls: int = 120) -> int:
    """Call the filer until the stage leaves the list; return the most calls in a row that moved nothing."""
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    unmoved = 0
    for _ in range(calls):
        if filer.run_once(import_id) is None:
            break
        unmoved = max(unmoved, import_filing.read_record(vault, import_id)["unmoved"])
    assert import_filing.read_view(vault, import_id)["state"] == "done"
    return unmoved


def test_a_read_of_many_unread_sources_under_a_waiting_writer_is_not_a_stall(vault: Path, monkeypatch) -> None:
    """Catches a filer that parks a plan whose cold read of the Sources takes more calls than the stall limit.

    The read moves no count and no planned file until it ends, so only the pages it has read show that it goes on.
    """
    sources = vault / "Knowledge Base" / "Sources"
    for number in range(adoption_run.FILING_STALL_CALLS + 4):
        page = sources / f"earlier-{number}.md"
        page.write_text(
            f"---\nimported_from: Elsewhere/earlier-{number}.md\noriginal_sha256: {number:064x}\ncaptured: 2026-10-27\n---\n# E\n",
            encoding="utf-8")
    # A page whose change time is within the racy window is read again at every pass, and `os.utime` cannot move the
    # change time, so the window is closed for the test: each page read is a page the process remembers.
    monkeypatch.setattr(adoption_plan, "_RACY_NS", 0)
    adoption_plan._SOURCE_PAGES.clear()  # a process that has read no Source yet
    monkeypatch.setattr(adoption_plan, "INDEX_READ_FLOOR", 1)
    _every_call_ends_for_a_waiting_writer(monkeypatch)

    unmoved = _file_to_done_without_a_stall(vault, _stage(vault))

    assert unmoved < adoption_run.FILING_STALL_CALLS, unmoved
    assert len(_sources(vault)) == len(FILES) + adoption_run.FILING_STALL_CALLS + 4


def test_a_finish_that_removes_its_staged_copies_over_many_calls_is_not_a_stall(vault: Path, monkeypatch) -> None:
    """Catches a filer that parks a `finish` whose removals take more calls than the stall limit.

    A removed copy changes no count and not the page's phase, so only the copies settled show that `finish` goes on.
    """
    files = {f"note-{number}.md": f"# Note {number}\n".encode() for number in range(adoption_run.FILING_STALL_CALLS + 3)}
    _every_call_ends_for_a_waiting_writer(monkeypatch)

    unmoved = _file_to_done_without_a_stall(vault, _stage(vault, files=files))

    assert unmoved < adoption_run.FILING_STALL_CALLS, unmoved
    assert len(_sources(vault)) == len(files)


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
    assert reply["progress"] is None and "left_out" not in reply and "rate" not in reply
    assert reply["path"] == f"_Imports/{import_id}"


def test_the_filing_view_reads_the_record_alone_and_reports_the_measured_rate_once_filed_work_is_covered(
    vault: Path, one_step_calls, monkeypatch,
) -> None:
    """A client polls this view for a stage of tens of thousands of files: it may open no run, inventory or outcome."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)
    filer.run_once(import_id)
    assert "rate" not in adoption_run.status(vault, path=f"_Imports/{import_id}", view="filing")  # one sample is no window
    while "rate" not in import_filing.read_view(vault, import_id):
        assert filer.run_once(import_id) is not None
    opened: list[str] = []
    real_open = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(self.name) or real_open(self, *a, **k))

    reply = adoption_run.status(vault, path=f"_Imports/{import_id}", view="filing")

    assert opened == [f"{import_id}.json"]
    assert reply["state"] in ("working", "done") and reply["rate"]["files_per_minute"] > 0
    assert set(reply["progress"]) == {"total", "filed", "skipped_duplicates", "failed", "left_out", "remaining"}


def test_a_stage_filed_in_one_call_after_its_registration_shows_a_rate_and_never_0_0(vault: Path) -> None:
    """The first call's files count: with only the calls as samples, a stage that one call filed showed 0.0 files a minute."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None, fresh_stage=True)

    _drive(filer, import_id)

    view = import_filing.read_view(vault, import_id)
    assert view["state"] == "done" and view["progress"]["filed"] == len(FILES)
    assert view["rate"]["files_per_minute"] > 0


def test_a_stage_that_lists_no_manifest_reports_no_total_and_no_remaining_but_its_counts(vault: Path) -> None:
    """With no receipt the total is unknown: a total of 0 would read as nothing to file, and `remaining` as 0 would say done."""
    import_id = _stage(vault)
    (import_stage.receipt_path(vault, import_id)).unlink()
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)

    filer.run_once(import_id)

    progress = import_filing.read_view(vault, import_id)["progress"]
    assert progress["total"] is None and progress["remaining"] is None
    assert isinstance(progress["filed"], int)


def test_a_done_stage_lists_what_it_left_out_by_reason_and_counts_the_rest_as_remaining_none(vault: Path) -> None:
    """The person must see which files were not filed and why: `done` with no list says everything went in."""
    import_id = _stage(vault, files={**FILES, ".draft.md": b"# draft\n", "Archive/.old.md": b"# old\n"})
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)

    _drive(filer, import_id)

    view = import_filing.read_view(vault, import_id)
    assert view["state"] == "done" and view["needs"] == []
    assert view["left_out"] == {
        "by_reason": {"outside_run": 2},
        "items": [{"path": ".draft.md", "code": "outside_run"}, {"path": "Archive/.old.md", "code": "outside_run"}],
        "truncated": False,
    }
    assert view["progress"] == {
        "total": 5, "filed": len(FILES), "skipped_duplicates": 0, "failed": 0, "left_out": 2, "remaining": 0}


def test_a_done_stage_lists_at_most_100_left_out_files_and_counts_them_all(vault: Path) -> None:
    """A stage of thousands of hidden files must not put thousands of paths in a view that a client polls."""
    hidden = {f".h{n:03}.md": b"# h\n" for n in range(import_filing.LEFT_OUT_ITEMS + 1)}
    import_id = _stage(vault, files={**FILES, **hidden})
    filer = import_filing.Filer(vault)
    filer.register(import_id, principal=_owner(), surface=None)

    _drive(filer, import_id)

    left_out = import_filing.read_view(vault, import_id)["left_out"]
    assert sum(left_out["by_reason"].values()) == len(hidden)
    assert len(left_out["items"]) == import_filing.LEFT_OUT_ITEMS and left_out["truncated"] is True
    assert [item["path"] for item in left_out["items"]] == sorted(item["path"] for item in left_out["items"])


def _spy_on_file_calls(monkeypatch) -> list[str]:
    """The name of the thread that makes each `file` call."""
    callers: list[str] = []
    real = adoption_run.file

    def spy(*args, **kwargs):
        callers.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(adoption_run, "file", spy)
    return callers


def test_a_wake_call_after_the_login_ended_registers_the_stage_under_the_new_login_and_files_nothing_itself(
    vault: Path, cloud_cell, monkeypatch,
) -> None:
    """Home wakes a stage whose Cloud login ended: the call must resume it for the new caller and answer without filing."""
    import_id = _stage(vault)
    filer = import_filing.Filer(vault)
    cloud_owner = write_intake.principal_record(principal_module.cloud_owner_principal(CELL, surface="cloud-import"))
    filer.register(import_id, principal=cloud_owner, surface=None)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN")  # the cell no longer holds the login that registered the stage
    filer.run_once(import_id)
    ended = import_filing.read_view(vault, import_id)
    assert ended["state"] == "parked" and ended["stop"]["code"] == "IMPORT_FILING_LOGIN_ENDED"
    assert ended["needs"] == [{"need": "login"}]

    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "t" * 64)
    callers = _spy_on_file_calls(monkeypatch)
    import_filing.start(vault)
    try:
        started = time.monotonic()
        reply = _file_call(vault, import_id, wait=False)
        answered_in = time.monotonic() - started
        view = _wait_for(vault, import_id, "done")
    finally:
        import_filing.stop(vault)

    assert reply["state"] == "working" and reply["needs"] == [] and reply["mutated"] is False
    assert answered_in < 5 and "MainThread" not in callers and set(callers) == {"exomem-import-filer"}
    assert view["state"] == "done" and len(_sources(vault)) == len(FILES)


def test_a_wake_call_from_a_login_the_cell_cannot_replay_refuses_and_registers_nothing(vault: Path) -> None:
    """A delegated session may be narrowed after the call: filing as it later would exceed what the owner still grants."""
    import_id = _stage(vault, filing=None)
    command = write_intake.command_named("adoption_studio")
    delegated = principal_module.local_owner_principal(surface="mcp").with_authorization_session("session-1")
    import_filing.start(vault)
    try:
        with principal_module.request_scope(delegated):
            with pytest.raises(ValueError, match="IMPORT_FILING_LOGIN_NOT_REPLAYABLE"):
                writer_lease.invoke_command(command, vault, action="file", path=f"_Imports/{import_id}", wait=False)
    finally:
        import_filing.stop(vault)

    assert import_filing.read_view(vault, import_id)["state"] == "unknown"


def test_the_filing_view_refuses_a_run_id_and_a_path_that_is_not_a_staged_folder(vault: Path) -> None:
    import_id = _stage(vault, filing=None)

    for kwargs in ({"path": "Notes"}, {"path": f"_Imports/{import_id}", "run_id": "run-1"}, {}):
        with pytest.raises(adoption_run.AdoptionRunError) as refused:
            adoption_run.status(vault, view="filing", **kwargs)
        assert refused.value.code == "IMPORT_REQUEST_INVALID"
