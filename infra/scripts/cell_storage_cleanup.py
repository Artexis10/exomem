#!/usr/bin/env python3
"""Operator clean-up for one deleted cell's object-store key.

`contain-cloud-controller-compromise` D9: cellctl holds no credential that deletes keys, so a
deleted cell leaves the alert `cell-storage-cleanup-pending-<cell id>`. This script answers it.

It lists the account's application keys, deletes each key whose name prefix is exactly
`cells/<cell id>/`, and, for the cell-data account, hides the cell's current backup files once a
7-day grace after the cell's deletion has passed. It never deletes a key it did not find in the
provider's listing, and it never deletes a file version: hiding adds a marker, and the compliance
lock keeps every version until its retention ends.

Dry-run by default. `--apply` acts. Output is value-free (account label, counts, a grace date)
because the terminal may be shared.

Environment: CELL_CLEANUP_B2_KEY_ID and CELL_CLEANUP_B2_APPLICATION_KEY, the operator's key for
the chosen account. It needs listKeys and deleteKeys; for cell-data it also needs listFiles and
writeFiles (hiding a file is a write).

Exit status: 0 when the run finished (a dry run included), 1 when a provider call failed, 2 when
the arguments or the environment are invalid.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TextIO

# The same v4 as the key watcher: the v3 b2_list_keys hides most keys of a live account.
API_VERSION = "v4"
AUTHORIZE_URL = f"https://api.backblazeb2.com/b2api/{API_VERSION}/b2_authorize_account"
TIMEOUT_SECONDS = 20
PAGE_SIZE = 1000
MAX_PAGES = 100
# How long a deleted cell's backups stay visible before they are hidden.
HIDE_GRACE = timedelta(days=7)
ACCOUNTS = ("founder", "cell-data")

# An operator types this id. Its form is fixed by cellctl's namespace rule `exo-cell-[a-z2-7]{16}`
# (the key watcher keeps the same copy), so a regex over it checks a format, not a meaning.
_CELL_ID = re.compile(r"[a-z2-7]{16}")


class Opener(Protocol):
    """The transport: urllib's OpenerDirector in production, a fake in tests."""

    def open(self, request: urllib.request.Request, /, *, timeout: float) -> Any: ...


