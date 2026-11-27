"""What's new on a cell: the `whats_new` bootstrap section and the one-time `update_notice`.

The notice cases send requests the way the Cloud gateway forwards them (one `x-exomem-app-id`
per connected app) and read what the connected assistant would read back. The notes file and the
running version are the only things replaced: they stand for the image the cell runs.
"""

from __future__ import annotations

import json
import logging

import pytest
from _public_call import public_call
from test_cloud_cell import LEGACY_VERSION, TOKEN
from test_cloud_service_notice import run_cell as service_notice_cell

from exomem import cloud_cell, commands, release_notes, state_paths

APP = "11111111-1111-4111-8111-111111111111"
OTHER_APP = "22222222-2222-4222-8222-222222222222"

def note(text: str, title: str | None = None) -> dict:
    return {"title": title, "text": text}


RELEASES = [
    {
        "version": "1.3.0",
        "released_on": "2026-11-20",
        "notes": [note("Newest change.", "Newest"), note("Second change.", "Second")],
    },
    {"version": "1.2.1", "released_on": "2026-11-10", "notes": [note("Third change.", "Third")]},
    {"version": "1.2.0", "released_on": "2026-11-01", "notes": [note("Old change.")]},  # a note merged without the check: no title
]

#: What Home receives for RELEASES: `notes` stays the list of sentences that every earlier Home reads, `items` adds the titles.
SERVED = [
    {
        "version": "1.3.0",
        "released_on": "2026-11-20",
        "notes": ["Newest change.", "Second change."],
        "items": [note("Newest change.", "Newest"), note("Second change.", "Second")],
    },
    {"version": "1.2.1", "released_on": "2026-11-10", "notes": ["Third change."], "items": [note("Third change.", "Third")]},
    {"version": "1.2.0", "released_on": "2026-11-01", "notes": ["Old change."], "items": [note("Old change.")]},
]


@pytest.fixture
def run_cell(vault, monkeypatch, tmp_path):
    """Run a scenario against a cell (or, with `cloud=False`, a personal service) over HTTP."""
    yield from service_notice_cell.__wrapped__(vault, monkeypatch, tmp_path)


@pytest.fixture
def image(monkeypatch, tmp_path):
    """The image the cell runs: `image.version = "1.3.0"` upgrades it, `image.notes(...)` rebuilds its notes."""

    class Image:
        version = "1.2.0"
        reads = 0

        def notes(self, releases: list[dict] | str | None) -> None:
            path = tmp_path / "user_release_notes.json"
            if releases is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(releases if isinstance(releases, str) else json.dumps({"schema": 2, "releases": releases}))
            release_notes.releases.cache_clear()

        def read(self) -> str:
            self.reads += 1
            return (tmp_path / "user_release_notes.json").read_text("utf-8")

    built = Image()
    monkeypatch.setattr(release_notes, "running_version", lambda: built.version)
    monkeypatch.setattr(release_notes, "_read_text", built.read)
    built.notes(RELEASES)
    yield built
    release_notes.releases.cache_clear()


async def call(client, name, arguments, *, app_id: str | None = None) -> dict:
    """One tools/call as the gateway forwards it; the call's structured result."""
    name, arguments = public_call(name, arguments)
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {TOKEN}",
        "mcp-protocol-version": LEGACY_VERSION,
    }
    if app_id is not None:
        headers[cloud_cell.GATEWAY_APP_ID_HEADER] = app_id
    response = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


async def read(client, app_id: str | None = APP) -> dict:
    return await call(client, "recall_context", {"turn": "anything"}, app_id=app_id)


