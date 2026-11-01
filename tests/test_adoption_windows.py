"""The status of an import sent as windows: its budget, its unfiled members, its residual and the acknowledgement."""
from __future__ import annotations

import hashlib
import json
import shutil
import types
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_consume import PDF
from test_adoption_rename import _job_stage, _stop_after_the_rename_of

from exomem import (
    adoption_run,
    cloud_import_job,
    commands,
    import_stage,
    import_windows,
    state_paths,
)
from exomem.cloud_import import ArchiveRefused

GIB = 1 << 30
MIB = 1 << 20


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


@pytest.fixture
def store_policy(vault: Path, tmp_path: Path, monkeypatch):
    """Write the media store policy of `vault`: an enabled store plus the settings that a test names."""

    def write(**settings) -> None:
        policy = {"store": "test-store", "adapter": "filesystem-test", "root": str(tmp_path / "objects"),
                  "key_version": "1", "enabled": True, **settings}
        config = tmp_path / "media.json"
        config.write_text(json.dumps({"vaults": {str(vault.resolve()): policy}}))
        monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))

    return write


def _volume(monkeypatch, *, total: int, used: int) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: types.SimpleNamespace(total=total, used=used, free=total - used))


def _window(vault: Path, members: dict[str, bytes | int]) -> str:
    """A window as the Job leaves it: a receipt manifest and the series file.

    A member given as bytes is staged. A member given as a count of bytes is only declared, as a member
    that adoption has renamed away or that never arrived is.
    """
    import_id = str(uuid.uuid4())
    stage = vault / "_Imports" / import_id
    entries = []
    for path, data in {**{f"Field Notes/{name}": data for name, data in members.items()}, import_stage.SERIES_FILE: b"{}"}.items():
        if isinstance(data, bytes):
            (stage / path).parent.mkdir(parents=True, exist_ok=True)
            (stage / path).write_bytes(data)
            entries.append(import_stage.member(path, len(data), hashlib.sha256(data).hexdigest(), import_stage.Identity.of((stage / path).stat())))
        else:
            entries.append(import_stage.member(path, data, "0" * 64))
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(entries),
        declared_bytes=1, ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(entries)}, "members": entries}))
    return import_id


def _file_all_but(vault: Path, import_id: str, *left: str) -> None:
    """Adopt the stage with `left` (paths under the stage) unselected, and finish the run."""
    start_path = f"_Imports/{import_id}"
    run_id = adoption_run.start(vault, path=start_path, today=consume.TODAY)["run_id"]
    adoption_run.select(vault, run_id=run_id, include=[start_path], exclude=[f"{start_path}/{path}" for path in left])
    plan_id = adoption_run.plan(vault, run_id=run_id, today=consume.TODAY)["plan"]["plan_id"]
    consume._apply(vault, run_id, plan_id)
    adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)


def _status(vault: Path, **arguments) -> dict:
    return commands.op_adoption_studio(vault, action="status", **arguments)


def _windows(reply: dict) -> list[dict]:
    return reply["import_windows"]["windows"]


# The budget


