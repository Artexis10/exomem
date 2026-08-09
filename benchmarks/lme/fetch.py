"""Pinned, user-run LongMemEval-S fetch instructions and checksum verification."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


HF_DATASET = "xiaowu0162/longmemeval-cleaned"
VARIANT = "LongMemEval-S"


class ChecksumMismatch(ValueError):
    """A local dataset file does not match its recorded sha256."""


def fetch_instructions() -> str:
    return f"""Dataset fetch is deliberately user-run and never performed by this package.

Pinned source: Hugging Face dataset {HF_DATASET}
Pinned variant: {VARIANT} from the cleaned September 2025 refresh

1. Download the -S JSON from the pinned dataset page into a location outside this repository.
2. On first fetch, run:
   python -m benchmarks.lme.fetch verify PATH/TO/longmemeval_s_cleaned.json
3. Record the printed sha256 beside the run configuration.
4. On every evaluation, verify the recorded value with:
   python -m benchmarks.lme.fetch verify PATH/TO/longmemeval_s_cleaned.json --sha256 RECORDED_SHA256
"""


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path | str, recorded: str) -> str:
    """Compare one local file with the sha256 already recorded by the user."""

    actual = file_sha256(path)
    expected = recorded.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ChecksumMismatch("recorded sha256 must be 64 lowercase hexadecimal characters")
    if actual != expected:
        raise ChecksumMismatch(f"sha256 mismatch: recorded {expected}, observed {actual}")
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    verify = sub.add_parser("verify", help="print or verify a local file's sha256")
    verify.add_argument("path", type=Path)
    verify.add_argument("--sha256")
    args = parser.parse_args(argv)
    if args.command != "verify":
        print(fetch_instructions(), end="")
        return 0
    if args.sha256:
        print(verify_sha256(args.path, args.sha256))
    else:
        print(file_sha256(args.path))
        print("Record this sha256 in the run configuration before evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
