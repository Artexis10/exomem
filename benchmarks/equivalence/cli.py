from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from .differ import compare_runs


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
    args = parser.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else (dt.date.today() if args.exceptions else None)
    result = compare_runs(
        args.left, args.right, mode=args.mode, out=args.out or Path(args.left),
        exceptions_path=args.exceptions, today=today,
    )
    return 1 if result.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
