"""The operator's clean-up of a deleted cell's object-store key.

HTTP is injected at the transport. `FakeB2` answers B2 native API v4 and knows no other version,
so a script that lists keys through v3 would see none and fail the first test.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CELL_ID = "abcdefghijklmnop"
PREFIX = f"cells/{CELL_ID}/"
BUCKET_ID = "fixture-locked-bucket"
ENV = {"CELL_CLEANUP_B2_KEY_ID": "fixture-id", "CELL_CLEANUP_B2_APPLICATION_KEY": "fixture-key"}
FIRST_DRY_RUN = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


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
        return _Response({})

    def changes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(endpoint, body) for endpoint, body in self.posts if endpoint in {"b2_delete_key", "b2_hide_file"}]


def _run(argv: list[str], *, now: datetime = NOW, env: dict[str, str] = ENV) -> tuple[int, str, FakeB2]:
    b2, out = FakeB2(), io.StringIO()
    status = cleanup.main(argv, env, opener=b2, now=now, out=out)
    return status, out.getvalue(), b2


def test_a_dry_run_changes_nothing_and_apply_deletes_only_the_key_listed_for_exactly_that_prefix() -> None:
    """Defect: a prefix match too loose deletes another cell's key; a dry run that acts is not a dry run."""
    status, output, b2 = _run([CELL_ID, "--account", "founder"])
    assert (status, b2.changes()) == (0, [])
    assert "keys_found=1 keys_deleted=0" in output

    status, output, b2 = _run([CELL_ID, "--account", "founder", "--apply"])
    assert status == 0
    assert b2.changes() == [("b2_delete_key", {"applicationKeyId": "key-of-the-cell"})]
    assert "keys_found=1 keys_deleted=1" in output


def test_a_dry_run_counts_the_backup_files_and_names_the_date_the_grace_runs_from() -> None:
    """Defect: a count that waits for the grace hides from the operator that files remain."""
    status, output, b2 = _run([CELL_ID, "--account", "cell-data", "--bucket-id", BUCKET_ID])

    assert (status, b2.changes()) == (0, [])
    assert "files_found=2 files_hidden=0 hide=waiting first_dry_run=2026-11-01 grace_ends=2026-11-08" in output


def test_backups_stay_visible_through_the_grace_and_only_current_files_are_hidden_after_it() -> None:
    """Defect: hiding a deleted cell's backups at once removes the owner's chance to ask for them."""
    argv = [CELL_ID, "--account", "cell-data", "--apply", "--bucket-id", BUCKET_ID,
            "--first-dry-run", FIRST_DRY_RUN.isoformat()]
    _, output, b2 = _run(argv, now=NOW + timedelta(days=5, hours=23))
    assert "files_found=2 files_hidden=0 hide=waiting" in output and "grace_ends=2026-11-08" in output
    assert [endpoint for endpoint, _ in b2.changes()] == ["b2_delete_key"]  # the key goes at once

    _, output, b2 = _run(argv, now=NOW + timedelta(days=6, hours=12))
    assert b2.changes()[1:] == [
        ("b2_hide_file", {"bucketId": BUCKET_ID, "fileName": PREFIX + "pack-1"}),
        ("b2_hide_file", {"bucketId": BUCKET_ID, "fileName": PREFIX + "pack-3"}),
    ]
    assert "files_found=2 files_hidden=2 hide=due" in output


def test_bad_input_stops_before_any_provider_call() -> None:
    cell_data = [CELL_ID, "--account", "cell-data", "--bucket-id", BUCKET_ID]
    for argv, env in (
        (["not-a-cell-id", "--account", "founder"], ENV),
        ([*cell_data, "--apply"], ENV),  # the grace must start from the operator's own date
        ([*cell_data, "--first-dry-run", "2026-11-02"], ENV),  # a date after today starts no grace
        ([*cell_data, "--first-dry-run", "1 November"], ENV),
        ([CELL_ID, "--account", "cell-data", "--first-dry-run", "2026-11-01"], ENV),
        ([CELL_ID, "--account", "founder"], {}),
    ):
        status, output, b2 = _run(argv, env=env)
        assert (status, b2.posts) == (2, []), argv
        assert "fixture-key" not in output
