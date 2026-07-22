"""Validate that a PR title is a Release Please-compatible commit header."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence

_CONVENTIONAL_HEADER = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?: "
    r"(?P<description>\S(?:.*\S)?)$"
)
_EXPECTED = "<type>[optional scope][!]: <description>"


def validate_pr_title(title: str) -> str | None:
    """Return an explanation when *title* is not a conventional commit header."""

    if _CONVENTIONAL_HEADER.fullmatch(title) is None:
        return (
            f"expected {_EXPECTED} with a lowercase type, optional non-empty scope, "
            "and non-empty description without surrounding whitespace"
        )
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="pull-request title to validate")
    args = parser.parse_args(argv)

    error = validate_pr_title(args.title)
    if error is not None:
        print(f"Invalid PR title: {error}.", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  fix: describe the change", file=sys.stderr)
        print("  feat(parser)!: describe the breaking change", file=sys.stderr)
        return 1

    print(f"Valid PR title: {args.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
