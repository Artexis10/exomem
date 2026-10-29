"""A folder with more files than one run holds adopts page by page, under one collection."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from exomem import adoption_run, cloud_import_job, state_migration, state_paths

TODAY = dt.date(2026, 10, 27)
PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"


def _vault(tmp_path: Path) -> Path:
    """A vault with a Knowledge Base whose state placement is settled, as after its first start."""
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    (kb / "Notes").mkdir(parents=True)
    (kb / "Sources").mkdir()
    (kb / "Sources" / "index.md").write_text("# Sources - Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8")
    (kb / "index.md").write_text("# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n", encoding="utf-8")
    (kb / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    assert state_migration.bootstrap_fresh_state(vault)
    return vault


def _folder(vault: Path, files: dict[str, bytes], name: str = "Archive") -> str:
    for rel, data in files.items():
        (vault / name / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / name / rel).write_bytes(data)
    return name


def _stage(vault: Path, files: dict[str, bytes]) -> str:
    """A stage laid out as the Cloud import Job leaves it, with the receipt manifest that lists its files."""
    import_id = str(uuid.uuid4())
    members = []
    for rel, data in files.items():
        path = vault / "_Imports" / import_id / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        members.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(files),
        declared_bytes=sum(len(data) for data in files.values()), ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(files)}, "members": members}))
    return f"_Imports/{import_id}"


def _plan_page(vault: Path, page: dict, folder: str) -> str:
    """Select everything the page holds and plan it; returns the plan id."""
    adoption_run.select(vault, run_id=page["run_id"], include=[folder])
    planned = adoption_run.plan(vault, run_id=page["run_id"], today=TODAY)
    while planned.get("continuation"):
        planned = adoption_run.plan(vault, run_id=page["run_id"], continuation=planned["continuation"], today=TODAY)
    return planned["plan"]["plan_id"]


def _apply_page(vault: Path, page: dict, folder: str) -> dict:
    plan_id = _plan_page(vault, page, folder)
    reply = adoption_run.apply(vault, run_id=page["run_id"], plan_id=plan_id, today=TODAY)
    while reply.get("continuation"):
        reply = adoption_run.apply(
            vault, run_id=page["run_id"], plan_id=plan_id, continuation=reply["continuation"], today=TODAY)
    return reply


def _adopt_every_page(vault: Path, folder: str, *, cap: int, collection: str | None = None) -> list[dict]:
    """Adopt a folder page by page the way an agent does; returns each page's start reply."""
    pages: list[dict] = []
    page = adoption_run.start(vault, path=folder, collection=collection, max_candidates=cap, today=TODAY)
    while True:
        pages.append(page)
        _apply_page(vault, page, folder)
        if not page.get("next_after"):
            return pages
        page = adoption_run.start(vault, path=folder, after=page["next_after"], today=TODAY)


def _collection_paths(vault: Path, pages: list[dict]) -> list[str]:
    runs = [adoption_run.AdoptionRunStore(vault).load(page["run_id"]) for page in pages]
    return [item["collection_path"] for run in runs for item in run["plan"]["items"]]