def _call(opener: Opener, url: str, *, headers: Mapping[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="GET" if data is None else "POST")
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("unexpected response")
    return payload


class Session:
    def __init__(self, opener: Opener, key_id: str, key: str) -> None:
        basic = base64.b64encode(f"{key_id}:{key}".encode()).decode("ascii")
        payload = _call(opener, AUTHORIZE_URL, headers={"Authorization": f"Basic {basic}"})
        self._opener = opener
        self._api_url = payload["apiInfo"]["storageApi"]["apiUrl"].rstrip("/")
        self._token = payload["authorizationToken"]
        self.account_id = payload["accountId"]

    def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        return _call(
            self._opener,
            f"{self._api_url}/b2api/{API_VERSION}/{endpoint}",
            headers={"Authorization": self._token, "Content-Type": "application/json"},
            body=body,
        )


def prefix_key_ids(session: Session, prefix: str) -> list[str]:
    """The IDs, as the provider lists them, of every key whose name prefix is exactly `prefix`."""
    found: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        body: dict[str, Any] = {"accountId": session.account_id, "maxKeyCount": PAGE_SIZE}
        if cursor is not None:
            body["startApplicationKeyId"] = cursor
        page = session.post("b2_list_keys", body)
        found += [key["applicationKeyId"] for key in page["keys"] if key.get("namePrefix") == prefix]
        cursor = page.get("nextApplicationKeyId")
        if cursor is None:
            return found
    raise ValueError("the key listing did not end")


def current_files(session: Session, bucket_id: str, prefix: str) -> list[dict[str, str]]:
    """The newest version of each file under `prefix` that is not already hidden."""
    files: list[dict[str, str]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        body: dict[str, Any] = {"bucketId": bucket_id, "prefix": prefix, "maxFileCount": PAGE_SIZE}
        if cursor is not None:
            body["startFileName"] = cursor
        page = session.post("b2_list_file_names", body)
        files += [{"fileName": f["fileName"]} for f in page["files"] if f.get("action") == "upload"]
        cursor = page.get("nextFileName")
        if cursor is None:
            return files
    raise ValueError("the file listing did not end")


def clean_up(
    session: Session, *, label: str, cell_id: str, apply: bool,
    backup: tuple[str, datetime] | None, now: datetime, out: TextIO,
) -> None:
    """`backup` is the locked bucket's ID and the cell's deletion time, set for the cell-data account."""
    prefix = f"cells/{cell_id}/"
    key_ids = prefix_key_ids(session, prefix)
    for key_id in key_ids if apply else []:
        session.post("b2_delete_key", {"applicationKeyId": key_id})
    print(f"account={label} mode={'apply' if apply else 'dry-run'} keys_found={len(key_ids)} "
          f"keys_deleted={len(key_ids) if apply else 0}", file=out)
    if backup is None:
        return
    bucket_id, deleted_at = backup
    grace_ends = deleted_at + HIDE_GRACE
    if now < grace_ends:
        print(f"hide=waiting grace_ends={grace_ends.date().isoformat()}", file=out)
        return
    files = current_files(session, bucket_id, prefix)
    for file in files if apply else []:
        session.post("b2_hide_file", {"bucketId": bucket_id, **file})
    print(f"hide=due files_found={len(files)} files_hidden={len(files) if apply else 0}", file=out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cell_id", help="the 16-character cell id")
    parser.add_argument("--account", choices=ACCOUNTS, required=True)
    parser.add_argument("--apply", action="store_true", help="act; the default is a dry run")
    parser.add_argument("--deleted-at", help="cell-data: when the cell was deleted, ISO 8601 with a UTC offset")
    parser.add_argument("--bucket-id", help="cell-data: the locked backup bucket's ID")
    return parser


def main(
    argv: Sequence[str], env: Mapping[str, str], *, opener: Opener | None = None,
    now: datetime | None = None, out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # argparse: 0 for --help, 2 for a bad argument
        return exit_request.code if isinstance(exit_request.code, int) else 2
    deleted_at = None
    if args.deleted_at:
        try:
            deleted_at = datetime.fromisoformat(args.deleted_at)
        except ValueError:
            deleted_at = None
    problems = [
        message for failed, message in (
            (not _CELL_ID.fullmatch(args.cell_id), "the cell id is not 16 characters of a-z and 2-7"),
            (args.account == "cell-data" and (deleted_at is None or deleted_at.tzinfo is None),
             "cell-data needs --deleted-at as ISO 8601 with a UTC offset"),
            (args.account == "cell-data" and not args.bucket_id, "cell-data needs --bucket-id"),
            (not env.get("CELL_CLEANUP_B2_KEY_ID") or not env.get("CELL_CLEANUP_B2_APPLICATION_KEY"),
             "CELL_CLEANUP_B2_KEY_ID and CELL_CLEANUP_B2_APPLICATION_KEY must be set"),
        ) if failed
    ]
    if problems:
        print("; ".join(problems), file=out)
        return 2
    try:
        session = Session(opener or urllib.request.build_opener(), env["CELL_CLEANUP_B2_KEY_ID"],
                          env["CELL_CLEANUP_B2_APPLICATION_KEY"])
        backup = (args.bucket_id, deleted_at) if args.account == "cell-data" else None
        clean_up(session, label=args.account, cell_id=args.cell_id, apply=args.apply, backup=backup,
                 now=now or datetime.now(UTC), out=out)
    except (OSError, http.client.HTTPException, ValueError, KeyError) as exc:
        # The type only: a provider message could carry an ID.
        print(f"cleanup stopped: {type(exc).__name__}; rerun it, every step is safe to repeat", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
