"""`exomem cell-media-census`: the cell's drain record, for the backup report (D12).

The nightly backup Job runs this beside its use figure. It prints the record line, `<count> <bytes> <census
time>`, that the cell wrote when it last counted its evicted originals. When the record is missing or malformed it
prints `unknown unknown` and the reason on stderr. The Job mounts the volume read-only, so this reads one file and
opens no store, lease or database.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import media_drain_record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-census")
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    args = parser.parse_args(argv)
    try:
        line = media_drain_record.read(args.vault)
    except media_drain_record.RecordUnreadable as error:
        print("unknown unknown")
        print(error, file=sys.stderr)
        return 1
    print(line)
    return 0
