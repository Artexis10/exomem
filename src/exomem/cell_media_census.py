"""`exomem cell-media-census`: the evicted originals of a cell's media store, for the backup report (D12).

The nightly backup Job runs this beside its use figure. It prints `<count> <bytes>`, or `unknown unknown` with
the reason on stderr when the census is incomplete, and cellctl reads that line from the Job's termination message.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import media_store, originals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-census")
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    parser.add_argument("--store", required=True, help="the store identity that the cell's policy names")
    args = parser.parse_args(argv)
    try:
        count, size = media_store.evicted_census(args.vault, originals.StoreBinding(args.store))
    except media_store.MediaStoreError as error:
        print("unknown unknown")
        print(error, file=sys.stderr)
        return 1
    print(count, size)
    return 0
