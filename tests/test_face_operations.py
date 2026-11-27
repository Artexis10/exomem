"""The owner's face operations through the doors and the CLI, on real face stores.

Faces are seeded on synthetic vectors through the grouping code the face step uses, so no face model runs.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastmcp import Client

from exomem import __main__ as cli
from exomem import (
    cli_ops,
    commands,
    face_groups,
    face_models,
    face_naming,
    face_operations,
    face_scan,
    face_store,
    media_models,
    writer_lease,
)
from exomem import find as find_module
from exomem import server as server_module
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

MODEL = face_models.embedder_model_id()
WIDTH = int(media_models.model_spec("face-embed")["dimensions"])


def face(axis: int, *, slot: int = 0) -> face_groups.PageFace:
    vector = np.zeros(WIDTH, dtype=np.float32)
    vector[axis] = 1.0
    return face_groups.PageFace(None, (0.1 + 0.4 * slot, 0.2, 0.3, 0.3), 96.0, 0.99, vector)


def turn_on(vault: Path) -> None:
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at=datetime.now(UTC).isoformat(), surface="cli")


@pytest.fixture
def doors(vault, monkeypatch, tmp_path):
    """Call `inspect_memory` or `manage_memory` with one operation, as a host does."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    mcp = server_module.build_server(require_auth=False)

    def call(door: str, operation: str, arguments: dict) -> dict:
        async def run():
            async with Client(mcp) as client:
                result = await client.call_tool(
                    door, {"operation": operation, "arguments": arguments}, raise_on_error=False
                )
                body = result.structured_content
                if body is None:
                    return {"is_error": result.is_error, "text": " ".join(getattr(b, "text", "") for b in result.content)}
                return body["result"] if set(body) == {"result"} else body

        return asyncio.run(run())

    yield call
    writer_lease.reset_managers_for_tests()


def test_an_agent_asking_to_turn_faces_on_gets_the_disclosure_and_faces_stay_off(doors, vault) -> None:
    """Catches biometric processing enabled by an agent call that no person confirmed."""
    reply = doors("manage_memory", "faces.set", {"state": "on"})

    assert "exomem faces on" in reply["outcome"], reply
    assert "face vector" in reply["disclosure"]
    assert not face_store.enabled(vault)
    assert not face_store.store_dir(vault).exists()
    assert not face_scan.backfill_running(vault)


class Terminal(io.StringIO):
    """Standard input that reports a terminal, holding what the person types."""

    def isatty(self) -> bool:
        return True