def test_the_status_reports_the_window_budget_from_its_inputs(vault: Path, store_policy, monkeypatch) -> None:
    """Catches a budget that leaves out the reserve, the hydration capacity, the placed copies or the unfiled text."""
    store_policy(growth_threshold=0.8, max_temporary_bytes=int(5.25 * GIB), max_hydrations=1)
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(GIB // 4))
    _window(vault, {f"notes/{number:03}.md": 4 * MIB for number in range(128)})
    _volume(monkeypatch, total=20 * GIB, used=3 * GIB)

    reply = _status(vault)["window_budget"]

    # 0.8 * 20 - 3 used - 0.25 reserve - 5.25 hydration - 1 * 5 placed copy - 0.5 unfiled text, in GiB.
    assert reply["window_budget"] == {"bytes": 2 * GIB}
    assert reply["classification"]["largest_offloadable_file"] == 5 * GIB
    assert reply["drain"]["unfiled_bytes"]["text"] == GIB // 2


def test_the_status_reports_the_bytes_that_the_volume_uses(vault: Path, store_policy, monkeypatch) -> None:
    """Catches a status without the volume's use: the sender cannot check a window against the cell's admission rule."""
    store_policy(growth_threshold=0.8, max_temporary_bytes=int(5.25 * GIB), max_hydrations=1)
    _volume(monkeypatch, total=20 * GIB, used=3 * GIB)

    assert _status(vault)["window_budget"]["volume"] == {"used_bytes": 3 * GIB}


@pytest.mark.parametrize("without", ["no store", "a disabled store"])
def test_a_cell_without_the_media_store_reports_no_window_budget(vault: Path, store_policy, without) -> None:
    """Catches a budget computed from a store that is off: the sender would size windows for room that never frees."""
    if without == "a disabled store":
        store_policy(enabled=False)
    import_id = _window(vault, {"scan.pdf": PDF})

    reply = _status(vault)

    assert "window_budget" not in reply
    assert [window["import_id"] for window in _windows(reply)] == [import_id]


@pytest.mark.parametrize("cause, mentioned", [("volume", "volume"), ("capacity", "hydration capacity")])
def test_an_input_that_cannot_be_read_makes_the_budgets_unknown_and_never_zero(
    vault: Path, store_policy, monkeypatch, cause, mentioned
) -> None:
    """Catches a failed read of the volume, or a policy with no hydration capacity, that reads as zero bytes of room."""
    store_policy(growth_threshold=0.8, **({"max_temporary_bytes": 10 * MIB, "max_hydrations": 1} if cause == "volume" else {}))
    if cause == "volume":
        def unreadable(_path):
            raise OSError("volume not mounted")

        monkeypatch.setattr(shutil, "disk_usage", unreadable)

    reply = _status(vault)["window_budget"]

    for name in ("window_budget", "reachable_budget", "effective_file_limit"):
        assert list(reply[name]) == ["unknown"] and mentioned in reply[name]["unknown"]
    if cause == "volume":
        assert list(reply["volume"]) == ["unknown"] and mentioned in reply["volume"]["unknown"]


# The windows


def test_a_landed_window_with_no_run_reports_every_member_but_the_series_file_as_unfiled(vault: Path) -> None:
    """Catches a window that reads as fully filed because no run has scanned it, or that counts the series file."""
    import_id = _window(vault, {"a.pdf": PDF, "b.pdf": PDF + b"b", "notes.md": b"# Notes\n"})

    reply = _status(vault)

    (window,) = _windows(reply)
    assert window["import_id"] == import_id and window["state"] == "landed"
    assert window["unfiled"] == {"count": 3, "bytes": len(PDF) + len(PDF + b"b") + len(b"# Notes\n")}
    assert window["unfiled_staged"] == window["unfiled"] and "residual" not in window
    assert sorted(member["path"] for member in window["members"]) == ["Field Notes/a.pdf", "Field Notes/b.pdf", "Field Notes/notes.md"]
    keep = reply["import_windows"]["keep_source"]
    assert keep["keep"] is True and "Keep the source folder" in keep["message"]


def test_a_landed_window_with_a_run_reports_only_the_members_that_the_run_did_not_file(vault: Path) -> None:
    """Catches a window that reads as fully filed because a run finished, though it left a member unselected."""
    import_id = _window(vault, {"filed.pdf": PDF, "left.pdf": PDF + b"left"})
    _file_all_but(vault, import_id, "Field Notes/left.pdf")

    (window,) = _windows(_status(vault))

    assert [member["path"] for member in window["members"]] == ["Field Notes/left.pdf"]
    assert window["unfiled"] == {"count": 1, "bytes": len(PDF + b"left")}


def test_a_series_that_is_fully_filed_and_discarded_stays_listed_with_no_member_and_stops_asking_to_keep_the_source(vault: Path) -> None:
    """Catches a status that keeps asking for the source after adoption filed everything, or that drops the window's receipt.

    The sender finds a window's receipt by its import ID in this list, and sends a window again when it is absent.
    """
    import_id = _window(vault, {"a.pdf": PDF, "b.pdf": PDF + b"b"})
    _file_all_but(vault, import_id)
    cloud_import_job.discard(vault, import_id)
    assert import_stage.read_residual(vault, import_id) == []

    reply = _status(vault)

    (window,) = _windows(reply)
    assert (window["import_id"], window["state"], window["residual"]) == (import_id, "discarded", {"count": 0, "bytes": 0})
    assert reply["import_windows"]["keep_source"] == {"keep": False, "message": None}


# The residual


def _discarded_with_two_unfiled(vault: Path) -> str:
    import_id = _window(vault, {"filed.pdf": PDF, "left.pdf": PDF + b"left", "also.pdf": PDF + b"also"})
    _file_all_but(vault, import_id, "Field Notes/left.pdf", "Field Notes/also.pdf")
    cloud_import_job.discard(vault, import_id)
    return import_id


def test_a_residual_record_lists_its_members_as_discarded_not_filed_and_asks_to_keep_the_source(vault: Path) -> None:
    """Catches a discarded window that drops out of the status, so the person deletes the only copy of a recording."""
    _discarded_with_two_unfiled(vault)

    reply = _status(vault)

    (window,) = _windows(reply)
    assert window["state"] == "discarded"
    assert {(member["path"], member["state"]) for member in window["members"]} == {
        ("Field Notes/left.pdf", "discarded, not filed"), ("Field Notes/also.pdf", "discarded, not filed")}
    assert reply["import_windows"]["keep_source"]["keep"] is True


def test_an_acknowledgement_moves_only_the_named_paths_under_acknowledged_with_their_time(vault: Path) -> None:
    """Catches an acknowledgement that clears the whole record, or that erases an entry so a mistake cannot be undone."""
    import_id = _discarded_with_two_unfiled(vault)

    cleared = commands.op_adoption_studio(vault, action="acknowledge", import_id=import_id, paths=["Field Notes/left.pdf"])

    assert cleared["cleared"] == ["Field Notes/left.pdf"] and cleared["remaining_members"] == 1
    (window,) = _windows(_status(vault))
    assert [member["path"] for member in window["members"]] == ["Field Notes/also.pdf"]
    assert window["residual"] == {"count": 1, "bytes": len(PDF + b"also")}
    (acknowledged,) = window["acknowledged"]["members"]
    assert (acknowledged["path"], acknowledged["bytes"]) == ("Field Notes/left.pdf", len(PDF + b"left"))
    assert datetime.fromisoformat(acknowledged["at"]).tzinfo is not None and window["acknowledged"]["count"] == 1


def test_a_window_with_every_member_acknowledged_stays_listed_and_stops_asking_to_keep_the_source(vault: Path) -> None:
    """Catches an acknowledged member that still counts as unfiled, or one that vanishes from the status."""
    import_id = _discarded_with_two_unfiled(vault)

    commands.op_adoption_studio(
        vault, action="acknowledge", import_id=import_id, paths=["Field Notes/left.pdf", "Field Notes/also.pdf"])

    reply = _status(vault)
    (window,) = _windows(reply)
    assert window["members"] == [] and window["acknowledged"]["count"] == 2
    assert reply["import_windows"]["keep_source"] == {"keep": False, "message": None}


def test_an_acknowledgement_without_paths_is_refused_and_the_record_keeps_every_member(vault: Path) -> None:
    """Catches an acknowledgement that clears a whole record when the caller names nothing."""
    import_id = _discarded_with_two_unfiled(vault)

    with pytest.raises(ValueError, match="IMPORT_REQUEST_INVALID"):
        commands.op_adoption_studio(vault, action="acknowledge", import_id=import_id)

    assert len(import_stage.read_residual(vault, import_id)) == 2


def test_an_acknowledgement_clears_a_walk_record_when_the_receipt_cannot_be_read(vault: Path) -> None:
    """Catches an acknowledgement that only knows the receipt, so a walked window can never be cleared."""
    import_id = _window(vault, {"scan.pdf": PDF, "other.pdf": PDF + b"other"})
    import_stage.receipt_path(vault, import_id).write_text("{ not a receipt", encoding="utf-8")
    cloud_import_job.discard(vault, import_id, walk_stage=True)

    commands.op_adoption_studio(vault, action="acknowledge", import_id=import_id, paths=["Field Notes/scan.pdf"])

    assert [member["path"] for member in import_stage.read_residual(vault, import_id)] == ["Field Notes/other.pdf"]
    assert [entry["path"] for entry in import_stage.read_walk(vault, import_id)["acknowledged"]] == ["Field Notes/scan.pdf"]
    assert import_stage.receipt_path(vault, import_id).read_text(encoding="utf-8") == "{ not a receipt"


def test_the_members_come_page_by_page_from_the_cursor(vault: Path, monkeypatch) -> None:
    """Catches a status that lists every member of a large series in one reply, or repeats one across pages."""
    monkeypatch.setattr(import_windows, "PAGE_MEMBERS", 2)
    _window(vault, {"a.pdf": 1, "b.pdf": 1, "c.pdf": 1})

    first = _status(vault)["import_windows"]
    second = _status(vault, after=first["next_after"])["import_windows"]

    listed = [member["path"] for page in (first, second) for window in page["windows"] for member in window["members"]]
    assert listed == ["Field Notes/a.pdf", "Field Notes/b.pdf", "Field Notes/c.pdf"]
    assert first["next_after"] is not None and second["next_after"] is None


def test_an_acknowledgement_takes_its_paths_as_a_list(vault: Path) -> None:
    """Catches a bare string that reads as one path per character, so a call clears nothing and says nothing."""
    import_id = _discarded_with_two_unfiled(vault)

    with pytest.raises(ValueError, match="IMPORT_REQUEST_INVALID"):
        commands.op_adoption_studio(vault, action="acknowledge", import_id=import_id, paths="Field Notes/left.pdf")

    assert len(import_stage.read_residual(vault, import_id)) == 2


@pytest.mark.parametrize("arguments", [{"after": "cursor"}, {"series_id": "series-a", "next_window_bytes": 1}])
def test_the_status_of_one_run_refuses_the_window_parameters_that_it_would_drop(vault: Path, arguments) -> None:
    """Catches a sender that sends a demand with a run ID, gets a run's status, and never learns that no demand was kept."""
    import_id = _window(vault, {"scan.pdf": PDF})
    run_id = adoption_run.start(vault, path=f"_Imports/{import_id}", today=consume.TODAY)["run_id"]

    with pytest.raises(ValueError, match="IMPORT_REQUEST_INVALID"):
        _status(vault, run_id=run_id, **arguments)


# The record of a window


def _damage_the_receipt(vault: Path, import_id: str, damage: str) -> None:
    receipt = import_stage.receipt_path(vault, import_id)
    if damage == "not json":
        receipt.write_text("{ damaged", encoding="utf-8")
        return
    document = json.loads(receipt.read_text(encoding="utf-8"))
    document["residual"]["members"] = "Field Notes/left.pdf"
    receipt.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("damage", ["not json", "members not a list"])
def test_a_discarded_window_whose_record_cannot_be_read_stays_in_the_status_and_asks_to_keep_the_source(
    vault: Path, damage
) -> None:
    """Catches a record that cannot be read dropping out of the status once its stage is gone: the person deletes the source."""
    import_id = _discarded_with_two_unfiled(vault)
    _damage_the_receipt(vault, import_id, damage)

    reply = _status(vault)

    (window,) = _windows(reply)
    assert (window["import_id"], window["state"]) == (import_id, "unreadable") and window["problem"]
    assert reply["import_windows"]["keep_source"]["keep"] is True


@pytest.mark.parametrize("stage_holds_files, budget_is_known", [(False, True), (True, False)])
def test_an_unreadable_window_makes_the_budget_unknown_only_while_its_stage_holds_files(
    vault: Path, store_policy, monkeypatch, stage_holds_files, budget_is_known
) -> None:
    """Catches a record that cannot be read poisoning the budget when no byte of its window is on the volume."""
    store_policy(growth_threshold=0.8, max_temporary_bytes=10 * MIB, max_hydrations=1)
    _volume(monkeypatch, total=20 * GIB, used=3 * GIB)
    import_id = _window(vault, {"scan.pdf": PDF}) if stage_holds_files else _discarded_with_two_unfiled(vault)
    _damage_the_receipt(vault, import_id, "not json")

    reply = _status(vault)

    assert ("bytes" in reply["window_budget"]["window_budget"]) is budget_is_known
    assert [window["state"] for window in _windows(reply)] == ["unreadable"]


def test_a_record_that_lists_a_path_twice_is_damaged_and_never_breaks_the_listing(vault: Path) -> None:
    """Catches a duplicate path that makes the status raise when it sorts the members, or that pages one member twice."""
    import_id = _discarded_with_two_unfiled(vault)
    receipt = import_stage.receipt_path(vault, import_id)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    first = document["residual"]["members"][0]
    document["residual"]["members"] = [first, {**first, "bytes": first["bytes"] + 1}]
    receipt.write_text(json.dumps(document), encoding="utf-8")

    (window,) = _windows(_status(vault))

    assert window["state"] == "unreadable"


def test_a_stage_walk_stays_the_record_of_its_window_when_the_receipt_can_be_read_again(vault: Path) -> None:
    """Catches a manifest that outranks the walk: the walked file reads as filed and the status stops asking to keep the source."""
    import_id = _window(vault, {"a.pdf": PDF})
    _file_all_but(vault, import_id)
    extra = vault / "_Imports" / import_id / "Field Notes" / "extra.pdf"  # No manifest lists it.
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(PDF + b"extra")
    receipt = import_stage.receipt_path(vault, import_id)
    readable = receipt.read_text(encoding="utf-8")
    receipt.write_text("{ damaged", encoding="utf-8")
    cloud_import_job.discard(vault, import_id, walk_stage=True)
    receipt.write_text(readable, encoding="utf-8")

    reply = _status(vault)

    (window,) = _windows(reply)
    assert window["state"] == "discarded"
    assert [member["path"] for member in window["members"]] == ["Field Notes/extra.pdf"]
    assert reply["import_windows"]["keep_source"]["keep"] is True


def test_a_landed_window_reports_the_renamed_members_that_a_refused_discard_found(vault: Path, monkeypatch) -> None:
    """Catches a refused discard whose only sign is in the receipt: the status goes on calling the window fine."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    _stop_after_the_rename_of(vault, monkeypatch, import_id)
    with pytest.raises(ArchiveRefused):
        cloud_import_job.discard(vault, import_id)

    (window,) = _windows(_status(vault))

    assert window["state"] == "landed"
    assert window["renames_pending"] == ["Knowledge Base/Sources/Imported/2026-10-27-scan.bin"]


def test_an_unreadable_window_reports_the_renamed_members_that_a_refused_walk_found(vault: Path, monkeypatch) -> None:
    """Catches a walking discard whose refusal is written where only a readable receipt is looked at."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    _stop_after_the_rename_of(vault, monkeypatch, import_id)
    import_stage.receipt_path(vault, import_id).write_text("{ damaged", encoding="utf-8")
    with pytest.raises(ArchiveRefused):
        cloud_import_job.discard(vault, import_id, walk_stage=True)

    (window,) = _windows(_status(vault))

    assert window["state"] == "unreadable"
    assert window["renames_pending"] == ["Knowledge Base/Sources/Imported/2026-10-27-scan.bin"]


def test_a_malformed_policy_value_makes_the_budgets_unknown_and_never_breaks_the_status(
    vault: Path, store_policy, monkeypatch
) -> None:
    """Catches a policy value that is no number crashing the status, so the sender cannot read any window figure."""
    store_policy(growth_threshold=0.8, max_temporary_bytes=10 * MIB, max_hydrations=1, max_object_bytes="5G")
    _volume(monkeypatch, total=20 * GIB, used=3 * GIB)

    reply = _status(vault)["window_budget"]

    assert "5G" in reply["window_budget"]["unknown"] and "unknown" in reply["classification"]
