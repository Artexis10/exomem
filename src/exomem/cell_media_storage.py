"""`exomem cell-media-storage`: the cell's storage record, for the backup report (add-cloud-media-store D10).

The nightly backup Job runs this beside its use figure and its census. It prints the record line that the cell
wrote when it last read its two storage figures (`media_storage_record`). When the record is missing or malformed it
prints nothing on stdout and the reason on stderr, and the Job reports the figures as unknown. The Job mounts the
volume read-only, so this reads one file and opens no store, lease or database.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import media_storage_record
from .state_record_file import RecordUnreadable
from .vault_store import cli_fence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-storage")
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    args = parser.parse_args(argv)
    try:
        cli_fence.require("cell-media-storage", args.vault)
        line = media_storage_record.read(args.vault)
    except (RecordUnreadable, cli_fence.StoreFenceRefusal) as error:
        print(error, file=sys.stderr)
        return 1
    print(line)
    return 0
