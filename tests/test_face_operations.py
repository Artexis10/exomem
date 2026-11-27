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
    face_scan,
    face_store,
    media_models,
    writer_lease,
)
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


def test_the_cli_turns_faces_on_only_with_the_typed_confirmation_and_records_the_cli(vault, monkeypatch, capsys) -> None:
    """Catches biometric processing enabled by one call that no disclosure preceded."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # no terminal

    refused = cli.main(["faces", "on", "--vault", str(vault)])
    assert refused == 2
    assert not face_store.enabled(vault)

    wrong = cli.main(["faces", "on", "--vault", str(vault), "--confirm", "yes"])
    assert wrong == 1
    assert not face_store.enabled(vault)

    cli.main(["faces", "on", "--vault", str(vault), "--confirm", "turn faces on"])
    face_scan.stop_backfill(vault, timeout=30)

    printed = capsys.readouterr().out
    assert "face vector" in printed  # the disclosure came first
    setting = face_store.read_setting(vault)
    assert setting.enabled and setting.consent_surface == "cli" and setting.consent_at


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

    code = cli.main(["faces", "on", "--vault", str(vault), "--confirm", "turn faces on"])

    assert code == 1
    assert "FACES_UNAVAILABLE" in capsys.readouterr().err
    assert not face_store.store_dir(vault).exists()