def test_an_app_is_told_once_about_an_upgrade_with_notes_and_a_new_app_is_told_nothing(run_cell, image, vault):
    """The assistant repeats what a result carries to the person: a notice on every call would nag,
    a notice for an app that never saw the old version would tell it news it has no baseline for,
    a notice that did not survive a restart would repeat itself on every restart, and an image
    shipped without its notes file must not use up the notes of the release it hides."""

    async def scenario(client):
        seen = {"first call": await read(client)}
        image.version = "1.3.0"
        seen["no app id"] = await read(client, app_id=None)
        seen["upgrade"] = await read(client)
        seen["again"] = await read(client)
        seen["new app"] = await read(client, app_id=OTHER_APP)
        release_notes._told_memo.clear()  # a restart: the process forgets, the state store does not
        seen["after restart"] = await read(client)
        image.version = "1.3.1"
        image.notes(None)  # an image built without its notes file
        seen["image without notes"] = await read(client)
        saved = state_paths.vault_state_dir(vault) / release_notes.STATE_FILENAME
        seen["record after image without notes"] = json.loads(saved.read_text("utf-8"))["apps"][APP]
        image.version = "1.4.0"
        image.notes(
            [
                {"version": "1.4.0", "released_on": "2026-11-25", "notes": [note("Fourth change.", "Fourth")]},
                {"version": "1.3.1", "released_on": "2026-11-23", "notes": [note("The change in the image without notes.")]},
                *RELEASES,
            ]
        )
        seen["second upgrade"] = await read(client)
        return seen

    seen = run_cell(scenario)

    assert seen.pop("record after image without notes") == "1.3.0"
    assert [name for name, result in seen.items() if "update_notice" in result] == ["upgrade", "second upgrade"]
    notice = seen["upgrade"]["update_notice"]
    assert notice["version"] == "1.3.0"
    # Three notes arrived since 1.2.0: the newest note, a count of the other two, and the place to read them.
    assert notice["say"] == (
        "Tell the user once in this conversation: Evermatter Cloud was updated. Newest change. "
        "2 more changes: see What's new on Home."
    )
    assert list(seen["upgrade"])[-1] == "update_notice"
    # The release of the image without notes arrives with the next one that has them.
    assert seen["second upgrade"]["update_notice"]["say"].endswith("Fourth change. 1 more change: see What's new on Home.")


def test_an_upgrade_without_notes_moves_the_apps_version_and_tells_nothing(run_cell, image, vault):
    async def scenario(client):
        await read(client)
        image.version = "1.2.1"
        image.notes(RELEASES[2:])  # releases up to 1.2.0 have notes; 1.2.1 and 1.3.0 have none
        quiet = await read(client)
        image.version = "1.3.0"
        return quiet, await read(client)

    quiet, later = run_cell(scenario)

    assert "update_notice" not in quiet
    assert "update_notice" not in later
    saved = json.loads((state_paths.vault_state_dir(vault) / release_notes.STATE_FILENAME).read_text("utf-8"))
    assert saved["apps"] == {APP: "1.3.0"}


def test_a_state_store_that_refuses_the_write_costs_the_notice_and_never_the_call(run_cell, image, monkeypatch):
    """The app is told on a later call, once the store works: a failed write records nothing."""

    def refuse(path, told):
        raise OSError("read-only file system")

    async def scenario(client):
        await read(client)
        image.version = "1.3.0"
        with monkeypatch.context() as broken:
            broken.setattr(release_notes, "_write_told", refuse)
            refused = await read(client)
        return refused, await read(client), await read(client)

    refused, recovered, after = run_cell(scenario)

    assert "update_notice" not in refused
    assert recovered["update_notice"]["version"] == "1.3.0"
    assert "update_notice" not in after


def test_a_personal_service_never_carries_an_update_notice(run_cell, image):
    async def scenario(client):
        await read(client)
        image.version = "1.3.0"
        return await read(client)

    assert "update_notice" not in run_cell(scenario, cloud=False)


def test_a_rollback_keeps_the_apps_record_so_the_next_upgrade_does_not_repeat_the_news(run_cell, image):
    async def scenario(client):
        image.version = "1.3.0"
        await read(client)
        image.version = "1.2.1"
        rolled_back = await read(client)
        image.version = "1.3.0"
        return rolled_back, await read(client)

    rolled_back, upgraded_again = run_cell(scenario)

    assert "update_notice" not in rolled_back
    assert "update_notice" not in upgraded_again


def test_the_notice_is_one_bounded_sentence_even_when_a_note_is_too_long(image, vault):
    long_note = "A very long note. " * 30
    image.notes([{"version": "1.3.0", "released_on": "2026-11-20", "notes": [note(long_note, "Long"), note("Other.", "Other")]}])
    release_notes.update_notice(vault, APP)
    image.version = "1.3.0"

    notice = release_notes.update_notice(vault, APP)

    assert notice is not None
    assert notice["say"].startswith("Tell the user once in this conversation: ")
    assert len(notice["say"]) <= cloud_cell.SERVICE_NOTICE_MAX_SAY_CHARS
    assert notice["say"].endswith("1 more change: see What's new on Home.")


