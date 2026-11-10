"""The operator's clean-up of a deleted cell's legacy backup files, and the rescue of its hidden files.

HTTP is injected at the transport. `FakeB2` answers B2 native API v4 and knows no other version,
so a script that lists keys through v3 would see none and fail the first test.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CELL_ID = "abcdefghijklmnop"
PREFIX = f"cells/{CELL_ID}/"
BUCKET_ID = "fixture-locked-bucket"
MEDIA_BUCKET_ID = "fixture-media-bucket"
ENV = {"CELL_CLEANUP_B2_KEY_ID": "fixture-id", "CELL_CLEANUP_B2_APPLICATION_KEY": "fixture-key"}


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "cell_storage_cleanup_under_test", ROOT / "infra/scripts/cell_storage_cleanup.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_script()


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeB2:
    """Two key pages, so a key on the second page is found only when the script follows the cursor."""

    KEY_PAGES = {
        None: {
            "keys": [
                {"applicationKeyId": "key-other-cell", "namePrefix": "cells/bcdefghijklmnopq/"},
                {"applicationKeyId": "key-sub-prefix", "namePrefix": PREFIX + "exports/"},
                {"applicationKeyId": "key-no-prefix", "namePrefix": None},
            ],
            "nextApplicationKeyId": "key-sub-prefix",
        },
        "key-sub-prefix": {
            "keys": [{"applicationKeyId": "key-of-the-cell", "namePrefix": PREFIX}],
            "nextApplicationKeyId": None,
        },
    }
    FILES = [
        {"fileName": PREFIX + "pack-1", "action": "upload"},
        {"fileName": PREFIX + "pack-2", "action": "hide"},
        {"fileName": PREFIX + "pack-3", "action": "upload"},
    ]
    # Every version, two pages, so a version on the second page is deleted only when the script
    # follows the cursor. pack-1 has a hidden version above its live one.
    VERSION_PAGES = {
        None: {
            "files": [
                {"fileName": PREFIX + "pack-1", "fileId": "pack-1-hidden", "action": "hide"},
                {"fileName": PREFIX + "pack-1", "fileId": "pack-1-live", "action": "upload"},
            ],
            "nextFileName": PREFIX + "pack-2",
            "nextFileId": "pack-2-live",
        },
        "pack-2-live": {
            "files": [{"fileName": PREFIX + "pack-2", "fileId": "pack-2-live", "action": "upload"}],
            "nextFileName": None,
            "nextFileId": None,
        },
    }

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def open(self, request: Any, /, *, timeout: float) -> _Response:
        url = request.full_url
        if url == "https://api.backblazeb2.com/b2api/v4/b2_authorize_account":
            return _Response({
                "accountId": "fixture-account", "authorizationToken": "fixture-token",
                "apiInfo": {"storageApi": {"apiUrl": "https://api001.backblazeb2.com"}},
            })
        prefix = "https://api001.backblazeb2.com/b2api/v4/"
        assert url.startswith(prefix), url
        endpoint, body = url.removeprefix(prefix), json.loads(request.data)
        self.posts.append((endpoint, body))
        if endpoint == "b2_list_keys":
            return _Response(self.KEY_PAGES[body.get("startApplicationKeyId")])
        if endpoint == "b2_list_file_names":
            return _Response({"files": self.FILES, "nextFileName": None})
        if endpoint == "b2_list_file_versions":
            assert body["bucketId"] == BUCKET_ID and body["prefix"] == PREFIX, body
            return _Response(self.VERSION_PAGES[body.get("startFileId")])
        return _Response({})

    def changes(self) -> list[tuple[str, dict[str, Any]]]:
        changing = {"b2_delete_key", "b2_hide_file", "b2_delete_file_version", "b2_cancel_large_file"}
        return [(endpoint, body) for endpoint, body in self.posts if endpoint in changing]


class FakeMediaB2(FakeB2):
    """The media bucket's versions as state, three to a page. A removal changes what the next listing shows,
    so a later dry run proves what is left. The provider's delete call leaves an unfinished large file in
    place; only the cancel call removes it."""

    def __init__(self) -> None:
        super().__init__()
        self.versions = [
            {"fileName": PREFIX + "image-1", "fileId": "image-1-hidden", "action": "hide"},
            {"fileName": PREFIX + "image-1", "fileId": "image-1-live", "action": "upload"},
            {"fileName": PREFIX + "image-2", "fileId": "image-2-live", "action": "upload"},
            {"fileName": PREFIX + "video-1", "fileId": "video-1-unfinished", "action": "start"},
            {"fileName": PREFIX + "video-2", "fileId": "video-2-live", "action": "upload"},
        ]

    def open(self, request: Any, /, *, timeout: float) -> _Response:
        endpoint = request.full_url.rsplit("/", 1)[-1]
        if endpoint == "b2_list_file_versions":
            body = json.loads(request.data)
            self.posts.append((endpoint, body))
            assert body["bucketId"] == MEDIA_BUCKET_ID and body["prefix"] == PREFIX, body
            ids = [entry["fileId"] for entry in self.versions]
            start = ids.index(body["startFileId"]) if "startFileId" in body else 0
            page, rest = self.versions[start:start + 3], self.versions[start + 3:]
            return _Response({"files": page, "nextFileName": rest[0]["fileName"] if rest else None,
                              "nextFileId": rest[0]["fileId"] if rest else None})
        response = super().open(request, timeout=timeout)
        body = json.loads(request.data) if request.data else {}
        if endpoint == "b2_cancel_large_file":
            self.versions = [entry for entry in self.versions if entry["fileId"] != body["fileId"]]
        if endpoint == "b2_delete_file_version":
            self.versions = [entry for entry in self.versions
                             if entry["fileId"] != body["fileId"] or entry["action"] == "start"]
        return response


def _run(argv: list[str], *, env: dict[str, str] = ENV, b2: FakeB2 | None = None) -> tuple[int, str, FakeB2]:
    b2, out = b2 or FakeB2(), io.StringIO()
    status = cleanup.main(argv, env, opener=b2, out=out)
    return status, out.getvalue(), b2


FOUNDER = [CELL_ID, "--account", "founder", "--bucket-id", BUCKET_ID]


def test_a_founder_dry_run_counts_the_key_and_every_version_and_changes_nothing() -> None:
    """Defect: a dry run that acts is not a dry run, and a count of current files only would report
    hidden versions as gone while the old bucket still holds them."""
    status, output, b2 = _run(FOUNDER)

    assert (status, b2.changes()) == (0, [])
    assert "keys_found=1 keys_deleted=0" in output
    assert "files_found=3 files_deleted=0" in output


def test_founder_apply_deletes_every_version_under_the_prefix_and_only_the_key_listed_for_exactly_that_prefix() -> None:
    """Defect: a prefix match too loose deletes another cell's key; a version left behind, a hidden
    one or one on a later page, keeps the old cell's data in the bucket after the clean-up reports done."""
    status, output, b2 = _run([*FOUNDER, "--apply"])

    assert status == 0
    assert b2.changes() == [
        ("b2_delete_key", {"applicationKeyId": "key-of-the-cell"}),
        ("b2_delete_file_version", {"fileName": PREFIX + "pack-1", "fileId": "pack-1-hidden"}),
        ("b2_delete_file_version", {"fileName": PREFIX + "pack-1", "fileId": "pack-1-live"}),
        ("b2_delete_file_version", {"fileName": PREFIX + "pack-2", "fileId": "pack-2-live"}),
    ]
    assert "keys_found=1 keys_deleted=1" in output
    assert "files_found=3 files_deleted=3" in output


