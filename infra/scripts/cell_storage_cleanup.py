#!/usr/bin/env python3
"""Operator clean-up for one deleted cell's object-store key and backup files.

`contain-cloud-controller-compromise` D9: cellctl holds no credential that deletes keys or backup
files, so a deleted cell leaves the alert `cell-storage-cleanup-pending-<cell id>`. This script
answers it.

Run it once per account for every deleted cell, `--account founder` and `--account cell-data`:
the database no longer says which account holds the cell's key. It lists the account's
application keys and deletes each key whose name prefix is exactly `cells/<cell id>/`. It never
deletes a key it did not find in the provider's listing.
- cell-data: it also counts the cell's current backup files in the locked bucket, and hides them
  once a 7-day grace has passed. It never deletes a file version there: hiding adds a marker, and
  the compliance lock keeps every version until its retention ends.
- founder: the old cloud bucket has no lock, so `--apply` also deletes every file version under
  `cells/<cell id>/` in the bucket named by `--bucket-id`, hidden ones included, with no grace.

The grace starts at the operator's own first dry run, never at a time that cellctl or the
database holds: both are inputs that a compromised cellctl controls. The first cell-data dry run
prints `first_dry_run=<today, UTC>`; keep that date and pass it as `--first-dry-run` on every
later run. `--apply` for cell-data refuses to run without it. The operator confirms that the
database row says `desired_state = 'deleted'` before `--apply` (see the runbook).

Dry-run by default. `--apply` acts. Output is value-free (account label, counts, dates) because
the terminal may be shared. A later dry run that shows `keys_found=0` and `files_found=0` is the
proof that the clean-up is complete. For founder, `files_found` counts file versions.

Environment: CELL_CLEANUP_B2_KEY_ID and CELL_CLEANUP_B2_APPLICATION_KEY, the operator's key for
the chosen account. It needs listKeys and deleteKeys; for cell-data it also needs listFiles and
writeFiles (hiding a file is a write); for founder it also needs listBuckets, listFiles and
deleteFiles.

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
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, TextIO

# The same v4 as the key watcher: the v3 b2_list_keys hides most keys of a live account.
API_VERSION = "v4"
AUTHORIZE_URL = f"https://api.backblazeb2.com/b2api/{API_VERSION}/b2_authorize_account"
TIMEOUT_SECONDS = 20
PAGE_SIZE = 1000
MAX_PAGES = 100
# How long a deleted cell's backups stay visible after the operator's first dry run.
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


def file_versions(session: Session, bucket_id: str, prefix: str) -> list[dict[str, str]]:
    """Every version under `prefix`, hidden ones and unfinished large files included."""
    versions: list[dict[str, str]] = []
    cursor: dict[str, str] = {}
    for _ in range(MAX_PAGES):
        page = session.post("b2_list_file_versions",
                            {"bucketId": bucket_id, "prefix": prefix, "maxFileCount": PAGE_SIZE, **cursor})
        for entry in page["files"]:
            if not entry["fileName"].startswith(prefix):  # a delete never leaves the cell's prefix
                raise ValueError("the version listing left the prefix")
            versions.append({"fileName": entry["fileName"], "fileId": entry["fileId"]})
        if page.get("nextFileName") is None:
            return versions
        cursor = {"startFileName": page["nextFileName"], "startFileId": page["nextFileId"]}
    raise ValueError("the version listing did not end")


def clean_up(
    session: Session, *, label: str, cell_id: str, apply: bool, bucket_id: str,
    first_dry_run: date | None, today: date, out: TextIO,
) -> None:
    """`first_dry_run` is the date of the operator's first dry run, set for the cell-data account:
    the locked bucket's files are counted on every run and hidden only once the grace is over. Without
    it (the founder account) the unlocked bucket's file versions are deleted at once."""
    prefix = f"cells/{cell_id}/"
    key_ids = prefix_key_ids(session, prefix)
    for key_id in key_ids if apply else []:
        session.post("b2_delete_key", {"applicationKeyId": key_id})
    print(f"account={label} mode={'apply' if apply else 'dry-run'} keys_found={len(key_ids)} "
          f"keys_deleted={len(key_ids) if apply else 0}", file=out)
    if first_dry_run is None:
        versions = file_versions(session, bucket_id, prefix)
        for version in versions if apply else []:
            session.post("b2_delete_file_version", version)
        print(f"files_found={len(versions)} files_deleted={len(versions) if apply else 0}", file=out)
        return
    grace_ends = first_dry_run + HIDE_GRACE
    files = current_files(session, bucket_id, prefix)
    due = today >= grace_ends
    for file in files if apply and due else []:
        session.post("b2_hide_file", {"bucketId": bucket_id, **file})
    print(f"files_found={len(files)} files_hidden={len(files) if apply and due else 0} "
          f"hide={'due' if due else 'waiting'} first_dry_run={first_dry_run.isoformat()} "
          f"grace_ends={grace_ends.isoformat()}", file=out)


def _iso_date(text: str) -> date:
    """A calendar date as YYYY-MM-DD, the only form the runbook records."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cell_id", help="the 16-character cell id")
    parser.add_argument("--account", choices=ACCOUNTS, required=True)
    parser.add_argument("--apply", action="store_true", help="act; the default is a dry run")
    parser.add_argument("--first-dry-run", type=_iso_date, metavar="YYYY-MM-DD",
                        help="cell-data: the UTC date of your own first dry run for this cell; the grace runs from it")
    parser.add_argument("--bucket-id", help="the bucket's ID: the locked backup bucket for cell-data, "
                        "the old cloud bucket for founder")
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
    today = (now or datetime.now(UTC)).date()
    problems = [
        message for failed, message in (
            (not _CELL_ID.fullmatch(args.cell_id), "the cell id is not 16 characters of a-z and 2-7"),
            (not args.bucket_id, "--bucket-id is required"),
            (args.first_dry_run is not None and args.first_dry_run > today, "--first-dry-run is in the future"),
            (args.account == "cell-data" and args.apply and args.first_dry_run is None,
             "cell-data --apply needs --first-dry-run, the date your own first dry run printed"),
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
        first_dry_run = (args.first_dry_run or today) if args.account == "cell-data" else None
        clean_up(session, label=args.account, cell_id=args.cell_id, apply=args.apply, bucket_id=args.bucket_id,
                 first_dry_run=first_dry_run, today=today, out=out)
    except (OSError, http.client.HTTPException, ValueError, KeyError) as exc:
        # The type only: a provider message could carry an ID.
        print(f"cleanup stopped: {type(exc).__name__}; rerun it, every step is safe to repeat", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
