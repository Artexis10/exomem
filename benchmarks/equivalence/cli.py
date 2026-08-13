from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
from pathlib import Path

from .differ import compare_runs
from .selection import CANONICAL_LME_S_SOURCE, select_lme_s_25


def _canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _exclusive_write(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("selection output exists but is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            existing = handle.read()
        if existing != payload:
            raise ValueError("selection output exists with different bytes")
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def _select_lme(dataset: Path, revision: str, expected_sha256: str, output: Path) -> None:
    from lme.dataset import stable_dataset_bytes
    from protocol.models import LmeSelection

    if revision != CANONICAL_LME_S_SOURCE["revision"]:
        raise ValueError("source revision must equal the frozen LongMemEval-S revision")
    if expected_sha256 != CANONICAL_LME_S_SOURCE["sha256"]:
        raise ValueError("expected SHA-256 must equal the frozen LongMemEval-S SHA-256")
    raw = stable_dataset_bytes(dataset)
    if __import__("hashlib").sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("dataset SHA-256 differs from frozen LongMemEval-S source")
    if len(raw) != CANONICAL_LME_S_SOURCE["byte_count"]:
        raise ValueError("dataset byte count differs from frozen LongMemEval-S source")
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot decode canonical LongMemEval-S JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError("canonical LongMemEval-S dataset must be a JSON array")
    artifact = select_lme_s_25(rows, source=CANONICAL_LME_S_SOURCE)
    payload = _canonical_json_bytes(LmeSelection.model_validate(artifact).model_dump(mode="json"))
    _exclusive_write(output, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest="command", required=True)
    gate = command.add_parser("gate")
    gate.add_argument("--left", required=True)
    gate.add_argument("--right", required=True)
    gate.add_argument("--mode", required=True, choices=("blocking", "report"))
    # Artifacts belong beside the run they describe, never in whatever
    # directory the operator happened to be standing in.
    gate.add_argument("--out", default=None, help="defaults to the left run directory")
    gate.add_argument("--exceptions", default=None)
    gate.add_argument("--today", default=None, help="ISO date used to expire register entries")
    select_lme = command.add_parser("select-lme", help="generate the frozen LongMemEval-S 25-case artifact")
    select_lme.add_argument("--dataset", required=True, type=Path)
    select_lme.add_argument("--source-revision", required=True)
    select_lme.add_argument("--expected-sha256", required=True)
    select_lme.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "select-lme":
        _select_lme(args.dataset, args.source_revision, args.expected_sha256, args.out)
        return 0
    today = dt.date.fromisoformat(args.today) if args.today else (dt.date.today() if args.exceptions else None)
    result = compare_runs(
        args.left, args.right, mode=args.mode, out=args.out or Path(args.left),
        exceptions_path=args.exceptions, today=today,
    )
    return 1 if result.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
