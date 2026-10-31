"""A large staged artifact adopts in one streamed pass, a file over the ceiling stays in the residual, and a chunk that does not fit waits."""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import shutil
import tracemalloc
import uuid
from pathlib import Path

import pytest
import test_adoption_consume as consume

from exomem import (
    _held_fs_posix,
    adoption_run,
    client_artifacts,
    cloud_import_job,
    state_migration,
    state_paths,
)

TODAY = dt.date(2026, 10, 27)
MIB = 1024 * 1024
CEILING_ENV = "EXOMEM_ADOPTION_MAX_FILE_BYTES"


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


def _sparse(path: Path, size: int, mark: bytes) -> None:
    """A file of `size` bytes that takes no memory to build: a marked head and tail, a hole between."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(mark)
        handle.seek(size - len(mark))
        handle.write(mark)


def _digest(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _stage(vault: Path, files: dict[str, int | bytes]) -> str:
    """A stage laid out as the Cloud import Job leaves it; an int value is a sparse file of that size."""
    import_id = str(uuid.uuid4())
    members = []
    for rel, content in files.items():
        path = vault / "_Imports" / import_id / rel
        if isinstance(content, int):
            _sparse(path, content, rel.encode())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        members.append({"path": rel, "size": path.stat().st_size, "sha256": _digest(path)})
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(files),
        declared_bytes=sum(member["size"] for member in members), ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(files)}, "members": members}))
    return f"_Imports/{import_id}"


def _plan(vault: Path, run: dict, stage: str) -> str:
    adoption_run.select(vault, run_id=run["run_id"], include=[stage])
    return adoption_run.plan(vault, run_id=run["run_id"], today=TODAY)["plan"]["plan_id"]


def _apply_all(vault: Path, run: dict, plan_id: str) -> dict:
    reply = adoption_run.apply(vault, run_id=run["run_id"], plan_id=plan_id, today=TODAY)
    while reply.get("continuation"):
        reply = adoption_run.apply(
            vault, run_id=run["run_id"], plan_id=plan_id, continuation=reply["continuation"], today=TODAY)
    return reply


def test_the_ceiling_clears_the_client_pull_cap_on_a_cloud_cell_and_an_operator_can_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.delenv(CEILING_ENV, raising=False)
    default = client_artifacts.adoption_ceiling()
    assert default > client_artifacts.fetch_budget().max_file_bytes
    monkeypatch.setenv(CEILING_ENV, str(3 * MIB))
    assert client_artifacts.adoption_ceiling() == 3 * MIB
    monkeypatch.setenv(CEILING_ENV, "plenty")
    assert client_artifacts.adoption_ceiling() == default


def test_a_cloud_cell_adopts_a_staged_artifact_over_the_client_pull_cap_without_holding_it_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    vault = _vault(tmp_path)
    size = client_artifacts.fetch_budget().max_file_bytes + 5 * MIB
    stage = _stage(vault, {"Recordings/long-take.bin": size})
    staged = next((vault / stage).rglob("long-take.bin"))
    expected = _digest(staged)
    run = adoption_run.start(vault, path=stage, today=TODAY)
    assert [(row["path"], row["eligible"]) for row in run["inventory"]] == [(f"{stage}/Recordings/long-take.bin", True)]
    plan_id = _plan(vault, run, stage)

    tracemalloc.start()
    try:
        reply = _apply_all(vault, run, plan_id)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    outcome = next(iter(reply["outcomes"].values()))
    assert outcome["status"] == "applied"
    assert _digest(vault / outcome["artifact_path"]) == expected and not staged.exists()
    assert peak < size // 4, f"apply held {peak} bytes while filing a {size} byte file"


def test_an_artifact_that_changed_after_the_plan_is_refused_at_the_copy_and_its_neighbour_still_lands(
    tmp_path: Path,
) -> None:
    """The copy hashes the bytes it reads, so an edit that keeps the size is caught there, not after publication."""
    vault = _vault(tmp_path)
    stage = _stage(vault, {"scans/edited.bin": b"E" * 4096, "scans/intact.bin": b"I" * 4096})
    run = adoption_run.start(vault, path=stage, today=TODAY)
    plan_id = _plan(vault, run, stage)
    edited, intact = (next((vault / stage).rglob(name)) for name in ("edited.bin", "intact.bin"))
    edited.write_bytes(b"X" * 4096)

    reply = _apply_all(vault, run, plan_id)

    outcomes = {Path(path).name: outcome for path, outcome in reply["outcomes"].items()}
    assert outcomes["edited.bin"]["status"] == "failed" and outcomes["edited.bin"]["code"] == "SOURCE_CHANGED"
    assert outcomes["intact.bin"]["status"] == "applied" and not intact.exists()
    assert edited.read_bytes() == b"X" * 4096
    kept = [path.name for path in (vault / "Knowledge Base" / "Sources").rglob("*.bin")]
    assert kept == [Path(outcomes["intact.bin"]["artifact_path"]).name]


def test_a_staged_file_over_the_ceiling_stays_in_the_residual_with_a_code_that_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CEILING_ENV, str(2 * MIB))
    vault = _vault(tmp_path)
    stage = _stage(vault, {"clips/short.bin": b"S" * 4096, "clips/long.bin": 3 * MIB})
    run = adoption_run.start(vault, path=stage, today=TODAY)
    rows = {Path(row["path"]).name: row for row in run["inventory"]}
    assert (rows["long.bin"]["eligible"], rows["long.bin"]["reason"]) == (False, "OVER_ADOPTION_CEILING")
    plan_id = _plan(vault, run, stage)
    _apply_all(vault, run, plan_id)

    residual = adoption_run.finish(vault, run_id=run["run_id"], today=TODAY)["finish"]["residual"]

    assert residual["by_reason"] == {"over_ceiling": {"files": 1, "bytes": 3 * MIB}}
    assert residual["ceiling_bytes"] == 2 * MIB
    assert next((vault / stage).rglob("long.bin")).stat().st_size == 3 * MIB


def test_apply_stops_before_a_chunk_that_does_not_fit_the_free_space_and_a_later_call_continues_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve, size = 100_000, 4000
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(reserve))
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 1)
    vault = _vault(tmp_path)
    capacity = [reserve + 2 * size]  # after one filed copy a second fits the space, but not the space and the reserve

    def free_bytes() -> int:
        filed = sum(path.stat().st_size for path in (vault / "Knowledge Base" / "Sources").rglob("*") if path.is_file())
        return capacity[0] - filed

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(capacity[0] * 2, 0, free_bytes()))
    stage = _stage(vault, {f"scans/{n}.bin": bytes([65 + n]) * size for n in range(3)})
    run = adoption_run.start(vault, path=stage, consume=False, today=TODAY)
    plan_id = _plan(vault, run, stage)

    stopped = adoption_run.apply(vault, run_id=run["run_id"], plan_id=plan_id, today=TODAY)

    items = [item["original_path"] for item in stopped["plan"]["items"]]
    assert [(stopped["outcomes"].get(path) or {}).get("status") for path in items] == ["applied", None, None]
    assert len(list((vault / "Knowledge Base" / "Sources").rglob("*.bin"))) == 1
    space = stopped.get("apply_stop")
    assert space is not None and space["code"] == "INSUFFICIENT_SPACE"
    assert (space["chunk_bytes"], space["reserve_bytes"], space["free_bytes"]) == (size, reserve, free_bytes())
    assert stopped["phase"] == "applying"
    assert stopped["continuation"]  # the reply still carries one, so the next action must say it cannot be followed yet
    assert {action["action"]: action["status"] for action in stopped["next_actions"]} == {"apply": "blocked"}

    capacity[0] += 10 * size
    resumed = adoption_run.apply(vault, run_id=run["run_id"], plan_id=plan_id, today=TODAY)

    assert len(list((vault / "Knowledge Base" / "Sources").rglob("*.bin"))) == 3
    assert "apply_stop" not in resumed and resumed["phase"] == "applied"
    assert {action["action"]: action["status"] for action in resumed["next_actions"]} == {"finish": "available"}


def test_a_continuation_that_stopped_before_removing_a_chunk_s_staged_copies_removes_them_when_it_is_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve, size = 100_000, 4000
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(reserve))
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 1)
    vault = _vault(tmp_path)
    capacity = [reserve + 2 * size]  # the first call files one copy, then stops with a continuation

    def free_bytes() -> int:
        filed = sum(path.stat().st_size for path in (vault / "Knowledge Base" / "Sources").rglob("*") if path.is_file())
        return capacity[0] - filed

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(capacity[0] * 2, 0, free_bytes()))
    stage = _stage(vault, {f"scans/{n}.bin": bytes([65 + n]) * size for n in range(2)})
    # A rename leaves no staged copy to remove, so these files sit on another device and file by copy,
    # which the space check prices at their full size.
    monkeypatch.setattr(
        _held_fs_posix, "_rename_noreplace",
        consume._rename_across_devices(_held_fs_posix._rename_noreplace, "0.bin", "1.bin"))
    monkeypatch.setattr(adoption_run.import_rename, "_device_of", lambda _root, _relative_path: -1)
    run = adoption_run.start(vault, path=stage, today=TODAY)
    plan_id = _plan(vault, run, stage)
    stopped = adoption_run.apply(vault, run_id=run["run_id"], plan_id=plan_id, today=TODAY)
    capacity[0] += 10 * size

    def stop(*_arguments, **_keywords):
        raise KeyboardInterrupt

    # The second chunk commits; the call dies before it removes that chunk's staged copy.
    with monkeypatch.context() as stopping:
        stopping.setattr(adoption_run.adoption_consume, "remove_staged", stop)
        with pytest.raises(KeyboardInterrupt):
            adoption_run.apply(
                vault, run_id=run["run_id"], plan_id=plan_id, continuation=stopped["continuation"], today=TODAY)

    repeated = adoption_run.apply(
        vault, run_id=run["run_id"], plan_id=plan_id, continuation=stopped["continuation"], today=TODAY)

    assert repeated["phase"] == "applied" and repeated["progress"]["failed"] == 0
    assert all(outcome.get("consumed") is True for outcome in repeated["outcomes"].values())
    assert not [path for path in (vault / stage).rglob("*.bin")]