MEDIA = [CELL_ID, "--account", "media", "--bucket-id", MEDIA_BUCKET_ID]


def test_a_media_dry_run_counts_uploads_hide_markers_and_unfinished_files_apart_and_changes_nothing() -> None:
    """Defect: a count that folds hide markers into uploads tells the operator that a purged cell's media is
    still current, or hides that a rescue is still possible."""
    status, output, b2 = _run(MEDIA, b2=FakeMediaB2())

    assert (status, b2.changes()) == (0, [])
    assert "mode=dry-run uploads_found=3 hides_found=1 hides_deleted=0 unfinished_found=1" in output
    assert "b2_list_keys" not in [endpoint for endpoint, _ in b2.posts]  # the media bucket has no per-cell key


def test_rescue_deletes_every_hide_marker_and_no_upload_or_unfinished_entry() -> None:
    """Defect: a rescue that deletes an `upload` entry destroys the only copy of the media it was run to keep;
    one that misses a marker on a later page leaves that file hidden until the provider deletes it."""
    b2 = FakeMediaB2()
    b2.versions.insert(4, {"fileName": PREFIX + "video-2", "fileId": "video-2-hidden", "action": "hide"})

    status, output, b2 = _run([*MEDIA, "--rescue"], b2=b2)

    assert status == 0
    assert b2.changes() == [
        ("b2_delete_file_version", {"fileName": PREFIX + "image-1", "fileId": "image-1-hidden"}),
        ("b2_delete_file_version", {"fileName": PREFIX + "video-2", "fileId": "video-2-hidden"}),
    ]
    assert "mode=rescue uploads_found=3 hides_found=2 hides_deleted=2 unfinished_found=1" in output
    _, output, _ = _run(MEDIA, b2=b2)
    assert "uploads_found=3 hides_found=0" in output


def test_bad_input_stops_before_any_provider_call() -> None:
    """Defect: an operator who follows an old runbook deletes a held cell's files at once with an apply mode
    that the executors replaced, or the operator's key leaks into a shared terminal."""
    cell_data = [CELL_ID, "--account", "cell-data", "--bucket-id", BUCKET_ID]
    for argv, env in (
        (["not-a-cell-id", "--account", "founder", "--bucket-id", BUCKET_ID], ENV),
        ([CELL_ID, "--account", "founder"], ENV),  # the old bucket's ID is not guessed
        ([*cell_data, "--apply"], ENV),  # the key custodian purges the backups
        ([*MEDIA, "--apply"], ENV),  # the media edge purges the media
        ([*cell_data, "--first-dry-run", "2026-11-01"], ENV),  # the grace now lives in the deletion grant
        ([*FOUNDER, "--rescue"], ENV),  # the legacy bucket has no executor that hides
        ([CELL_ID, "--account", "media", "--rescue"], ENV),  # the media bucket's ID is not guessed
        (FOUNDER, {}),
    ):
        status, output, b2 = _run(argv, env=env)
        assert (status, b2.posts) == (2, []), argv
        assert "fixture-key" not in output
