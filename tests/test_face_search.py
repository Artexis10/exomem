"""Search by person: `search_memory.people` through the public wire, on real face stores.

Faces are seeded on synthetic vectors through the grouping and naming code the face step uses, so no
face model runs. Pictures are real sidecars from the preserve path with their capture fields stated.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastmcp import Client

from exomem import (
    cli_ops,
    commands,
    face_groups,
    face_models,
    face_naming,
    face_store,
    freshness,
    lexstore,
    media_models,
    preserve,
    writer_lease,
)
from exomem import find as find_module
from exomem import server as server_module
from exomem.capture_metadata import CaptureFacts
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

pytestmark = pytest.mark.skipif(not lexstore.fts5_available(), reason="SQLite build lacks FTS5")

MODEL = face_models.embedder_model_id()
WIDTH = int(media_models.model_spec("face-embed")["dimensions"])
JUNE = {"page.capture_date": {"$between": ["2024-06-01", "2024-06-30"]}}


def unit(axis: int) -> np.ndarray:
    vector = np.zeros(WIDTH, dtype=np.float32)
    vector[axis] = 1.0
    return vector


def face(axis: int, *, slot: int = 0, frame_ts: float | None = None) -> face_groups.PageFace:
    return face_groups.PageFace(frame_ts, (0.1 + 0.4 * slot, 0.2, 0.3, 0.3), 96.0, 0.99, unit(axis))


def picture(vault: Path, name: str, date: str, *, category: str = "image") -> tuple[str, str]:
    """Preserve one picture with a stated capture date; returns its original's path and its page."""
    suffix = ".mp4" if category == "video" else ".jpg"
    stored = preserve.preserve_bytes(
        vault, scope="Photos", category=category, filename=name + suffix,
        data=b"\xff\xd8" + name.encode(), content_type="video/mp4" if category == "video" else "image/jpeg",
    )
    sidecar = vault / stored.sidecar_path
    text = sidecar.read_text(encoding="utf-8").rstrip("\n") + "\nA day at the lake.\n"
    facts = CaptureFacts(time=f"{date}T09:00:00", date=date, lat=None, lon=None, make="Acme", width=64, height=48)
    sidecar.write_text(preserve.set_capture_fields(text, facts), encoding="utf-8")
    return stored.path, stored.sidecar_path


def turn_on(vault: Path) -> None:
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at=datetime.now(UTC).isoformat(), surface="cli")


def refresh(vault: Path) -> None:
    find_module.clear_cache()
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)


@pytest.fixture
def album(vault: Path) -> dict[str, tuple[str, str]]:
    """Faces on: Ada and Ben across photos, a photo of nobody, and Cara in one video's keyframe.

    Returns each picture's original and page by name.
    """
    pictures = {
        "ada-june": picture(vault, "ada-june", "2024-06-10"),
        "ada-and-ben-june": picture(vault, "ada-and-ben-june", "2024-06-20"),
        "ada-march": picture(vault, "ada-march", "2024-03-03"),
        "ben-june": picture(vault, "ben-june", "2024-06-12"),
        "nobody-june": picture(vault, "nobody-june", "2024-06-15"),
        "cara-video": picture(vault, "cara-video", "2024-06-18", category="video"),
    }
    turn_on(vault)
    faces = {
        "ada-june": [face(0)],
        "ada-and-ben-june": [face(0), face(1, slot=1)],
        "ada-march": [face(0)],
        "ben-june": [face(1)],
        "nobody-june": [],
        # Cara appears only in the video's second keyframe.
        "cara-video": [face(2, frame_ts=4.0)],
    }
    group_of = {}
    for name, found in faces.items():
        groups = face_groups.record_page(vault, pictures[name][0], found, model_id=MODEL)
        if len(found) == 1:
            group_of[name] = groups[0]
    face_naming.name_group(vault, group_of["ada-june"], "Ada Fixture")
    face_naming.name_group(vault, group_of["ben-june"], "Ben Fixture")
    face_naming.name_group(vault, group_of["cara-video"], "Cara Fixture")
    refresh(vault)
    return pictures


