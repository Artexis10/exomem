"""What's new on a cell: the `whats_new` bootstrap section and the one-time `update_notice`.

The notice cases send requests the way the Cloud gateway forwards them (one `x-exomem-app-id`
per connected app) and read what the connected assistant would read back. The notes file and the
running version are the only things replaced: they stand for the image the cell runs.
"""

from __future__ import annotations

import json

import pytest
from _public_call import public_call
from test_cloud_cell import LEGACY_VERSION, TOKEN
from test_cloud_service_notice import run_cell as service_notice_cell

from exomem import cloud_cell, commands, release_notes, state_paths

APP = "11111111-1111-4111-8111-111111111111"
OTHER_APP = "22222222-2222-4222-8222-222222222222"

RELEASES = [
    {"version": "1.3.0", "released_on": "2026-11-20", "notes": ["Newest change.", "Second change."]},
    {"version": "1.2.1", "released_on": "2026-11-10", "notes": ["Third change."]},
    {"version": "1.2.0", "released_on": "2026-11-01", "notes": ["Old change."]},
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
                path.write_text(releases if isinstance(releases, str) else json.dumps({"schema": 1, "releases": releases}))
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


def test_an_app_is_told_once_about_an_upgrade_with_notes_and_a_new_app_is_told_nothing(run_cell, image):
    """The assistant repeats what a result carries to the person: a notice on every call would nag,
    a notice for an app that never saw the old version would tell it news it has no baseline for,
    and a notice that did not survive a restart would repeat itself on every restart."""

    async def scenario(client):
        seen = {"first call": await read(client)}
        image.version = "1.3.0"
        seen["no app id"] = await read(client, app_id=None)
        seen["upgrade"] = await read(client)
        seen["again"] = await read(client)
        seen["new app"] = await read(client, app_id=OTHER_APP)
        release_notes._told_memo.clear()  # a restart: the process forgets, the state store does not
        seen["after restart"] = await read(client)
        image.version = "1.4.0"
        image.notes([{"version": "1.4.0", "released_on": "2026-11-25", "notes": ["Fourth change."]}, *RELEASES])
        seen["second upgrade"] = await read(client)
        return seen

    seen = run_cell(scenario)

    assert [name for name, result in seen.items() if "update_notice" in result] == ["upgrade", "second upgrade"]
    notice = seen["upgrade"]["update_notice"]
    assert notice["version"] == "1.3.0"
    # Three notes arrived since 1.2.0: the newest note, a count of the other two, and the place to read them.
    assert notice["say"] == (
        "Tell the user once in this conversation: Evermatter Cloud was updated. Newest change. "
        "2 more changes: see What's new on Home."
    )
    assert list(seen["upgrade"])[-1] == "update_notice"
    assert seen["second upgrade"]["update_notice"]["say"].endswith("Fourth change. You can read it under What's new on Home.")


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
    image.notes([{"version": "1.3.0", "released_on": "2026-11-20", "notes": [long_note, "Other."]}])
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

    assert first == {"whats_new": {"version": "1.3.0", "releases": RELEASES}}
    assert second == first
    assert image.reads == 1


def test_the_whats_new_section_is_served_to_home_through_the_cell(run_cell, image):
    image.version = "1.3.0"

    async def scenario(client):
        return await call(client, "bootstrap", {"profile": "compact", "section": "whats_new"})

    assert run_cell(scenario) == {"whats_new": {"version": "1.3.0", "releases": RELEASES}}


@pytest.mark.parametrize(
    "file",
    [
        None,
        "{not json",
        json.dumps({"schema": 2, "releases": RELEASES}),
        json.dumps({"schema": 1, "releases": [*RELEASES, {"version": "next", "released_on": "x", "notes": ["A."]}]}),
    ],
    ids=["no file", "not JSON", "unknown schema", "a release the cell cannot read"],
)
def test_an_image_without_usable_notes_serves_an_empty_list_and_never_fails(image, vault, file):
    image.version = "1.3.0"
    image.notes(file)

    assert commands.op_bootstrap(vault, section="whats_new") == {"whats_new": {"version": "1.3.0", "releases": []}}
