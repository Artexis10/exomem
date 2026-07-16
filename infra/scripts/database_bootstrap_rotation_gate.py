#!/usr/bin/env python3
"""Validate an attempt-bound rotation receipt before returning the Job outcome."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RECEIPT_KIND = "exomem-database-admin-rotation"
GATE_FAILURE_STATUS = 70
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECEIPT_FIELDS = {
    "schemaVersion",
    "kind",
    "attemptId",
    "credentialVersion",
    "rotatedOrRevokedAt",
}
_MAX_RECEIPT_BYTES = 4096


class RotationReceiptError(RuntimeError):
    """Content-free receipt validation failure."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RotationReceiptError("database admin rotation receipt is invalid")
        value[key] = item
    return value


def _read_private_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or mode & 0o077
            or mode & 0o111
            or not mode & stat.S_IRUSR
            or before.st_size <= 0
            or before.st_size > _MAX_RECEIPT_BYTES
        ):
            raise RotationReceiptError("database admin rotation receipt is invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RotationReceiptError("database admin rotation receipt is invalid")
            content = os.read(descriptor, _MAX_RECEIPT_BYTES + 1)
            if os.read(descriptor, 1) or len(content) > _MAX_RECEIPT_BYTES:
                raise RotationReceiptError("database admin rotation receipt is invalid")
        finally:
            os.close(descriptor)
    except RotationReceiptError:
        raise
    except OSError as error:
        raise RotationReceiptError("database admin rotation receipt is invalid") from error
    return content, opened


def validate_receipt(
    *,
    receipt: Path,
    attempt_id: str,
    credential_version: str,
    attempt_start_ns: int,
) -> None:
    if (
        not _IDENTITY.fullmatch(attempt_id)
        or not _IDENTITY.fullmatch(credential_version)
        or attempt_start_ns <= 0
    ):
        raise RotationReceiptError("database admin rotation receipt is invalid")
    content, metadata = _read_private_regular_file(receipt)
    if metadata.st_mtime_ns <= attempt_start_ns:
        raise RotationReceiptError("database admin rotation receipt is invalid")
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RotationReceiptError("database admin rotation receipt is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != _RECEIPT_FIELDS
        or type(payload["schemaVersion"]) is not int
        or payload["schemaVersion"] != 1
        or payload["kind"] != RECEIPT_KIND
        or payload["attemptId"] != attempt_id
        or payload["credentialVersion"] != credential_version
        or not isinstance(payload["rotatedOrRevokedAt"], str)
        or not payload["rotatedOrRevokedAt"].endswith("Z")
    ):
        raise RotationReceiptError("database admin rotation receipt is invalid")
    try:
        rotated_at = datetime.fromisoformat(
            payload["rotatedOrRevokedAt"][:-1] + "+00:00"
        )
    except ValueError as error:
        raise RotationReceiptError("database admin rotation receipt is invalid") from error
    if rotated_at.tzinfo is None or rotated_at.utcoffset() != timedelta(0):
        raise RotationReceiptError("database admin rotation receipt is invalid")
    attempt_start = datetime.fromtimestamp(attempt_start_ns / 1_000_000_000, tz=UTC)
    if rotated_at <= attempt_start or rotated_at > datetime.now(UTC) + timedelta(minutes=5):
        raise RotationReceiptError("database admin rotation receipt is invalid")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--credential-version", required=True)
    parser.add_argument("--attempt-start-ns", required=True, type=int)
    parser.add_argument("--job-status", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not 0 <= arguments.job_status <= 255:
        print("database bootstrap outcome is invalid")
        return GATE_FAILURE_STATUS
    try:
        validate_receipt(
            receipt=arguments.receipt,
            attempt_id=arguments.attempt_id,
            credential_version=arguments.credential_version,
            attempt_start_ns=arguments.attempt_start_ns,
        )
    except RotationReceiptError as error:
        print(str(error))
        return GATE_FAILURE_STATUS
    print("Database admin rotation receipt verified")
    return arguments.job_status


if __name__ == "__main__":
    raise SystemExit(main())