def test_a_flat_folder_larger_than_the_cap_is_covered_once_page_by_page_with_artifacts_first(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    artifacts = {f"scan-{n}.pdf": PDF + bytes([n]) for n in range(6)}
    notes = {f"note-{n}.md": f"# Note {n}\n\nInvented note {n}.\n".encode() for n in range(3)}
    folder = _folder(vault, {**notes, **artifacts})

    pages = _adopt_every_page(vault, folder, cap=4)

    assert [len(page["inventory"]) for page in pages] == [4, 4, 1]
    assert [page.get("next_after") is not None for page in pages] == [True, True, False]
    assert sorted(_collection_paths(vault, pages)) == sorted([*artifacts, *notes])
    forms = [row["form"] for page in pages for row in page["inventory"]]
    assert forms == ["artifact"] * 6 + ["text"] * 3
    assert {(page["collection"]["label"], page["collection"]["root"]) for page in pages} == {("Archive", "Archive")}


def test_consuming_a_top_level_file_does_not_change_the_next_page_s_collection(tmp_path: Path) -> None:
    """With one top-level folder the stage's collection is that folder; consuming a loose file would change it."""
    vault = _vault(tmp_path)
    stage = _stage(vault, {
        "Anchor.pdf": PDF, "Photos/a.pdf": PDF + b"a", "Photos/b.pdf": PDF + b"b", "Photos/c.pdf": PDF + b"c"})

    first = adoption_run.start(vault, path=stage, collection="Field Notes", max_candidates=1, today=TODAY)
    applied = _apply_page(vault, first, stage)
    assert applied["consumed"] == 1 and not (vault / stage / "Anchor.pdf").exists()
    pages = [first]
    while pages[-1].get("next_after"):
        page = adoption_run.start(vault, path=stage, after=pages[-1]["next_after"], today=TODAY)
        _apply_page(vault, page, stage)
        pages.append(page)

    assert sorted(_collection_paths(vault, pages)) == ["Anchor.pdf", "Photos/a.pdf", "Photos/b.pdf", "Photos/c.pdf"]
    assert {(page["collection"]["label"], page["collection"]["root"]) for page in pages} == {("Field Notes", stage)}


def test_a_cursor_the_server_did_not_issue_is_refused_and_a_used_one_returns_its_page(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = _folder(vault, {f"scan-{n}.pdf": PDF + bytes([n]) for n in range(3)})
    first = adoption_run.start(vault, path=folder, max_candidates=2, today=TODAY)

    forged = first["next_after"][:-1] + ("0" if first["next_after"][-1] != "0" else "1")
    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.start(vault, path=folder, after=forged, today=TODAY)
    assert refused.value.code == "INVALID_CONTINUATION"
    with pytest.raises(adoption_run.AdoptionRunError) as elsewhere:
        adoption_run.start(vault, path="Elsewhere", after=first["next_after"], today=TODAY)
    assert elsewhere.value.code == "INVALID_CONTINUATION"

    second = adoption_run.start(vault, path=folder, after=first["next_after"], today=TODAY)
    again = adoption_run.start(vault, path=folder, after=first["next_after"], today=TODAY)

    assert again["run_id"] == second["run_id"] and len(adoption_run.status(vault)["runs"]) == 2


def test_a_name_that_two_files_share_stays_unresolved_and_an_alias_alone_never_resolves(tmp_path: Path) -> None:
    """Page one sees one `target.md` and one alias; the folder holds a second of each on a later page."""
    vault = _vault(tmp_path)
    folder = _folder(vault, {
        "a.md": b"# Index\n\nSee [[target]], [[Nick]] and [[solo]].\n",
        "x/nick-home.md": b"---\naliases: [Nick]\n---\n# Nick home\n\nInvented page.\n",
        "x/solo.md": b"# Solo\n\nThe only page of that name.\n",
        "x/target.md": b"# Target\n\nFirst page of that name.\n",
        "y/nicky.md": b"---\naliases: [Nick]\n---\n# Nicky\n\nInvented page.\n",
        "y/target.md": b"# Target\n\nSecond page of that name.\n",
    })

    first = adoption_run.start(vault, path=folder, max_candidates=4, today=TODAY)
    _plan_page(vault, first, folder)

    index = next(i for i in adoption_run.AdoptionRunStore(vault).load(first["run_id"])["plan"]["items"]
                 if i["collection_path"] == "a.md")
    assert {link["raw"] for link in index["links"]["resolved"]} == {"solo"}
    assert {(link["raw"], link["reason"]) for link in index["links"]["unresolved"]} == {
        ("target", "ambiguous"), ("Nick", "not-found")}


def test_a_later_page_resolves_a_unique_name_through_the_page_that_imported_it(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = _folder(vault, {
        "a.md": b"# First\n\nNothing to link.\n",
        "x/solo.md": b"# Solo\n\nThe only page of that name.\n",
        "y/b.md": b"# Second\n\nSee [[solo]] and [[a]].\n",
    })
    first = adoption_run.start(vault, path=folder, max_candidates=2, today=TODAY)
    _apply_page(vault, first, folder)
    second = adoption_run.start(vault, path=folder, after=first["next_after"], today=TODAY)

    _plan_page(vault, second, folder)

    planned = adoption_run.AdoptionRunStore(vault).load(second["run_id"])["plan"]["items"]
    links = next(i for i in planned if i["collection_path"] == "y/b.md")["links"]
    assert {link["raw"] for link in links["resolved"]} == {"solo", "a"} and links["unresolved"] == []


def test_plan_refuses_a_page_while_an_earlier_page_is_unapplied_and_then_destinations_do_not_collide(
        tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = _folder(vault, {"x/report.pdf": PDF + b"x", "y/report.pdf": PDF + b"y"})
    first = adoption_run.start(vault, path=folder, max_candidates=1, today=TODAY)
    second = adoption_run.start(vault, path=folder, after=first["next_after"], today=TODAY)
    first_plan = _plan_page(vault, first, folder)
    adoption_run.select(vault, run_id=second["run_id"], include=[folder])

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.plan(vault, run_id=second["run_id"], today=TODAY)
    assert refused.value.code == "EARLIER_PAGE_UNAPPLIED" and first["run_id"] in refused.value.reason

    adoption_run.apply(vault, run_id=first["run_id"], plan_id=first_plan, today=TODAY)
    applied = _apply_page(vault, second, folder)

    targets = {o["artifact_path"] for run_id in (first["run_id"], second["run_id"])
               for o in adoption_run.AdoptionRunStore(vault).load(run_id)["outcomes"].values()}
    assert len(targets) == 2 and all(o["status"] == "applied" for o in applied["outcomes"].values())


def test_finish_of_a_page_that_later_pages_follow_does_not_offer_to_discard_their_files(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = b"# Field plan\n\nInvented plan for the week.\n"
    stage = _stage(vault, {"a.pdf": PDF + b"a", "b.pdf": PDF + b"b", "plan.md": note})
    first = adoption_run.start(vault, path=stage, collection="Field Notes", max_candidates=2, today=TODAY)
    _apply_page(vault, first, stage)

    early = adoption_run.finish(vault, run_id=first["run_id"], today=TODAY)["finish"]["residual"]

    assert (early["more_pages"], early["files"], early["discard"]) == (True, 1, None)
    last = adoption_run.start(vault, path=stage, after=first["next_after"], today=TODAY)
    _apply_page(vault, last, stage)

    final = adoption_run.finish(vault, run_id=last["run_id"], today=TODAY)["finish"]["residual"]

    assert final["more_pages"] is False and final["by_reason"] == {"text": {"files": 1, "bytes": len(note)}}
    assert stage.removeprefix("_Imports/") in final["discard"]