@pytest.fixture
def wire(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    mcp = server_module.build_server(require_auth=False)

    def search(arguments: dict) -> dict:
        async def run():
            async with Client(mcp) as client:
                result = await client.call_tool("search_memory", arguments, raise_on_error=False)
                body = result.structured_content
                if body is None:
                    return {"is_error": result.is_error, "text": " ".join(getattr(b, "text", "") for b in result.content)}
                return body["result"] if set(body) == {"result"} else body

        return asyncio.run(run())

    yield search
    writer_lease.reset_managers_for_tests()


def pictures_of(reply: dict, album: dict[str, tuple[str, str]]) -> set[str]:
    """The pictures a search returned, by name; a wire hit names the picture's original in `media_file`."""
    by_original = {original: name for name, (original, _page) in album.items()}
    assert "hits" in reply, reply
    return {by_original.get(hit.get("media_file"), str(hit)) for hit in reply["hits"]}


def test_a_person_with_images_and_a_capture_month_returns_only_that_persons_images_that_month(wire, album) -> None:
    """Catches a restriction that does not compose with the other filters."""
    reply = wire({"query": "", "people": ["Ada Fixture"], "file_types": ["image"], "filters": JUNE})

    assert pictures_of(reply, album) == {"ada-june", "ada-and-ben-june"}


def test_two_names_return_only_pages_with_both_people(wire, album) -> None:
    """Catches OR semantics where the description promises each person."""
    one = wire({"query": "", "people": ["Ada Fixture"]})
    both = wire({"query": "", "people": ["ada fixture", "Ben Fixture"]})

    assert pictures_of(one, album) == {"ada-june", "ada-and-ben-june", "ada-march"}
    assert pictures_of(both, album) == {"ada-and-ben-june"}


def test_a_person_seen_only_in_a_video_keyframe_returns_the_videos_page(wire, album) -> None:
    """Catches keyframe faces that never surface their video."""
    reply = wire({"query": "", "people": ["Cara Fixture"]})

    assert pictures_of(reply, album) == {"cara-video"}


def refused(reply: dict) -> dict:
    """The refusal block of a reply that ran no search. The wire puts the fix after the outcome in `message`."""
    assert "hits" not in reply, reply
    return reply.get("error") or reply


def test_with_faces_off_a_person_search_is_refused_and_names_how_to_turn_faces_on(wire, album, vault) -> None:
    """Catches a silent fallback that shows photos of other people."""
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_out(index)

    error = refused(wire({"query": "", "people": ["Ada Fixture"]}))

    assert error["code"] == "FACES_OFF", error
    assert error["message"].startswith("Face search is off")
    assert "faces.set" in error["message"]


def test_when_the_face_models_cannot_run_a_person_search_is_refused_as_unavailable(wire, album, vault) -> None:
    """Catches a silent fallback that shows photos of other people."""
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_availability(index, "the face embedder failed its digest check")

    error = refused(wire({"query": "", "people": ["Ada Fixture"]}))

    assert error["code"] == "FACES_UNAVAILABLE", error
    assert error["message"].startswith("Face search cannot run here (the face embedder failed")
    assert "Search without people" in error["message"]


def test_an_unknown_name_is_refused_and_the_refusal_lists_the_named_people(wire, album) -> None:
    """Catches a silent fallback that shows photos of other people."""
    error = refused(wire({"query": "", "people": ["Ada Fixture", "Dora Fixture"]}))

    assert error["code"] == "UNKNOWN_PERSON", error
    assert error["message"].startswith("No person is named 'Dora Fixture'")
    assert "Ada Fixture, Ben Fixture, Cara Fixture" in error["message"]


def test_after_a_rename_the_cached_search_with_the_old_name_is_refused(album, vault) -> None:
    """Catches stale results after naming, correction or delete."""
    def search() -> list:
        with library_scope():
            return find_module.find(vault, query="", people=["Ben Fixture"], scope="kb", mode="keyword")

    assert search(), "the first search should find Ben's photos and fill the cache"
    face_naming.rename_person(vault, "Ben Fixture", "Benjamin Fixture")

    with pytest.raises(cli_ops.OpError) as refusal:
        search()
    assert refusal.value.code == "UNKNOWN_PERSON"


@pytest.fixture
def store_opens(monkeypatch) -> list[str]:
    """Every SQLite open of a face store file, by path; any other open passes through."""
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        if "/faces/" in str(database).replace("\\", "/"):
            opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    return opened


def test_an_audience_bound_connector_session_is_refused_before_the_face_store_opens(album, vault, store_opens) -> None:
    """Catches face data or face-derived page membership served to another principal."""
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(cli_ops.OpError) as refusal:
            commands.op_ask_memory(vault, query="", people=["Ada Fixture"])

    assert refusal.value.code == "FACES_OWNER_ONLY"
    assert store_opens == []


def test_a_projected_search_with_people_is_refused_before_the_face_store_opens(album, vault, store_opens, monkeypatch) -> None:
    """Catches face data or face-derived page membership served to another principal."""
    from exomem.governance import projection_runtime

    # Any active projection: the refusal comes before the runtime is read.
    monkeypatch.setattr(projection_runtime, "load_active_projection_runtime", lambda root: object())

    with library_scope(), pytest.raises(cli_ops.OpError) as refusal:
        commands.op_find(vault, query="", people=["Ada Fixture"])

    assert refusal.value.code == "FACES_OWNER_ONLY"
    assert store_opens == []


def test_a_hosted_deployment_runs_faces_only_when_its_operator_enabled_them(album, vault, monkeypatch) -> None:
    """Catches faces running on a Cloud or hosted deployment whose operator never enabled them."""
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    monkeypatch.delenv(face_store.HOSTED_SETTING, raising=False)

    with library_scope(), pytest.raises(cli_ops.OpError) as refusal:
        find_module.find(vault, query="", people=["Ada Fixture"], mode="keyword")
    assert refusal.value.code == "FACES_UNAVAILABLE"
    assert "operator has not enabled faces" in refusal.value.message

    monkeypatch.setenv(face_store.HOSTED_SETTING, "1")
    with library_scope():
        hits = find_module.find(vault, query="", people=["Ben Fixture"], mode="keyword")
    assert {hit.path for hit in hits} == {album["ada-and-ben-june"][1], album["ben-june"][1]}


def seed_faces(vault: Path, count: int) -> None:
    """`count` faces on 500 pictures in three groups, one of them named; vectors are not read by a search."""
    turn_on(vault)
    with face_groups.Writing(vault) as writing:
        for _group in range(3):
            writing.index.execute(  # A new group starts unnamed, in the face index.
                "INSERT INTO groups (model_id, centroid_sum, face_count) VALUES (?, ?, ?)",
                (MODEL, b"", count // 3),
            )
        writing.index.executemany(
            "INSERT INTO faces (page, frame_ts, x, y, width, height, score, model_id, vector, group_id) "
            "VALUES (?, NULL, 0.1, 0.1, 0.2, 0.2, 0.99, ?, x'', ?)",
            ((f"Photos/p{number % 500}.jpg", MODEL, number % 3 + 1) for number in range(count)),
        )
    face_naming.name_group(vault, 1, "Ada Fixture")


def face_store_statements(vault: Path, monkeypatch) -> list[str]:
    """Run one person restriction and return every statement it sent to a face store file."""
    statements: list[str] = []
    real = sqlite3.connect

    def counting(database, *args, **kwargs):
        connection = real(database, *args, **kwargs)
        if "/faces/" in str(database).replace("\\", "/"):
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", counting)
    find_module.clear_cache()
    with library_scope():
        find_module.find(vault, query="", people=["Ada Fixture"], mode="keyword")
    monkeypatch.setattr(sqlite3, "connect", real)
    return statements


@pytest.mark.parametrize("count", [1_000, 200_000])
def test_a_person_restriction_sends_the_same_statements_at_any_store_size(vault, monkeypatch, count) -> None:
    """Catches a query per face, page or group."""
    seed_faces(vault, count)

    statements = face_store_statements(vault, monkeypatch)

    # The setting read, one name lookup and one page lookup.
    assert [line.split()[0] for line in statements] == ["SELECT", "SELECT", "SELECT"], statements