def test_the_cli_turns_faces_on_only_with_the_typed_confirmation_and_records_the_cli(vault, monkeypatch, capsys) -> None:
    """Catches biometric processing enabled by one call that no disclosure preceded."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # no terminal

    refused = cli.main(["faces", "on", "--vault", str(vault)])
    assert refused == 2
    assert not face_store.enabled(vault)

    monkeypatch.setattr("sys.stdin", Terminal("yes\n"))
    wrong = cli.main(["faces", "on", "--vault", str(vault)])
    assert wrong == 1
    assert not face_store.enabled(vault)

    monkeypatch.setattr("sys.stdin", Terminal("turn faces on\n"))
    cli.main(["faces", "on", "--vault", str(vault)])
    face_scan.stop_backfill(vault, timeout=30)

    printed = capsys.readouterr().out
    assert "face vector" in printed  # the disclosure came first
    setting = face_store.read_setting(vault)
    assert setting.enabled and setting.consent_surface == "cli" and setting.consent_at


def test_every_faces_command_takes_the_vault_option_that_on_takes(vault, tmp_path, monkeypatch, capsys) -> None:
    """Catches `exomem faces off --vault` failing, which leaves faces on for an owner who asked to stop them."""
    turn_on(vault)
    other = tmp_path / "another-vault"
    other.mkdir()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(other))  # The option, not the environment, names the vault.

    stopped = cli.main(["faces", "off", "--vault", str(vault)])
    shown = cli.main(["faces", "status", "--vault", str(vault), "--json"])

    assert stopped == 0 and shown == 0, capsys.readouterr().err
    assert not face_store.enabled(vault)
    assert '"state": "off"' in capsys.readouterr().out


def test_an_audience_bound_session_is_refused_every_face_operation_before_the_store_opens(vault, monkeypatch) -> None:
    """Catches face data served to another principal."""
    turn_on(vault)
    face_groups.record_page(vault, "Photos/a.jpg", [face(0)], model_id=MODEL)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        if "/faces/" in str(database).replace("\\", "/"):
            opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        for action, arguments in (("status", {}), ("set", {"state": "off"}), ("name", {"group": 1, "name": "X"})):
            with pytest.raises(cli_ops.OpError) as refusal:
                commands.op_manage_faces(vault, action, **arguments)
            assert refusal.value.code == "FACES_OWNER_ONLY", action

    assert opened == []
    assert face_store.enabled(vault)


def test_a_vault_copied_into_another_account_arrives_with_faces_off(vault, tmp_path, monkeypatch) -> None:
    """Catches an opt-in that travels with the vault into another account."""
    turn_on(vault)
    face_groups.record_page(vault, "Photos/a.jpg", [face(0)], model_id=MODEL)
    copy = tmp_path / "other-account" / "vault"
    shutil.copytree(vault, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "other-account" / "state"))

    with library_scope():
        reply = commands.op_manage_faces(copy, "status")

    assert reply["state"] == "off"
    assert reply["counts"] == {"faces": 0, "groups": 0, "people": 0}
    assert not face_store.store_dir(copy).exists()


def test_status_lists_groups_largest_first_with_samples_a_page_at_a_time(doors, vault) -> None:
    """Catches a group list the owner cannot page through or show with read_media."""
    turn_on(vault)
    for number in range(5):
        face_groups.record_page(vault, f"Photos/ada-{number}.jpg", [face(0)], model_id=MODEL)
    for number in range(3):
        face_groups.record_page(vault, f"Photos/ben-{number}.jpg", [face(1)], model_id=MODEL)
    face_groups.record_page(vault, "Photos/cara.jpg", [face(2)], model_id=MODEL)

    first = doors("inspect_memory", "faces.status", {"limit": 2})
    second = doors("inspect_memory", "faces.status", {"limit": 2, "cursor": first["next_cursor"]})

    assert [group["face_count"] for group in first["groups"]] == [5, 3], first
    assert [len(group["samples"]) for group in first["groups"]] == [3, 3]
    assert first["groups"][0]["samples"][0] == {"face": 1, "page": "Photos/ada-0.jpg", "region": [0.1, 0.2, 0.3, 0.3]}
    assert [group["face_count"] for group in second["groups"]] == [1]
    assert second["next_cursor"] is None


def test_status_for_one_picture_lists_its_faces_and_their_people(doors, vault) -> None:
    """Catches a picture whose faces the owner cannot find to name or correct."""
    turn_on(vault)
    [ada] = face_groups.record_page(vault, "Photos/ada.jpg", [face(0)], model_id=MODEL)
    face_groups.record_page(vault, "Photos/both.jpg", [face(0), face(1, slot=1)], model_id=MODEL)
    doors("manage_memory", "faces.name", {"group": ada, "name": "Ada Fixture"})

    reply = doors("inspect_memory", "faces.status", {"page": "Photos/both.jpg"})

    assert [(entry["region"][0], entry["person"]) for entry in reply["faces"]] == [(0.1, "Ada Fixture"), (0.5, None)]


def test_two_groups_named_one_name_in_different_case_belong_to_one_person(doors, vault) -> None:
    """Catches one person split in two by the spelling of a name."""
    turn_on(vault)
    [first] = face_groups.record_page(vault, "Photos/front.jpg", [face(0)], model_id=MODEL)
    [second] = face_groups.record_page(vault, "Photos/side.jpg", [face(1)], model_id=MODEL)

    doors("manage_memory", "faces.name", {"group": first, "name": "Fixture Person"})
    doors("manage_memory", "faces.name", {"group": second, "name": "fixture person"})
    renamed = doors("manage_memory", "faces.name", {"person": "FIXTURE PERSON", "name": "Fixture Persona"})

    assert renamed["changed"] is True, renamed
    status = doors("inspect_memory", "faces.status", {})
    assert status["counts"]["people"] == 1
    assert {group["person"] for group in status["groups"]} == {"Fixture Persona"}


def test_a_hosted_deployment_refuses_turn_on_without_its_operator_setting(vault, monkeypatch, capsys) -> None:
    """Catches faces turned on where the operator never enabled them."""
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    monkeypatch.delenv(face_store.HOSTED_SETTING, raising=False)

    monkeypatch.setattr("sys.stdin", Terminal("turn faces on\n"))
    code = cli.main(["faces", "on", "--vault", str(vault)])

    assert code == 1
    assert "FACES_UNAVAILABLE" in capsys.readouterr().err
    assert not face_store.store_dir(vault).exists()


def test_a_cloud_account_that_never_turned_faces_on_is_told_the_deployment_does_not_run_them(
    doors, vault, monkeypatch
) -> None:
    """Catches a Cloud owner told to run `exomem faces on`, which this deployment would refuse anyway."""
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.delenv(face_store.HOSTED_SETTING, raising=False)

    with library_scope(), pytest.raises(cli_ops.OpError) as search:
        find_module.find(vault, query="", people=["Ada Fixture"], mode="keyword")
    turn_on_reply = doors("manage_memory", "faces.set", {"state": "on"})
    status = doors("inspect_memory", "faces.status", {})

    assert search.value.code == "FACES_UNAVAILABLE"
    assert "FACES_UNAVAILABLE" in str(turn_on_reply), turn_on_reply
    assert status["available"] is False
    assert face_operations.TURN_ON_COMMAND not in status["next_action"]
    assert "operator" in status["next_action"]
    # The customer's agent cannot set the operator's switch, so no reply names it.
    for reply in (str(search.value.as_public_dict()), str(turn_on_reply), str(status)):
        assert face_store.HOSTED_SETTING not in reply
    assert not face_store.store_dir(vault).exists()


def test_a_threshold_outside_its_real_range_is_refused_and_one_inside_is_kept(doors, vault) -> None:
    """Catches a similarity above 1 or a score above 1 that would silently stop all grouping or detection."""
    turn_on(vault)

    too_similar = doors("manage_memory", "faces.set", {"thresholds": {"join_similarity": 5.0}})
    too_sure = doors("manage_memory", "faces.set", {"thresholds": {"min_detection_score": 7}})
    kept = doors("manage_memory", "faces.set", {"thresholds": {"join_similarity": 0.6}})

    assert "INVALID_ARGUMENT" in str(too_similar) and "INVALID_ARGUMENT" in str(too_sure)
    assert face_groups.overrides(vault) == {"join_similarity": 0.6}
    assert kept["changed"] is True


def test_turning_faces_off_drops_the_face_vectors_held_in_memory(doors, vault) -> None:
    """Catches group centroids, which are face data, kept in a running service after the owner turns faces off."""
    turn_on(vault)
    face_groups.record_page(vault, "Photos/garden.jpg", [face(0)], model_id=MODEL)
    people_key = str(face_store.people_path(vault))
    assert any(key[0] == people_key for key in face_groups._CACHE)  # Grouping held them, so the check is live.

    doors("manage_memory", "faces.set", {"state": "off"})

    assert not any(key[0] == people_key for key in face_groups._CACHE)


def test_a_face_step_in_flight_loads_no_model_and_creates_no_store_after_off_or_delete(vault, monkeypatch) -> None:
    """Catches a picture already in the media worker bringing faces back after the owner stopped or erased them."""
    loads = []

    def load_while_the_owner_turns_faces_off():
        loads.append(True)
        # The owner turns faces off while the models load; `turn_off` then waits to unload them.
        with closing(face_store.connect_index(vault)) as index:
            face_store.record_opt_out(index)
        return object()

    monkeypatch.setattr(face_models, "load", load_while_the_owner_turns_faces_off)
    turn_on(vault)
    face_scan.unload()

    assert face_scan.models(vault) is None  # Off during the load: nothing stays loaded.
    assert face_scan.models(vault) is None and len(loads) == 1  # Off before the next step: nothing loads.

    turn_on(vault)
    face_operations.delete(vault)
    assert face_scan.models(vault) is None
    assert face_scan.commit(vault, "Photos/garden.jpg", [face(0)], mtime=1.0) is False
    assert not face_store.store_dir(vault).exists()
    assert len(loads) == 1


def seed_two_people_and_strangers(vault: Path) -> None:
    """Five pictures: two named people (one in two pictures, with a stranger) and two unnamed groups."""
    turn_on(vault)
    [ada] = face_groups.record_page(vault, "Photos/ada.jpg", [face(0)], model_id=MODEL)
    face_groups.record_page(vault, "Photos/ada-and-guest.jpg", [face(0), face(2, slot=1)], model_id=MODEL)
    [ben] = face_groups.record_page(vault, "Photos/ben.jpg", [face(1)], model_id=MODEL)
    face_groups.record_page(vault, "Photos/stranger.jpg", [face(3)], model_id=MODEL)
    face_groups.record_page(vault, "Photos/empty.jpg", [], model_id=MODEL)
    face_naming.name_group(vault, ada, "Ada Fixture")
    face_naming.name_group(vault, ben, "Ben Fixture")


def test_delete_removes_every_face_store_file_reports_what_it_held_and_turns_search_by_person_off(
    doors, vault
) -> None:
    """Catches an erasure that leaves a WAL or a directory behind, or reports counts it did not delete."""
    seed_two_people_and_strangers(vault)
    # A reader holds the face index open, so its WAL and shared-memory files exist while the delete runs.
    reader = sqlite3.connect(face_store.index_path(vault))
    reader.execute("SELECT COUNT(*) FROM faces").fetchone()
    assert Path(f"{face_store.index_path(vault)}-wal").is_file()

    reply = doors("manage_memory", "faces.delete", {})
    reader.close()

    assert reply["deletion"] == "deleted", reply
    assert reply["counts"] == {"faces": 5, "groups": 4, "people": 2, "pages": 4}
    assert not face_store.store_dir(vault).exists()
    with library_scope(), pytest.raises(cli_ops.OpError) as refusal:
        find_module.find(vault, query="", people=["Ada Fixture"], mode="keyword")
    assert refusal.value.code == "FACES_OFF"


def test_a_delete_that_cannot_remove_a_file_reports_partial_and_a_repeat_finishes_it(
    doors, vault, monkeypatch
) -> None:
    """Catches a partial delete reported as complete."""
    seed_two_people_and_strangers(vault)
    real_unlink = Path.unlink

    def people_store_held(path, *args, **kwargs):
        if path.name == "people.sqlite":
            raise PermissionError(13, "Permission denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", people_store_held)
    first = doors("manage_memory", "faces.delete", {})
    monkeypatch.setattr(Path, "unlink", real_unlink)
    repeat = doors("manage_memory", "faces.delete", {})

    assert first["deletion"] == "partial", first
    assert first["remaining"] == ["faces/people.sqlite", "faces"]
    assert "faces/people.sqlite" in first["outcome"]
    assert repeat["deletion"] == "deleted", repeat
    assert repeat["counts"] == first["counts"] == {"faces": 5, "groups": 4, "people": 2, "pages": 4}
    assert not face_store.store_dir(vault).exists()


@pytest.mark.parametrize("damage", ["garbage bytes", "a newer schema"])
def test_delete_erases_a_face_index_this_release_cannot_open(doors, vault, damage) -> None:
    """Catches a damaged or newer face index that stops the owner from deleting their face data."""
    seed_two_people_and_strangers(vault)
    index_file = face_store.index_path(vault)
    if damage == "garbage bytes":
        index_file.write_bytes(b"not a database, just bytes" * 200)
        for suffix in ("-wal", "-shm"):
            Path(f"{index_file}{suffix}").unlink(missing_ok=True)
    else:
        with closing(sqlite3.connect(index_file)) as index:
            index.execute(f"PRAGMA user_version = {face_store.SCHEMA_VERSION + 1}")

    reply = doors("manage_memory", "faces.delete", {})

    assert reply["deletion"] == "deleted", reply
    assert not face_store.store_dir(vault).exists()
    assert reply["counts"] == "unknown" or reply["counts"]["people"] == 2


@pytest.mark.parametrize("damage", ["garbage bytes", "a newer schema"])
def test_status_on_a_face_index_this_release_cannot_open_names_it_and_points_to_delete(doors, vault, damage) -> None:
    """Catches a status that fails on a damaged face index, which leaves the owner no way to learn the fix."""
    seed_two_people_and_strangers(vault)
    index_file = face_store.index_path(vault)
    if damage == "garbage bytes":
        index_file.write_bytes(b"not a database, just bytes" * 200)
        for suffix in ("-wal", "-shm"):
            Path(f"{index_file}{suffix}").unlink(missing_ok=True)
    else:
        with closing(sqlite3.connect(index_file)) as index:
            index.execute(f"PRAGMA user_version = {face_store.SCHEMA_VERSION + 1}")

    status = doors("inspect_memory", "faces.status", {})

    assert status["state"] == "unreadable", status
    assert status["available"] is False
    assert "faces.delete" in status["next_action"]


def test_the_delete_result_and_status_name_what_the_delete_cannot_reach_and_when_it_ends(doors, vault) -> None:
    """Catches a delete that lets the owner believe no copy of their face data remains anywhere."""
    seed_two_people_and_strangers(vault)

    reply = doors("manage_memory", "faces.delete", {})
    status = doors("inspect_memory", "faces.status", {})

    residues = {residue["residue"]: residue for residue in reply["residues"]}
    assert set(residues) == {"older backups", "freed disk blocks"}  # A self-hosted account has no Cloud snapshot.
    assert "keep settings" in residues["older backups"]["ends"]
    assert "encryption protects" not in residues["freed disk blocks"]["ends"]
    assert status["state"] == "off"
    assert status["last_deletion"]["counts"] == reply["counts"]
    assert status["residues"] == reply["residues"]


def test_a_cloud_cell_delete_also_names_the_running_backups_snapshot(doors, vault, monkeypatch) -> None:
    """Catches a Cloud owner not told that a backup in progress still holds the deleted store."""
    seed_two_people_and_strangers(vault)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv(face_store.HOSTED_SETTING, "1")

    reply = doors("manage_memory", "faces.delete", {})

    residues = [residue["residue"] for residue in reply["residues"]]
    assert residues == ["older backups", "a running Cloud cell backup's volume snapshot", "freed disk blocks"]
