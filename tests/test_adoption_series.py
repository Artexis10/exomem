"""A window of a series takes its collection root, its label and its link scope from the series that it belongs to."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
import test_adoption_consume as consume

from exomem import adoption_run, cloud_import_job, import_stage, state_paths

LABEL = "Field Notes"
SERIES = ["Recordings/a.md", "Recordings/b.md", "Notes/c.md"]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _window(vault: Path, files: dict[str, bytes], *, series: list[str] | None = SERIES) -> str:
    """A stage as the Cloud import Job leaves it: its files at the stage root, a receipt, and the series file.

    `files` are the window's own members. `series` is the path list that every window of the series carries;
    None leaves the series file out.
    """
    import_id = str(uuid.uuid4())
    stage = vault / "_Imports" / import_id
    document = json.dumps({"label": LABEL, "paths": series}).encode()
    members = []
    for path, data in {**files, **({} if series is None else {import_stage.SERIES_FILE: document})}.items():
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        (stage / path).write_bytes(data)
        members.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(members),
        declared_bytes=sum(member["size"] for member in members), ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(members)}, "members": members}))
    return import_id


def _start(vault: Path, import_id: str, **options) -> dict:
    return adoption_run.start(vault, path=f"_Imports/{import_id}", today=consume.TODAY, **options)


def _file_everything(vault: Path, import_id: str) -> None:
    """Adopt every artifact of the stage, then finish, so that the folders that filing emptied are removed."""
    run_id, _plan_id, _reply = consume._adopt(vault, f"_Imports/{import_id}")
    adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)
    stage = vault / "_Imports" / import_id
    assert not (stage / "Recordings").exists() and (stage / "Notes" / "plan.md").exists()


def _plan_items(vault: Path, import_id: str) -> dict[str, dict]:
    """The planned items of a run over the whole stage, by collection path."""
    run_id, _plan_id = consume._plan(vault, f"_Imports/{import_id}")
    items = adoption_run.AdoptionRunStore(vault).load(run_id)["plan"]["items"]
    return {item["collection_path"]: item for item in items}


def test_two_windows_of_one_series_get_one_root_and_the_series_label(vault: Path) -> None:
    first = _window(vault, {"Recordings/a.md": b"# A\n"})
    second = _window(vault, {"Recordings/b.md": b"# B\n", "Notes/c.md": b"# C\n"})

    runs = [_start(vault, first), _start(vault, second)]

    assert [run["collection"] for run in runs] == [
        {"label": LABEL, "root": f"_Imports/{first}"}, {"label": LABEL, "root": f"_Imports/{second}"}]
    assert set(_plan_items(vault, first)) == {"Recordings/a.md"}
    assert set(_plan_items(vault, second)) == {"Recordings/b.md", "Notes/c.md"}


def test_a_resend_window_with_the_first_series_file_files_its_member_at_the_first_series_collection_path(vault: Path) -> None:
    first = _window(vault, {"Recordings/a.md": b"# A\n", "Notes/c.md": b"# C\n"})
    resend = _window(vault, {"Recordings/a.md": b"# A\n"})

    original = _plan_items(vault, first)
    resent = _plan_items(vault, resend)

    assert set(resent) == {"Recordings/a.md"} <= set(original)
    assert _start(vault, resend)["collection"]["label"] == LABEL


def test_a_stage_whose_artifacts_are_all_filed_keeps_its_collection_root_from_its_series_file(vault: Path) -> None:
    import_id = _window(vault, {"Recordings/a.pdf": consume.PDF, "Notes/plan.md": b"# Plan\n"},
                        series=["Recordings/a.pdf", "Notes/plan.md"])
    first = _start(vault, import_id)
    _file_everything(vault, import_id)

    again = _start(vault, import_id)

    assert first["collection"] == {"label": LABEL, "root": f"_Imports/{import_id}"}
    assert again["collection"] == first["collection"]


def test_a_stage_without_a_series_file_keeps_its_collection_root_from_its_manifest_once_its_artifacts_are_filed(vault: Path) -> None:
    import_id = _window(vault, {"Recordings/a.pdf": consume.PDF, "Notes/plan.md": b"# Plan\n"}, series=None)
    first = _start(vault, import_id)
    _file_everything(vault, import_id)

    again = _start(vault, import_id)

    assert first["collection"] == {"label": import_id, "root": f"_Imports/{import_id}"}
    assert again["collection"] == first["collection"]


def test_a_series_file_without_a_usable_path_list_leaves_the_root_to_the_manifest_and_is_no_member_of_it(vault: Path) -> None:
    import_id = _window(vault, {"Fieldwork/a.md": b"# A\n"})
    (vault / "_Imports" / import_id / import_stage.SERIES_FILE).write_text(json.dumps({"label": LABEL, "paths": "a.md"}))

    run = _start(vault, import_id)

    assert run["collection"] == {"label": LABEL, "root": f"_Imports/{import_id}/Fieldwork"}


def test_a_bare_name_that_repeats_in_a_later_window_stays_ambiguous(vault: Path) -> None:
    """The first window sees one `todo.md`. The series holds a second one, in a window that has not landed."""
    first = _window(
        vault, {"Journal/day.md": b"# Day\n\nSee [[todo]].\n", "Notes/todo.md": b"# Todo\n"},
        series=["Journal/day.md", "Notes/todo.md", "Archive/todo.md"])

    links = _plan_items(vault, first)["Journal/day.md"]["links"]

    assert links["resolved"] == []
    assert [(link["raw"], link["reason"]) for link in links["unresolved"]] == [("todo", "ambiguous")]


@pytest.mark.parametrize("include_hidden", [False, True])
def test_the_series_file_is_never_an_item_of_a_run_and_never_a_hidden_file_to_report(vault: Path, include_hidden: bool) -> None:
    import_id = _window(vault, {"Notes/c.md": b"# C\n"})

    run = adoption_run.AdoptionRunStore(vault).load(_start(vault, import_id, include_hidden=include_hidden)["run_id"])

    assert [row["path"] for row in run["inventory"] if row.get("kind") != "folder"] == [f"_Imports/{import_id}/Notes/c.md"]
    assert run["hidden_files_skipped"] == 0


def test_a_path_list_relative_to_the_source_folder_leaves_the_root_and_the_collection_paths_to_the_manifest(vault: Path) -> None:
    """The stage holds the source folder, but its series file lists paths without it: no file lies at those paths."""
    import_id = _window(
        vault, {"Field Notes/Recordings/a.md": b"# A\n", "Field Notes/Notes/b.md": b"# B\n"},
        series=["Recordings/a.md", "Notes/b.md"])

    run = _start(vault, import_id)

    assert run["collection"] == {"label": LABEL, "root": f"_Imports/{import_id}/Field Notes"}
    assert set(_plan_items(vault, import_id)) == {"Recordings/a.md", "Notes/b.md"}


def test_a_path_list_that_names_no_member_of_the_stage_is_no_scope_for_a_link_and_no_root(vault: Path) -> None:
    import_id = _window(
        vault, {"Journal/day.md": b"# Day\n\nSee [[todo]].\n", "Notes/todo.md": b"# Todo\n"}, series=["Archive/todo.md"])

    assert _start(vault, import_id)["collection"]["root"] == f"_Imports/{import_id}"
    links = _plan_items(vault, import_id)["Journal/day.md"]["links"]
    assert links["unresolved"] == [] and len(links["resolved"]) == 1


def test_a_window_whose_artifacts_are_all_filed_reports_no_residual_file_for_its_series_file(vault: Path) -> None:
    import_id = _window(vault, {"Recordings/a.pdf": consume.PDF}, series=["Recordings/a.pdf"])
    run_id, _plan_id, _reply = consume._adopt(vault, f"_Imports/{import_id}")

    residual = adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)["finish"]["residual"]

    assert (residual["files"], residual["by_reason"], residual["discard"]) == (0, {}, None)
