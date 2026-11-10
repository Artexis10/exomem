#!/usr/bin/env python3
"""Operator clean-up for a deleted cell's legacy backup files, and rescue of a deleted cell's hidden files.

`automate-cell-storage-deletion`: the key custodian and the media edge purge a deleted cell's key and files in
the cell-data account and the media bucket, on the cell's deletion grant, after its `purge_after`. This
script has no mode that hides or deletes files there. It has three targets, chosen by `--account`:
- founder: cellctl raises `cell-storage-cleanup-pending-<cell id>` only for a cell with data in the founder
  account's legacy backup bucket, which has no lock and no executor. `--apply` answers that alert. It
  deletes each key whose name prefix is exactly `cells/<cell id>/`, and every file version under that prefix
  in the bucket named by `--bucket-id`, hidden ones included. It never deletes a key it did not find in the
  provider's listing.
- cell-data: the locked backup bucket.
- media: the founder account's media bucket.
For cell-data and media, a dry run counts the uploads, hide markers and unfinished large files under
`cells/<cell id>/`. `--rescue` deletes each hide marker there, so the version under it becomes current
again. It deletes no `upload` entry and no `start` entry. The provider deletes a hidden media version N days
after its hide, and a hidden backup version one day after its lock lapses, so a rescue works only before
then. In the locked bucket, the provider may refuse to delete a hide marker; the run then stops with exit 1.
Hold the cell first in the chart value `cellDeletion.holdCells`, or the executors hide the files again on
their next pass. The runbook `docs/runbooks/cloud-cell-deletion.md` gives the steps.

Dry-run by default. Output is value-free (account label and counts) because the terminal may be shared. For
founder, a later dry run that shows `keys_found=0` and `files_found=0` proves that the clean-up is complete;
`files_found` counts file versions.

Environment: CELL_CLEANUP_B2_KEY_ID and CELL_CLEANUP_B2_APPLICATION_KEY, the operator's key for the chosen
account. For founder it needs listKeys, deleteKeys, listBuckets, listFiles and deleteFiles. For cell-data and
media it needs listFiles, and deleteFiles for `--rescue` (deleting a hide marker deletes a file version),
restricted to the bucket, and no key capability.

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
from typing import Any, Protocol, TextIO

# The same v4 as the key watcher: the v3 b2_list_keys hides most keys of a live account.
API_VERSION = "v4"
AUTHORIZE_URL = f"https://api.backblazeb2.com/b2api/{API_VERSION}/b2_authorize_account"
TIMEOUT_SECONDS = 20
PAGE_SIZE = 1000
MAX_PAGES = 100
ACCOUNTS = ("founder", "cell-data", "media")
# The accounts whose files the executors purge: the script only counts or rescues there.
RESCUE_ACCOUNTS = ("cell-data", "media")
# `action` is the provider's own closed value for a version listing (B2 native API): "upload" is a stored
# version, "hide" a hide marker, and "start" an upload that was never finished.
UPLOAD, HIDE, START = "upload", "hide", "start"

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
            versions.append({"fileName": entry["fileName"], "fileId": entry["fileId"], "action": entry.get("action")})
        if page.get("nextFileName") is None:
            return versions
        cursor = {"startFileName": page["nextFileName"], "startFileId": page["nextFileId"]}
    raise ValueError("the version listing did not end")


def count_or_rescue(session: Session, *, label: str, cell_id: str, rescue: bool, bucket_id: str, out: TextIO) -> None:
    """cell-data and media: count the entries under the cell's prefix and, with `rescue`, delete the hide
    markers among them. It lists no key: the key custodian deletes the cell's cell-data key, and the media
    bucket has no per-cell key."""
    entries = file_versions(session, bucket_id, f"cells/{cell_id}/")
    hides = [entry for entry in entries if entry["action"] == HIDE]
    for entry in hides if rescue else []:
        session.post("b2_delete_file_version", {"fileName": entry["fileName"], "fileId": entry["fileId"]})
    uploads = sum(entry["action"] == UPLOAD for entry in entries)
    unfinished = sum(entry["action"] == START for entry in entries)
    print(f"account={label} mode={'rescue' if rescue else 'dry-run'} uploads_found={uploads} "
          f"hides_found={len(hides)} hides_deleted={len(hides) if rescue else 0} unfinished_found={unfinished}",
          file=out)


def clean_up_founder(session: Session, *, cell_id: str, apply: bool, bucket_id: str, out: TextIO) -> None:
    """The legacy backup bucket has no lock, so `apply` deletes its file versions at once."""
    prefix = f"cells/{cell_id}/"
    key_ids = prefix_key_ids(session, prefix)
    for key_id in key_ids if apply else []:
        session.post("b2_delete_key", {"applicationKeyId": key_id})
    print(f"account=founder mode={'apply' if apply else 'dry-run'} keys_found={len(key_ids)} "
          f"keys_deleted={len(key_ids) if apply else 0}", file=out)
    versions = file_versions(session, bucket_id, prefix)
    for version in versions if apply else []:
        session.post("b2_delete_file_version", {"fileName": version["fileName"], "fileId": version["fileId"]})
    print(f"files_found={len(versions)} files_deleted={len(versions) if apply else 0}", file=out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cell_id", help="the 16-character cell id")
    parser.add_argument("--account", choices=ACCOUNTS, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="founder: delete the key and every file version; the default is a dry run")
    parser.add_argument("--rescue", action="store_true",
                        help="cell-data and media: delete the hide markers, so each hidden version is current again")
    parser.add_argument("--bucket-id", help="the bucket's ID: the locked backup bucket for cell-data, "
                        "the old cloud bucket for founder, the media bucket for media")
    return parser


def main(argv: Sequence[str], env: Mapping[str, str], *, opener: Opener | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # argparse: 0 for --help, 2 for a bad argument
        return exit_request.code if isinstance(exit_request.code, int) else 2
    rescue_target = args.account in RESCUE_ACCOUNTS
    problems = [
        message for failed, message in (
            (not _CELL_ID.fullmatch(args.cell_id), "the cell id is not 16 characters of a-z and 2-7"),
            (not args.bucket_id, "--bucket-id is required"),
            (rescue_target and args.apply,
             f"{args.account} has no --apply: the executors purge it; --rescue deletes only hide markers"),
            (not rescue_target and args.rescue, "--rescue is for cell-data and media"),
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
        if rescue_target:
            count_or_rescue(session, label=args.account, cell_id=args.cell_id, rescue=args.rescue,
                            bucket_id=args.bucket_id, out=out)
        else:
            clean_up_founder(session, cell_id=args.cell_id, apply=args.apply, bucket_id=args.bucket_id, out=out)
    except (OSError, http.client.HTTPException, ValueError, KeyError) as exc:
        # The type only: a provider message could carry an ID.
        print(f"cleanup stopped: {type(exc).__name__}; rerun it, every step is safe to repeat", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