def test_the_whats_new_section_gives_the_running_version_and_the_releases_newest_first_from_one_read(image, vault):
    image.version = "1.3.0"
    image.notes(list(reversed(RELEASES)))  # the file's own order does not matter: the section is newest first

    first = commands.op_bootstrap(vault, section="whats_new")
    second = commands.op_bootstrap(vault, section="whats_new")

    assert first == {"whats_new": {"version": "1.3.0", "releases": SERVED, "notes_available": True}}
    assert second == first
    assert image.reads == 1


def test_the_whats_new_section_is_served_to_home_through_the_cell(run_cell, image):
    image.version = "1.3.0"

    async def scenario(client):
        # Home's call exactly as Home sends it; the cell refuses the command name `bootstrap`.
        return await call(client, "read_instructions", {"profile": "compact", "section": "whats_new"})

    assert run_cell(scenario) == {"whats_new": {"version": "1.3.0", "releases": SERVED, "notes_available": True}}


@pytest.mark.parametrize(
    "file",
    [
        None,
        "{not json",
        json.dumps({"schema": 1, "releases": [{**RELEASES[0], "notes": ["Newest change."]}]}),
        json.dumps({"schema": 3, "releases": RELEASES}),
        json.dumps({"schema": 2, "releases": [*RELEASES, {"version": "next", "released_on": "x", "notes": [note("A.")]}]}),
        json.dumps({"schema": 2, "releases": [{**RELEASES[0], "notes": ["Newest change."]}]}),
    ],
    ids=["no file", "not JSON", "schema 1 file", "unknown schema", "a release the cell cannot read", "a note that is a bare string"],
)
def test_an_image_without_usable_notes_says_the_notes_are_unavailable_and_never_fails(image, vault, file):
    """Home must tell "unknown" from "nothing to show": an empty list alone reads as the second."""
    image.version = "1.3.0"
    image.notes(file)

    assert commands.op_bootstrap(vault, section="whats_new") == {
        "whats_new": {"version": "1.3.0", "releases": [], "notes_available": False}
    }


def test_a_valid_notes_file_with_no_releases_is_available_and_empty(image, vault):
    image.version = "1.3.0"
    image.notes([])

    assert commands.op_bootstrap(vault, section="whats_new") == {
        "whats_new": {"version": "1.3.0", "releases": [], "notes_available": True}
    }


@pytest.mark.parametrize("file", [None, "{not json"], ids=["no file", "not JSON"])
def test_a_cloud_cell_warns_once_when_its_notes_are_missing_or_unusable(image, vault, monkeypatch, caplog, file):
    """A Cloud image is built with its notes, so a missing file there is a build defect to see in the log."""
    monkeypatch.setattr(cloud_cell, "cloud_mode_enabled", lambda: True)
    image.notes(file)

    with caplog.at_level(logging.WARNING, logger=release_notes._log.name):
        release_notes.payload()
        release_notes.payload()

    assert [record.event for record in caplog.records] == ["release_notes_unreadable"]


def test_a_personal_service_without_a_notes_file_logs_nothing(image, vault, caplog):
    image.notes(None)

    with caplog.at_level(logging.WARNING, logger=release_notes._log.name):
        release_notes.payload()

    assert caplog.records == []


def test_the_notes_file_round_trips_titles_and_refuses_a_note_it_cannot_read_whole():
    """The release build writes through `document` and the cell reads through `parse_document`: one reading
    of a note, with a null title kept and any other shape refused rather than served in part."""
    from exomem import release_notes_format as fmt

    release = fmt.Release("1.3.0", (1, 3, 0), "2026-11-20", (fmt.Note("Newest", "Newest change."), fmt.Note(None, "Old change.")))

    assert fmt.parse_document(json.dumps(fmt.document([release]))) == (release,)
    for broken in (
        {"title": "", "text": "A."},  # an empty title is null or text, never blank
        {"title": "T", "text": ""},
        {"title": "T", "text": "A.", "kind": "fixed"},
        {"text": "A."},
    ):
        with pytest.raises(ValueError):
            fmt.parse_document(json.dumps({"schema": 2, "releases": [{**RELEASES[0], "notes": [broken]}]}))
